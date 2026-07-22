"""Slack event handler."""

import fcntl
import json
import os
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Set, Tuple

import requests

from src.config import Config, load_config
from src.git_manager import GitManager
from src.indexer import Indexer
from src.tools import Tools, Workspace
from src.agent import Agent
from src.budget_tracker import BudgetTracker
from src.slack.verify import verify_signature
from src.slack.responder import SlackResponder

logger = logging.getLogger(__name__)

# In-memory event deduplication
# Format: {event_id: timestamp}
PROCESSED_EVENTS: Dict[str, float] = {}
EVENT_TTL_SECONDS = 3600  # Keep events for 1 hour

def cleanup_old_events():
    """Remove events older than TTL to prevent memory bloat."""
    current_time = time.time()
    expired = [
        event_id for event_id, timestamp in PROCESSED_EVENTS.items()
        if current_time - timestamp > EVENT_TTL_SECONDS
    ]
    for event_id in expired:
        del PROCESSED_EVENTS[event_id]
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired events from dedup cache")

def is_duplicate_event(event_id: str) -> bool:
    """Check if event has already been processed."""
    cleanup_old_events()
    
    if event_id in PROCESSED_EVENTS:
        logger.info(f"Duplicate event detected: {event_id}")
        return True
    
    PROCESSED_EVENTS[event_id] = time.time()
    return False


# Guru's Slack identity -- confirmed live 2026-07-18 from real decline messages in
# #product / #tmp_betterbrain. Guru edits carry identity under event["message"], so
# we match on EITHER the user id or the bot_id. Messages from any OTHER bot
# (including our own replies, which would otherwise self-trigger) are still dropped.
GURU_BOT_USER_ID = "U028VSYP9CZ"
# This bot's own Slack user id (auth.test -> user_id, betterbrain_aha_doc_r).
# Needed because an @mention only arrives as an `app_mention` event if the Slack
# app subscribes to that event type; with only `message.channels` subscribed the
# same ping arrives as a plain `message` whose text contains this id. Matching on
# it lets a direct mention work under either subscription.
SELF_BOT_USER_ID = "U0BCJ3LSCG5"
GURU_BOT_ID = "B028AF2UNH4"

# A message counts as a Guru decline when its "go elsewhere" pointer is present.
# Matched against 4 real decline variants (2026-07-18) whose hedge wording all
# differed ("don't have a verified answer / corpus source / canonical list",
# "not as a verified current ... capability") but which ALL ended with a
# "Look here instead:" pointer -- the one marker unique to declines. A confident
# Guru answer does not include it, so this is the reliable signal.
GURU_DECLINE_PHRASES = ("look here instead",)


def looks_like_guru_decline(text: str) -> bool:
    # Normalize curly apostrophes to straight so "don't" matches Guru's "don’t".
    lowered = (text or "").lower().replace("’", "'").replace("‘", "'")
    return all(phrase in lowered for phrase in GURU_DECLINE_PHRASES)


def _collect_block_text(node: Any) -> str:
    """Recursively pull every string under a "text" key out of Slack blocks.
    Guru's answer body lives in blocks, not the flat top-level text field."""
    parts = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                parts.append(value)
            else:
                parts.append(_collect_block_text(value))
    elif isinstance(node, list):
        for item in node:
            parts.append(_collect_block_text(item))
    return " ".join(p for p in parts if p)


def normalize_event(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize Slack event to a consistent format."""
    event = body.get("event", {})
    event_type = event.get("type")

    # Guru streams answers by posting a placeholder ("Answer Generating...") then
    # EDITING it with the final answer/decline. That edit arrives as a
    # message_changed event whose real content lives under event["message"], so we
    # unwrap edits and treat the nested message as the effective message.
    is_edit = event.get("subtype") == "message_changed"
    if event.get("subtype") == "message_deleted":
        return None
    msg = (event.get("message") or {}) if is_edit else event

    msg_user = msg.get("user")
    msg_bot_id = msg.get("bot_id")
    is_bot_message = bool(msg_bot_id) or msg.get("subtype") == "bot_message" or msg_user == GURU_BOT_USER_ID

    if is_bot_message:
        # Only Guru gets through (match on user id OR bot_id). Every other bot,
        # including our own replies, is dropped to avoid a self-trigger loop.
        if msg_user != GURU_BOT_USER_ID and msg_bot_id != GURU_BOT_ID:
            return None
    elif is_edit:
        # A human editing their own message -- nothing to react to.
        return None

    # Guru's answer body lives in blocks (its flat text is just a placeholder),
    # but a normal human message carries the SAME content in both the flat text
    # field and its blocks -- so naively concatenating doubles human messages.
    # Only combine when each side adds something; otherwise take the longer one.
    flat = (msg.get("text") or "").strip()
    blocks = _collect_block_text(msg.get("blocks", [])).strip()
    if blocks and flat and blocks not in flat and flat not in blocks:
        full_text = (flat + " " + blocks).strip()
    else:
        full_text = blocks if len(blocks) > len(flat) else flat

    if event_type in ("app_mention", "message"):
        return {
            "type": event_type,
            "channel": event.get("channel"),
            "user": msg_user,
            "text": full_text,
            "ts": msg.get("ts") or event.get("ts"),
            "thread_ts": msg.get("thread_ts"),
            "is_bot_message": is_bot_message,
            "is_edit": is_edit,
        }

    return None


def should_process_event(event: Dict[str, Any], config: Config) -> bool:
    """Determine if we should process this event."""
    channel_id = event.get("channel")
    event_type = event.get("type")

    if not channel_id:
        return False

    # Check if channel is configured
    channel_config = config.channels.get(channel_id)
    if not channel_config:
        return False

    # Check triggers
    triggers = channel_config.triggers

    if event.get("is_bot_message"):
        # normalize_event() already dropped every bot message except Guru's. This
        # is only a STRUCTURAL gate -- is this Guru's substantial final answer in a
        # guruDecline channel? (>250 chars skips the short "Answer Generating..."
        # placeholder.) Whether that answer is actually a decline is judged by a
        # model in process_guru_decline, because Guru's wording varies too much for
        # reliable keyword matching.
        if not triggers.get("guruDecline", False):
            return False
        return len((event.get("text") or "").strip()) > 250

    if event_type == "app_mention" and triggers.get("appMention", True):
        return True

    # Same ping, different subscription. If the Slack app is only subscribed to
    # message.channels, an @mention never arrives as `app_mention` -- it comes
    # through as a `message` carrying our user id in its text. Without this, an
    # @mention in a channel with appMention enabled is silently ignored.
    if (
        event_type == "message"
        and triggers.get("appMention", False)
        and f"<@{SELF_BOT_USER_ID}>" in (event.get("text") or "")
    ):
        return True

    # An article/author command may arrive as a plain `message` (when only
    # message.channels is subscribed, not app_mention) -- accept it either way as
    # long as the channel has mentions enabled.
    if triggers.get("appMention", False) and (
        parse_article_command(event.get("text", "")) or parse_author_command(event.get("text", ""))
    ):
        return True

    if event_type == "message" and triggers.get("questionLikeMessages", False):
        # Check if message looks like a question
        text = event.get("text", "").lower()
        if "?" in text or any(text.startswith(q) for q in ["how", "what", "why", "where", "when", "who"]):
            return True

    return False


def handle(event: Dict[str, Any], headers: Dict[str, str], body: str) -> Dict[str, Any]:
    """
    Main Slack event handler.
    
    Args:
        event: Parsed event body
        headers: Request headers
        body: Raw request body string
    
    Returns:
        Response dict with statusCode and body
    """
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Configuration error"})
        }
    
    # Verify signature
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    signature = headers.get("X-Slack-Signature", "")
    
    if not verify_signature(config.slack.signing_secret, timestamp, body, signature):
        logger.warning("Invalid Slack signature")
        return {
            "statusCode": 401,
            "body": json.dumps({"error": "Invalid signature"})
        }
    
    # Handle URL verification challenge
    if event.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "body": json.dumps({"challenge": event.get("challenge")})
        }
    
    # Normalize event
    normalized_event = normalize_event(event)
    if not normalized_event:
        logger.info("Event ignored (bot message or unsupported type)")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
    
    # Check for duplicate events. Guru's placeholder and its edits share one ts, so
    # for edits we fold the text into the key -- each distinct edit content is
    # evaluated once (until the full decline text arrives) without colliding.
    event_id = f"{normalized_event.get('channel')}_{normalized_event.get('ts')}"
    if normalized_event.get("is_edit"):
        event_id += f"_{hash(normalized_event.get('text', ''))}"
    if is_duplicate_event(event_id):
        logger.info(f"Skipping duplicate event: {event_id}")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
    
    # Check if we should process this event
    if not should_process_event(normalized_event, config):
        logger.info(
            "Event ignored (channel not configured or triggers not met): "
            f"type={normalized_event.get('type')} channel={normalized_event.get('channel')} "
            f"bot={normalized_event.get('is_bot_message')} len={len(normalized_event.get('text') or '')}"
        )
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
    
    # Process event asynchronously (in production, this would be queued)
    try:
        if normalized_event.get("is_bot_message"):
            process_guru_decline(normalized_event, config)
        elif parse_author_command(normalized_event.get("text", "")):
            process_author_request(normalized_event, config)
        elif parse_article_command(normalized_event.get("text", "")):
            process_article_request(normalized_event, config)
        else:
            ch_cfg = config.channels.get(normalized_event.get("channel"))
            if ch_cfg and getattr(ch_cfg, "project", None):
                process_question(normalized_event, config)
            elif ch_cfg and ch_cfg.triggers.get("guruDecline") and config.betterbrain:
                # A direct @mention in a product-knowledge channel. These channels
                # have no `project`, so the codebase-search path above cannot serve
                # them -- but the BetterBrain cascade can, and a product question is
                # what someone @mentioning the bot in #product actually wants.
                #
                # This is also the trigger that does not depend on Guru. The
                # guruDecline path only fires when Guru posts a decline, so without
                # this branch BetterBrain would go completely silent in these
                # channels the moment Guru is removed.
                process_mention_question(normalized_event, config)
            else:
                # @mention in a non-Q&A channel with no project -- offer the one
                # thing we can do here rather than erroring.
                SlackResponder(config.slack.bot_token).post_message(
                    normalized_event["channel"],
                    "I can draft a support-article scaffold from the PKR corpus — try: "
                    "`@BetterBrain draft article: <feature name>`",
                    thread_ts=normalized_event.get("thread_ts") or normalized_event.get("ts"),
                )
    except Exception as e:
        logger.error(f"Failed to process event: {e}")
        # Still return 200 to Slack to avoid retries

    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def process_question(event: Dict[str, Any], config: Config):
    """Process a question from Slack."""
    channel = event["channel"]
    text = event["text"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    
    # Get channel config
    channel_config = config.channels.get(channel)
    if not channel_config:
        logger.warning(f"No config for channel {channel}")
        return
    
    # Get project config
    project_config = config.projects.get(channel_config.project)
    if not project_config:
        logger.error(f"Project {channel_config.project} not found")
        return
    
    # Initialize Slack responder
    responder = SlackResponder(config.slack.bot_token)
    
    # Post interim message
    interim_msg = None
    if config.slack.post_interim_message:
        interim_response = responder.post_interim_message(channel, thread_ts)
        if interim_response.get("ok"):
            interim_msg = interim_response.get("ts")
    
    try:
        # Ensure repository exists
        git_manager = GitManager(config.projects_dir)
        repo_path = git_manager.ensure_repo(project_config, skip_pull_if_exists=True)
        
        # Build index (or use cached)
        indexer = Indexer.get_or_create(repo_path, config.access, force_rebuild=False)
        
        # Initialize tools and workspace
        workspace = Workspace()
        tools = Tools(indexer, workspace)
        
        # Load agent directory
        agent_dir = repo_path / project_config.agent_dir
        if not agent_dir.exists():
            agent_dir = repo_path  # Fallback to repo root
        
        # Initialize agent with trace callback
        def trace_callback(kind: str, title: str, detail: str = ""):
            logger.info(f"[{kind}] {title}")
            if detail:
                logger.debug(detail[:500])
        
        # Create budget tracker
        budget_tracker = BudgetTracker()
        
        agent = Agent(config, tools, workspace, agent_dir, trace_callback, budget_tracker, project_root=repo_path)
        
        # Ask question
        answer = agent.ask(text, agent_name=channel_config.agent)
        
        # Append budget footer
        footer = "\n\n---\n" + budget_tracker.format_slack_footer()
        final_answer = answer + footer
        
        # Post answer
        if interim_msg and config.slack.response_mode == "thread":
            # Update interim message
            responder.update_message(channel, interim_msg, final_answer)
        else:
            # Post new message
            responder.post_message(channel, final_answer, thread_ts=thread_ts)
    
    except Exception as e:
        logger.error(f"Error processing question: {e}", exc_info=True)
        error_msg = f"❌ Sorry, I encountered an error: {str(e)[:200]}"
        
        if interim_msg:
            responder.update_message(channel, interim_msg, error_msg)
        else:
            responder.post_message(channel, error_msg, thread_ts=thread_ts)


def fetch_parent_message_text(bot_token: str, channel: str, parent_ts: str) -> Optional[str]:
    """Fetch the text of the message a Guru decline was replying to.

    Guru always threads its reply onto the original question (thread_ts on the
    Guru message equals the parent's own ts) -- confirmed against a real example
    2026-07-18. conversations.replies with limit=1 returns the parent as the
    first (and here, only) message.
    """
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"channel": channel, "ts": parent_ts, "limit": 1},
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch parent message: {e}")
        return None

    if not data.get("ok"):
        logger.error(f"conversations.replies failed: {data.get('error')}")
        return None

    messages = data.get("messages") or []
    if not messages:
        return None
    return messages[0].get("text") or None


USAGE_LOG = Path(
    os.getenv("BETTERBRAIN_USAGE_LOG")
    or Path.home() / "Library" / "Logs" / "BetterBrainSupport" / "usage.jsonl"
)


def _record_usage(label: str, argv: list, data: dict, usage: dict) -> None:
    """Append one structured usage row per `claude -p` call.

    The same numbers already go to the text log, but only as prose -- fine for
    tailing, useless for "what did this cost us last month and what did we get
    for it". One JSON object per line is aggregable, and recording the model
    means a cheaper-model experiment can be evaluated against the runs it
    replaced rather than against a vague memory of what things used to cost.

    Never let bookkeeping break an answer: any failure here is logged and
    swallowed.
    """
    try:
        model = "default"
        if "--model" in argv:
            model = argv[argv.index("--model") + 1]
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": label,
            "model": model,
            "cost_usd": data.get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "turns": data.get("num_turns"),
            "duration_ms": data.get("duration_ms"),
            "is_error": bool(data.get("is_error")),
        }
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:  # pragma: no cover - telemetry must never break a reply
        logger.warning(f"could not record usage row ({label}): {exc}")


def _run_claude(prompt: str, *, cwd: str, timeout: int, extra_args: Optional[list] = None,
                label: str = "claude", with_status: bool = False):
    """Run `claude -p` with --output-format json so every subscription-billed call
    logs its token usage + cost (these all draw on the Claude Max plan, so we want
    visibility as the team hammers the POC). Returns the result text, or None on
    timeout / missing CLI / non-zero exit / unparseable output (callers treat None
    as failure and fall back).

    With ``with_status=True`` returns a ``(text, status)`` tuple instead, where
    status is one of ``ok`` / ``empty`` / ``timeout`` / ``not_found`` / ``error``
    -- so a caller can tell a timeout apart from a genuine empty result. The
    default (no status) keeps the plain ``Optional[str]`` contract other callers
    rely on."""
    def _ret(text: Optional[str], status: str):
        return (text, status) if with_status else text

    argv = ["claude", "-p", prompt, "--output-format", "json"] + (extra_args or [])
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f"claude -p timed out ({label})")
        return _ret(None, "timeout")
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH -- is Claude Code installed on this machine?")
        return _ret(None, "not_found")
    if result.returncode != 0:
        logger.error(f"claude -p exited {result.returncode} ({label}): {result.stderr[:400]}")
        return _ret(None, "error")
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"claude -p output not JSON ({label}); treating as failure")
        return _ret(None, "error")
    usage = data.get("usage") or {}
    logger.info(
        f"[usage] {label}: in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
        f"cache_read={usage.get('cache_read_input_tokens')} cost=${data.get('total_cost_usd')} "
        f"turns={data.get('num_turns')} {data.get('duration_ms')}ms (Max subscription)"
    )
    _record_usage(label, argv, data, usage)
    if data.get("is_error"):
        logger.error(f"claude -p returned is_error ({label}): {str(data.get('result'))[:300]}")
        return _ret(None, "error")
    text = data.get("result")
    return _ret(text, "ok" if text else "empty")


def run_betterbrain_cascade(question: str, betterbrain_config) -> Tuple[Optional[str], str]:
    """Run the /betterbrain-ask skill headlessly against the given question.

    Returns ``(answer, status)`` -- answer is the cascade result (or None), and
    status is ``ok`` / ``empty`` / ``timeout`` / ``not_found`` / ``error`` so the
    caller can DM a timeout differently from a genuine dry run.

    This is a read-only invocation by design (see .claude/settings.json in the
    BetterBrain repo): search + escalation across BetterBrain/Confluence/Aha/
    GitHub/Slack works headlessly, but gap-logging and PKR-drafting (steps 4-5
    of the skill) require interactive approval and will silently no-op here --
    that's intentional for v0, not a bug. Those stay a deliberate follow-up
    action in an interactive session, not something this daemon does unattended.
    """
    if not betterbrain_config:
        logger.error("No betterbrain config set -- cannot run cascade")
        return None, "error"
    # The skill's own output guidance is written for an interactive operator, who
    # wants the full derivation. This answer goes into a busy Slack channel where
    # a wall of text reads as less confident, not more -- and where nobody wants
    # the bot's internal bookkeeping. Constrain the shape at the call site so the
    # skill stays verbose where verbosity is useful.
    answer, status = _run_claude(
        f"/betterbrain-ask {question}\n\n"
        "OUTPUT CONTRACT — this answer is posted directly into a Slack thread for "
        "non-technical colleagues, so it must be short:\n"
        "1. Lead with the answer in ONE sentence. No preamble, no restating the question.\n"
        "2. Then ONE line of provenance THAT COVERS THE LEAD SENTENCE AND NOTHING ELSE. "
        "It is not a bibliography for the whole answer -- every other claim carries its "
        "own source under rule 3. Pick by SOURCE AUTHORITY, not by how sure you "
        "feel. Exactly one of:\n"
        "   - `Verified · PKR-XXXXXX` -- a verified PKR already backs this.\n"
        "   - `Not yet in BetterBrain — from <source>` -- you derived it from an "
        "authoritative source: implementation code, release notes, support docs, API "
        "docs, or an explicit Product/Engineering confirmation. The answer is well "
        "grounded; it simply is not recorded as durable knowledge yet.\n"
        "   - `Unconfirmed — from <source>` -- your only sources are enrichment-grade: "
        "Confluence, Aha, or a Slack thread that is not a Product/Eng confirmation. "
        "Doctrine says these cannot establish product truth on their own.\n"
        "   - `Not in BetterBrain` -- no usable source at all.\n"
        "   Do NOT write `Not verified`. It reads as 'I am guessing' and it undersells "
        "an answer read straight out of current source code, which is the strongest "
        "evidence there is. Reserve doubt for sources that actually warrant it.\n"
        "   When that source is a Confluence page, an Aha item or a Slack thread, put its "
        "last-updated date in the line -- e.g. `Unconfirmed — from Confluence "
        "(ENG/3679748113, updated Mar 2025)`. A reader can then weigh the answer without "
        "chasing the source. This is cheap and it matters: a 16-month-old page produced a "
        "confidently wrong answer on 2026-07-21, and the date alone would have made it "
        "visibly suspect.\n"
        "3. Then AT MOST three short bullets, only for detail that changes what the "
        "reader does next (e.g. a caveat that limits when the answer holds).\n"
        "   EVERY BULLET THAT ASSERTS A FACT NAMES ITS OWN SOURCE, inline and in "
        "parentheses -- e.g. `(code: hris-connector ukg_sync.py)`, `(PKR-000553)`, "
        "`(Slack #signaturesupport, Aug 2024)`. If you cannot name the artifact that "
        "asserts a bullet, DELETE THE BULLET. The line-2 provenance does not stand "
        "behind it.\n"
        "   Why this rule exists: on 2026-07-22 an answer was labelled `from GitHub + "
        "Slack` when the code proved only that UKG shares a pipeline, while a two-year-old "
        "Slack thread carried the actual email-update behavior. One label covered two "
        "claims of very different strength, and the weaker one inherited the stronger "
        "one's credibility. If the lead sentence and a bullet do not share a source, they "
        "must not share a label.\n"
        "4. Stop. Target ~700 characters.\n"
        "Put derivation, file/line evidence, per-source breakdowns and alternate "
        "readings NOWHERE in this answer -- they belong in a follow-up if asked.\n"
        "Never narrate your own process or environment: no step numbers, no "
        "'I searched X then Y', and above all nothing about tool permissions, "
        "denied writes, gap logs, or drafts you were unable to save. That is "
        "operator bookkeeping and it is noise to the person who asked.\n"
        "The FIRST CHARACTER of your reply is the first word of the answer sentence. No "
        "status preamble -- not 'Confirmed still current', not 'Ready to give the answer', "
        "not 'Here's what I found'. Those read as a machine talking to its operator in a "
        "channel where colleagues are reading.\n"
        "If you have no confident answer, say so in one line and name the best next "
        "step. A short honest decline is more useful than a long hedge.\n"
        "\n"
        "HARD RULES — these are prohibitions, not style preferences. Each one is here "
        "because it was broken on 2026-07-22 and produced a confidently wrong answer.\n"
        "A. A Claim Ledger entry (CLAIM-XXXXXX) may NEVER be stated as current behavior, "
        "in any tier, under any phrasing. The Claim Ledger exists to hold what is NOT yet "
        "true. If a claim is your only support, the answer is 'not current behavior -- "
        "tracked as planned/aspirational', naming its lifecycle status. Broken by reading "
        "CLAIM-000292 as shipped: the answer said the reminder due date is the Admin "
        "Review submit date, when the code sets due_date = conversation_end "
        "unconditionally and the submit-date behavior is an unshipped Aha request.\n"
        "B. For any question about whether a FIELD, PARAMETER or CAPABILITY exists, quote "
        "the actual field list or schema. Never infer it from an endpoint name, an article "
        "title or a feature description. Broken by answering 'can we bulk-update user time "
        "zones' with 'yes, use /api/v1/users/bulk/'. That endpoint is real and has no "
        "timezone field. An endpoint existing is not the field existing.\n"
        "C. Never say something is NOT supported unless a source says so. Absence of "
        "evidence is not a limitation. Say 'nothing in the corpus documents X' instead. "
        "Broken by answering 'there's no auto-approve option' from a PKR that simply "
        "never mentions auto-approval. See also rule E, which governs how you may say "
        "that.\n"
        "E. NEGATIVE-EXISTENCE CHECK — run this before writing ANY claim that a "
        "document, field, setting or capability does not exist. It is a three-step "
        "procedure, not a caution.\n"
        "   Step 1: list the source types you ACTUALLY searched -- PKRs, claim ledger, "
        "support docs, release notes, API docs, Confluence, Aha, code. The ones you "
        "searched, not the ones you could have.\n"
        "   Step 2: if any AUTHORITATIVE type is missing from that list, go search it "
        "before answering. Support docs are the most common omission and the most common "
        "place the answer actually turns out to be.\n"
        "   Step 3: put the list in the answer -- e.g. 'nothing found in PKRs, support "
        "docs or release notes'. That lets the reader tell a thorough search from a "
        "shallow one, and forces you to notice which you did.\n"
        "   Never write a bare 'there is no documented X'. A failed retrieval is not an "
        "absent document. Broken on 2026-07-22: asked the ideal Engage email logo size, "
        "the answer was 'No documented ideal size exists'. The guidance was in support "
        "article 4539420726029 -- 'a maximum image width of 207 px' -- already imported "
        "into this corpus, and a colleague quoted it from that article ten minutes later.\n"
        "   That same answer also invented a figure, 'roughly 150-250px', with no source "
        "behind it. If you cannot cite a number, do not produce one. An invented number "
        "that happens to be close is still invented.\n"
        "D. SCOPE CHECK — perform this before writing the provenance line, every time.\n"
        "   Step 1: read the `fact` field of the record you are about to cite.\n"
        "   Step 2: list any PROVIDER (BambooHR, UKG, Workday, TriNet Zenefits), EDITION "
        "(Classic, NextGen) or SURFACE (anytime vs scheduled, mobile, API, kiosk) that the "
        "fact names.\n"
        "   Step 3: if it names one the QUESTION did not, that record CANNOT be the "
        "provenance for a general answer. You then have exactly two legal moves: cite a "
        "record that covers the general case, or narrow your answer to the specific case "
        "that record covers and say which case it is. There is no third move.\n"
        "   If every hit you have is provider- or edition-specific, SEARCH AGAIN for the "
        "general case before answering. Answering a general question from the first narrow "
        "hit is precisely the failure this rule exists to stop, and it shows up as a "
        "suspiciously short run.\n"
        "   Broken twice on 2026-07-22: a general question about 'the location field in "
        "the HRIS file' was labelled `Verified · PKR-000551`, which is BambooHR-specific "
        "and says Location comes from BambooHR's City field -- true of BambooHR, not of "
        "HRIS generally; and an anytime-conversations question was answered from "
        "PKR-000471, which covers Classic missed-deadline edit requests.\n"
        "   Note the second-order cost: mislabelling a narrow record as `Verified` also "
        "suppresses the verifier @mention, so the answer silences the human check that "
        "would have caught it.\n"
        "\n"
        "VERIFIER: if the answer is NOT already backed by a verified PKR, end with one "
        "line @mentioning the owner for that product area and asking them to correct it:\n"
        "  Calibration / Platform / Talent -> <@U031J16QHEV> (Kate Malcolm)\n"
        "  Conversations / Feedback        -> <@U045DHXMGDT> (Neeraj Mohan)\n"
        "  Goals                           -> <@U04KCTA594Y> (Varnika Garg)\n"
        "  Reporting / Analytics           -> <@U051W375UJK> (Arnav Garg)\n"
        "  Integrations / HRIS             -> <@U03MC6LERH9> (Rinku Ravi)\n"
        "  Engage                          -> <@U066KKBKKBJ> (Sharan M)\n"
        "Phrase it as e.g. '<@U03MC6LERH9> correct me if this is wrong.' Mention exactly "
        "one person, and only when no verified PKR backs the answer -- pinging on every answer trains "
        "people to mute the bot, which costs the correction this is meant to earn. If the "
        "product area is unclear or unlisted, mention nobody.",
        cwd=str(betterbrain_config.repo_path),
        timeout=betterbrain_config.cli_timeout_seconds, label="cascade", with_status=True,
        # Sonnet, not the default. The cascade searches, reads and synthesises with
        # citations -- work this tier handles well -- and 18 runs on the default
        # model cost $31.51 ($1.75 each), which does not scale to three channels.
        #
        # Model tier was not the quality lever here: the 2026-07-21 Workday answer
        # was wrong on the *default* model, and the fix was the precedence rule that
        # makes it check code before trusting a doc. The output contract, provenance
        # line and verifier mention do the quality work; the model does the reading.
        #
        # Override with BETTERBRAIN_CASCADE_MODEL to A/B against the usage log.
        extra_args=["--model", os.getenv("BETTERBRAIN_CASCADE_MODEL", "sonnet")],
    )
    return ((answer or "").strip() or None), status


DECLINE_CLASSIFIER_PROMPT = (
    "A support user asked a question and the Guru knowledge bot replied. Decide "
    "whether Guru FULLY and confidently answered it, or whether it DECLINED / "
    "hedged / said it could not verify / pointed the user elsewhere -- meaning a "
    "human or a deeper search should step in. A cited but negative-or-uncertain "
    "reply (\"I don't have a verified source\", \"not confirmed\", \"look here "
    "instead\") counts as DECLINE. Reply with exactly one word: DECLINE or ANSWER."
    "\n\nQUESTION:\n{question}\n\nGURU RESPONSE:\n{guru}"
)


def guru_response_is_decline(question: str, guru_text: str, betterbrain_config) -> bool:
    """Ask a fast model whether Guru's reply is a decline/hedge (BetterBrain should
    step in) or a confident answer (stay silent). Guru's phrasing varies too much
    for reliable keyword matching. Falls back to the keyword matcher on any model
    failure, so behavior never regresses below hard-decline detection."""
    prompt = DECLINE_CLASSIFIER_PROMPT.format(
        question=(question or "")[:1500], guru=(guru_text or "")[:4000]
    )
    cwd = str(betterbrain_config.repo_path) if betterbrain_config else None
    out = _run_claude(prompt, cwd=cwd, timeout=60, extra_args=["--model", "haiku"],
                      label="decline-classifier")
    if out is None:  # any failure -> keyword fallback, never regress
        return looks_like_guru_decline(guru_text)

    verdict = out.strip().upper()
    tokens = verdict.split()
    last = tokens[-1] if tokens else ""
    if last == "DECLINE":
        logger.info("decline classifier verdict: DECLINE")
        return True
    if last == "ANSWER":
        logger.info("decline classifier verdict: ANSWER")
        return False
    logger.warning(f"decline classifier unclear verdict {verdict[:60]!r}; keyword fallback")
    return looks_like_guru_decline(guru_text)


def log_gap(question: str, answer: Optional[str], channel: str, betterbrain_config,
            status: str = "ok") -> None:
    """Append a Guru-declined question (+ BetterBrain's cascade result) to the corpus
    gap log, so every miss becomes a durable, reviewable candidate for a PKR draft
    (Step 5 of the /betterbrain-ask skill) instead of evaporating after the DM.

    Best-effort and fcntl-locked: two concurrent cascades can append safely, and a
    logging failure must never break the DM flow. This only ever *appends a review
    candidate* -- it never writes a PKR or the claim ledger (that stays an
    interactive, human-in-the-loop step by design)."""
    if not betterbrain_config:
        return
    try:
        gap_path = (
            Path(str(betterbrain_config.repo_path))
            / "knowledge-corpus" / "generated" / "betterbrain-ask-gaps.jsonl"
        )
        if answer:
            summary = answer[:1500]
        elif status == "timeout":
            summary = "Cascade timed out before finishing -- no answer captured."
        elif status in ("error", "not_found"):
            summary = f"Cascade failed to run ({status}) -- no answer captured."
        else:
            summary = "Cascade produced no answer."
        entry = {
            "question": question,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "escalated_to": [],  # bot can't see which live sources the cascade hit
            "found_elsewhere": bool(answer),
            "summary": summary,
            "cascade_status": status,
            "promoted_to_pkr": None,
            "source": "guru-decline-slack-bot",
            "channel": channel,
        }
        gap_path.parent.mkdir(parents=True, exist_ok=True)
        with open(gap_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        logger.info(f"Logged gap (status={status}) for: {question[:80]}")
    except Exception as e:
        logger.warning(f"Failed to log gap: {e}")


ARTICLE_CMD_RE = re.compile(
    r"draft\s+(?:an?\s+)?article(?:\s+(?:on|for|about))?\s*:?\s*(.+)", re.IGNORECASE | re.DOTALL
)


def parse_article_command(text: str) -> Optional[Dict[str, str]]:
    """Parse `@BetterBrain draft article: <topic>` (optionally with `integration`
    or `area=Goals`). Returns None if the message isn't an article request."""
    stripped = re.sub(r"<@[^>]+>", "", text or "").strip()
    m = ARTICLE_CMD_RE.search(stripped)
    if not m:
        return None
    topic = m.group(1).strip()
    tmpl = "integration" if re.search(r"\bintegration\b", topic, re.IGNORECASE) else "feature"
    area = ""
    am = re.search(r"\barea[=:]\s*([A-Za-z]+)", topic)
    if am:
        area = am.group(1)
        topic = re.sub(r"\barea[=:]\s*[A-Za-z]+", "", topic).strip()
    topic = topic.strip(" .:-")
    if not topic:
        return None
    return {"topic": topic, "tmpl": tmpl, "area": area}


def process_article_request(event: Dict[str, Any], config: Config):
    """Run draft_support_article.py against a support-requested topic and post the
    generated SCAFFOLD + gap manifest back to the thread. Deterministic and
    draft-only: it prepares an authoring scaffold from publishable PKRs (release-
    gated), it does NOT write finished prose or publish anything customer-facing."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    responder = SlackResponder(config.slack.bot_token)

    cmd = parse_article_command(event.get("text", ""))
    if not cmd:
        responder.post_message(
            channel,
            "Usage: `@BetterBrain draft article: <feature name>` "
            "(add `integration` or `area=Goals` to refine).",
            thread_ts=thread_ts,
        )
        return

    bb = config.betterbrain
    if not bb:
        responder.post_message(channel, "BetterBrain repo isn't configured.", thread_ts=thread_ts)
        return

    py = str(Path(str(bb.repo_path)) / ".venv" / "bin" / "python3")
    argv = [py, "scripts/draft_support_article.py", "--new", cmd["tmpl"], "--name", cmd["topic"],
            "--rerank", "--rerank-model", "mistral-small3.2"]
    if cmd["area"]:
        argv += ["--area", cmd["area"]]

    responder.post_message(
        channel,
        f"📝 Drafting a *{cmd['tmpl']}* article for *{cmd['topic']}*"
        f"{' (area: ' + cmd['area'] + ')' if cmd['area'] else ''} — matching PKRs, then writing the "
        "full draft. ~5-12 min…",
        thread_ts=thread_ts,
    )

    try:
        result = subprocess.run(
            argv, cwd=str(bb.repo_path), capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        responder.post_message(channel, "❌ Article generator timed out.", thread_ts=thread_ts)
        return
    except FileNotFoundError:
        responder.post_message(
            channel, "❌ BetterBrain venv/python not found on this machine.", thread_ts=thread_ts
        )
        return

    if result.returncode != 0:
        logger.error(f"draft_support_article exited {result.returncode}: {result.stderr[:500]}")
        responder.post_message(
            channel, f"❌ Generator exited {result.returncode}:\n```{result.stderr[-600:]}```",
            thread_ts=thread_ts,
        )
        return

    m = re.search(r"^\s*([a-z0-9][a-z0-9-]+):\s+closed=(\d+)", result.stdout, re.MULTILINE)
    if not m:
        responder.post_message(
            channel, f"Generated, but couldn't locate the output:\n```{result.stdout[-600:]}```",
            thread_ts=thread_ts,
        )
        return

    slug = m.group(1)
    closed = int(m.group(2))
    summary = m.group(0).strip()
    out_dir = Path(str(bb.repo_path)) / "knowledge-corpus" / "generated" / "article-drafts"
    scaffold_f = out_dir / f"{slug}.scaffold.md"
    gaps_f = out_dir / f"{slug}.gaps.md"
    logger.info(f"Generated scaffold '{slug}' for '{cmd['topic'][:80]}' (closed={closed})")

    responder.post_message(channel, f"📄 Matched *{closed}* PKR(s) for `{slug}`. `{summary}`", thread_ts=thread_ts)
    if gaps_f.exists():
        responder.post_message(channel, "*Gap manifest — what's covered vs. missing:*", thread_ts=thread_ts)
        _deliver_doc(responder, channel, thread_ts, f"{slug}.gaps.md", f"{slug} — gap manifest", gaps_f.read_text())

    # Zero-coverage guard: don't spend a full authoring pass on a topic the corpus
    # barely covers -- hand back the scaffold and let a human decide instead.
    if closed == 0:
        responder.post_message(
            channel,
            "The corpus has ~no durable knowledge on this yet, so I'm not auto-writing it. "
            f"Scaffold attached; add PKR coverage, or force it with `@BetterBrain write it: {slug}`.",
            thread_ts=thread_ts,
        )
        if scaffold_f.exists():
            _deliver_doc(responder, channel, thread_ts, f"{slug}.scaffold.md", f"{slug} — scaffold", scaffold_f.read_text())
        return

    # One-step: go straight from scaffold to the finished draft.
    responder.post_message(channel, f"✍️ Now writing the full draft for `{slug}` — ~3-10 min…", thread_ts=thread_ts)
    _author_and_deliver(responder, channel, thread_ts, bb, slug)


AUTHOR_CMD_RE = re.compile(
    r"\b(?:write(?:\s+it|\s+the\s+article|\s+article|\s+up)?|author(?:\s+article)?)\b[:\s]+"
    r"([a-z0-9][a-z0-9-]{4,})",
    re.IGNORECASE,
)
AUTHOR_TIMEOUT_SECONDS = 900  # full authoring pass can run several minutes


def parse_author_command(text: str) -> Optional[str]:
    """Parse `@BetterBrain write it: <article-slug>` -> the slug, else None."""
    stripped = re.sub(r"<@[^>]+>", "", text or "").strip()
    m = AUTHOR_CMD_RE.search(stripped)
    return m.group(1).strip().lower() if m else None


def _chunk_text(text: str, size: int) -> list:
    """Split on line boundaries into <=size chunks for Slack message limits."""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > size:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks


def _deliver_doc(responder, channel, thread_ts, filename: str, title: str, content: str) -> None:
    """Deliver a generated doc as an uploaded .md file when the workspace grants
    files:write; otherwise fall back to chunked code-block messages so it still
    arrives. Uploading is much nicer than raw chunked markdown for scaffolds/articles."""
    if responder.upload_file(channel, content, filename, title=title, thread_ts=thread_ts):
        return
    for chunk in _chunk_text(content, 3500):
        responder.post_message(channel, "```" + chunk + "```", thread_ts=thread_ts)


def _author_and_deliver(responder, channel, thread_ts, bb, slug: str) -> bool:
    """Run /author-article on an existing scaffold (headless claude -p + acceptEdits so
    it can write its output) and post the finished draft. Draft-only: keeps
    [SCREENSHOT]/[VERIFY] markers + PKR citations; nothing is published. Assumes
    <slug>.scaffold.md exists. Returns True on success."""
    drafts = Path(str(bb.repo_path)) / "knowledge-corpus" / "generated" / "article-drafts"
    summary = _run_claude(
        f"/author-article {slug}", cwd=str(bb.repo_path), timeout=AUTHOR_TIMEOUT_SECONDS,
        extra_args=["--permission-mode", "acceptEdits"], label="author-article",
    )
    if summary is None:
        responder.post_message(channel, "❌ Authoring failed or timed out.", thread_ts=thread_ts)
        return False

    out_md = drafts / f"{slug}.md"
    if not out_md.exists():
        responder.post_message(
            channel,
            f"Authoring ran but produced no `{slug}.md`.\n```{(summary or '')[-600:]}```",
            thread_ts=thread_ts,
        )
        return False

    article = out_md.read_text()
    responder.post_message(channel, f"✅ *Draft article:* `{slug}` ({len(article)} chars)", thread_ts=thread_ts)
    _deliver_doc(responder, channel, thread_ts, f"{slug}.md", f"{slug} (draft article)", article)
    if summary and summary.strip():
        responder.post_message(channel, "*Author notes:*\n" + summary.strip()[-1500:], thread_ts=thread_ts)
    responder.post_message(
        channel,
        "_Still a DRAFT — capture the `[SCREENSHOT]` markers, resolve any `[VERIFY:]` flags, and "
        "review before publishing. `<!-- PKR-xxx -->` citations strip at publish._",
        thread_ts=thread_ts,
    )
    logger.info(f"Authored article '{slug}' ({len(article)} chars)")
    return True


def process_author_request(event: Dict[str, Any], config: Config):
    """Re-author an EXISTING scaffold via `@BetterBrain write it: <slug>` -- the power
    path for when someone edited a scaffold and wants to regenerate the prose. The main
    flow (draft article) already authors in one step; this is the manual re-run."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    responder = SlackResponder(config.slack.bot_token)

    slug = parse_author_command(event.get("text", ""))
    if not slug:
        responder.post_message(
            channel,
            "Usage: `@BetterBrain write it: <article-slug>` — re-author an existing scaffold "
            "(e.g. `recognition-badges-nextgen`).",
            thread_ts=thread_ts,
        )
        return

    bb = config.betterbrain
    if not bb:
        responder.post_message(channel, "BetterBrain repo isn't configured.", thread_ts=thread_ts)
        return

    scaffold = Path(str(bb.repo_path)) / "knowledge-corpus" / "generated" / "article-drafts" / f"{slug}.scaffold.md"
    if not scaffold.exists():
        responder.post_message(
            channel,
            f"No scaffold `{slug}.scaffold.md` found — generate it with "
            f"`@BetterBrain draft article: <topic>` (which now writes the draft too).",
            thread_ts=thread_ts,
        )
        return

    responder.post_message(
        channel, f"✍️ Re-authoring `{slug}` from its scaffold — ~3-10 min…", thread_ts=thread_ts,
    )
    _author_and_deliver(responder, channel, thread_ts, bb, slug)


def process_mention_question(event: Dict[str, Any], config: Config):
    """Answer a direct @mention in a product-knowledge channel via the cascade.

    Unlike process_guru_decline, this does not wait for Guru to fail: somebody
    asked BetterBrain directly, so it answers directly. That makes it the only
    trigger in these channels that survives Guru being removed.

    Because the mention is an explicit request, the answer always goes to the
    thread the person asked in, regardless of the channel's cascade_reply
    setting -- DMing a third party the answer to someone else's direct question
    would be a strange thing to do.
    """
    channel = event["channel"]
    reply_ts = event.get("thread_ts") or event.get("ts")

    # Strip the leading @mention so the cascade sees the question, not the ping.
    question = re.sub(r"<@[A-Z0-9]+>", " ", event.get("text", "")).strip()
    if len(question) < 8:
        SlackResponder(config.slack.bot_token).post_message(
            channel,
            "Ask me a product question and I'll check BetterBrain — e.g. "
            "`@BetterBrain what's the max number of additional contributors?`",
            thread_ts=reply_ts,
        )
        return

    logger.info(f"Direct mention in {channel}; running BetterBrain cascade for: {question[:120]}")
    answer, status = run_betterbrain_cascade(question, config.betterbrain)
    log_gap(question, answer, channel, config.betterbrain, status=status)

    responder = SlackResponder(config.slack.bot_token)
    if answer:
        responder.post_message(channel, answer, thread_ts=reply_ts)
        logger.info(f"Answered direct mention in {channel}/{reply_ts}")
    else:
        # They asked directly, so they get a straight answer either way. Saying
        # nothing to someone who @mentioned you is the worst option.
        responder.post_message(
            channel,
            "I don't have a confident answer for that in BetterBrain, and my "
            "escalation didn't turn one up either. Logged it as a gap.",
            thread_ts=reply_ts,
        )
        logger.info(f"Direct mention in {channel} produced no answer (status={status})")


def process_guru_decline(event: Dict[str, Any], config: Config):
    """Handle a Guru decline: run the BetterBrain cascade and DM the result to a
    human reviewer. Deliberately does NOT reply in the original thread -- nothing
    posts anywhere visible to the channel without a person looking at it first."""
    channel = event["channel"]
    parent_ts = event.get("thread_ts")

    channel_config = config.channels.get(channel)
    if not channel_config or not channel_config.notify_user_id:
        logger.error(f"guruDecline fired for {channel} but no notifyUserId is configured")
        return

    if not parent_ts:
        logger.info("Guru decline message has no parent thread -- nothing to answer, skipping")
        return

    question = fetch_parent_message_text(config.slack.bot_token, channel, parent_ts)
    if not question:
        logger.warning(f"Could not fetch parent question for {channel}/{parent_ts}")
        return

    # Guru also replies to our own bot commands (draft-article / write-it requests);
    # those aren't genuine unanswered user questions, so don't cascade on them.
    if parse_article_command(question) or parse_author_command(question):
        logger.info(f"Guru replied to a bot command in {channel}; not a real gap, skipping cascade")
        return

    # Guru's reply reached us; let a model decide if it actually declined/hedged
    # (BetterBrain steps in) or answered confidently (stay silent).
    if not guru_response_is_decline(question, event.get("text", ""), config.betterbrain):
        logger.info(f"Guru answered confidently in {channel}; staying silent (no cascade)")
        return

    logger.info(f"Guru declined in {channel}; running BetterBrain cascade for: {question[:120]}")
    answer, status = run_betterbrain_cascade(question, config.betterbrain)

    # Log the gap regardless of whether the cascade found an answer -- every Guru
    # miss becomes a reviewable PKR-draft candidate (skill Step 4). Human still
    # runs Step 5 interactively to promote any of these into an actual PKR.
    log_gap(question, answer, channel, config.betterbrain, status=status)

    responder = SlackResponder(config.slack.bot_token)
    orig_link = (
        f"<https://betterworks.slack.com/archives/{channel}/"
        f"p{parent_ts.replace('.', '')}|Original question>"
    )

    if not answer:
        # No answer to forward, but don't stay silent -- a miss the reviewer never
        # hears about is a miss twice (this is exactly how the 2026-07-20 UKG-sync
        # timeout went unnoticed). Send a short heads-up so timeouts and dry runs
        # are visible; the full entry is already in betterbrain-ask-gaps.jsonl.
        if status == "timeout":
            note = (
                f"⏳ *Guru declined and my BetterBrain cascade timed out before finishing.*\n"
                f"{orig_link}\n\n"
                f"No answer captured -- logged as a gap. Worth a manual look or a re-run "
                f"(the cascade may just need more time than the current limit)."
            )
        elif status in ("error", "not_found"):
            note = (
                f":warning: *Guru declined, but my BetterBrain cascade failed to run "
                f"({status}).*\n{orig_link}\n\nLogged as a gap; the bot may need attention."
            )
        else:
            note = (
                f"🧠 *Guru declined; I ran BetterBrain's cascade but it came up empty.*\n"
                f"{orig_link}\n\n"
                f"No corpus/escalation answer found -- logged as a gap for review."
            )
        responder.post_message(channel_config.notify_user_id, note)
        logger.info(
            f"DMed {channel_config.notify_user_id} a no-answer heads-up "
            f"(status={status}) for: {question[:80]}"
        )
        return

    if channel_config.cascade_reply == "thread":
        # Answer the person who actually asked, in their own thread. This is the
        # point of the whole system: the asker gets unblocked without a human
        # relay, and the area SME -- who is already in this channel and already
        # reading this thread -- can correct it in place. A DM to a reviewer
        # cannot produce either of those.
        responder.post_message(channel, answer, thread_ts=parent_ts)
        logger.info(
            f"Posted cascade answer in-thread to {channel}/{parent_ts} for: {question[:80]}"
        )
        # Keep the reviewer informed without making them the delivery path.
        responder.post_message(
            channel_config.notify_user_id,
            f"🧠 *Posted a BetterBrain cascade answer in-thread.*\n{orig_link}\n\n{answer}",
        )
        return

    dm_text = (
        f"🧠 *Guru couldn't answer this one, so I ran BetterBrain's cascade:*\n"
        f"{orig_link}\n\n"
        f"{answer}\n\n"
        f"_Not posted anywhere -- forward or reply yourself if it's worth sharing._"
    )
    responder.post_message(channel_config.notify_user_id, dm_text)
    logger.info(f"DMed {channel_config.notify_user_id} with cascade result for: {question[:80]}")
