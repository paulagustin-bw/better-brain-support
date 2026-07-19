"""Slack event handler."""

import fcntl
import json
import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Set

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

    # An article command may arrive as a plain `message` (when only
    # message.channels is subscribed, not app_mention) -- accept it either way as
    # long as the channel has mentions enabled.
    if triggers.get("appMention", False) and parse_article_command(event.get("text", "")):
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
        logger.info("Event ignored (channel not configured or triggers not met)")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
    
    # Process event asynchronously (in production, this would be queued)
    try:
        if normalized_event.get("is_bot_message"):
            process_guru_decline(normalized_event, config)
        elif parse_article_command(normalized_event.get("text", "")):
            process_article_request(normalized_event, config)
        else:
            ch_cfg = config.channels.get(normalized_event.get("channel"))
            if ch_cfg and getattr(ch_cfg, "project", None):
                process_question(normalized_event, config)
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


def run_betterbrain_cascade(question: str, betterbrain_config) -> Optional[str]:
    """Run the /betterbrain-ask skill headlessly against the given question.

    This is a read-only invocation by design (see .claude/settings.json in the
    BetterBrain repo): search + escalation across BetterBrain/Confluence/Aha/
    GitHub/Slack works headlessly, but gap-logging and PKR-drafting (steps 4-5
    of the skill) require interactive approval and will silently no-op here --
    that's intentional for v0, not a bug. Those stay a deliberate follow-up
    action in an interactive session, not something this daemon does unattended.
    """
    if not betterbrain_config:
        logger.error("No betterbrain config set -- cannot run cascade")
        return None

    try:
        result = subprocess.run(
            ["claude", "-p", f"/betterbrain-ask {question}"],
            cwd=str(betterbrain_config.repo_path),
            capture_output=True,
            text=True,
            timeout=betterbrain_config.cli_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.error("betterbrain-ask cascade timed out")
        return None
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH -- is Claude Code installed on this machine?")
        return None

    if result.returncode != 0:
        logger.error(f"betterbrain-ask exited {result.returncode}: {result.stderr[:500]}")
        return None

    answer = (result.stdout or "").strip()
    return answer or None


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
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("decline classifier timed out; falling back to keyword match")
        return looks_like_guru_decline(guru_text)
    except FileNotFoundError:
        logger.error("`claude` CLI not found for decline classifier; keyword fallback")
        return looks_like_guru_decline(guru_text)
    if result.returncode != 0:
        logger.warning(f"decline classifier exited {result.returncode}; keyword fallback")
        return looks_like_guru_decline(guru_text)

    verdict = (result.stdout or "").strip().upper()
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


def log_gap(question: str, answer: Optional[str], channel: str, betterbrain_config) -> None:
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
        entry = {
            "question": question,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "escalated_to": [],  # bot can't see which live sources the cascade hit
            "found_elsewhere": bool(answer),
            "summary": (answer or "Cascade produced no answer.")[:1500],
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
        logger.info(f"Logged gap ({'answer' if answer else 'no-answer'}) for: {question[:80]}")
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
        f"📝 Drafting a *{cmd['tmpl']}* scaffold for *{cmd['topic']}*"
        f"{' (area: ' + cmd['area'] + ')' if cmd['area'] else ''} — matching + reranking the PKR corpus, ~40s…",
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

    m = re.search(r"^\s*([a-z0-9][a-z0-9-]+):\s+closed=", result.stdout, re.MULTILINE)
    if not m:
        responder.post_message(
            channel, f"Generated, but couldn't locate the output:\n```{result.stdout[-600:]}```",
            thread_ts=thread_ts,
        )
        return

    slug = m.group(1)
    summary = m.group(0).strip()
    out_dir = Path(str(bb.repo_path)) / "knowledge-corpus" / "generated" / "article-drafts"
    scaffold_f = out_dir / f"{slug}.scaffold.md"
    gaps_f = out_dir / f"{slug}.gaps.md"

    responder.post_message(channel, f"✅ *Scaffold ready:* `{slug}`\n`{summary}`", thread_ts=thread_ts)
    if gaps_f.exists():
        responder.post_message(
            channel, "*Gap manifest — what's covered vs. missing:*\n```"
            + gaps_f.read_text()[:1800] + "```",
            thread_ts=thread_ts,
        )
    if scaffold_f.exists():
        responder.post_message(
            channel, "*Scaffold — fill the [AUTHOR] stubs, then run `/author-article`:*\n```"
            + scaffold_f.read_text()[:2600] + "```",
            thread_ts=thread_ts,
        )
    responder.post_message(
        channel,
        "_Draft only — not published anywhere. Review, author the prose, and publish through the "
        "normal flow. Full files on the mini at `knowledge-corpus/generated/article-drafts/`._",
        thread_ts=thread_ts,
    )
    logger.info(f"Generated article scaffold '{slug}' for topic: {cmd['topic'][:80]}")


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

    # Guru's reply reached us; let a model decide if it actually declined/hedged
    # (BetterBrain steps in) or answered confidently (stay silent).
    if not guru_response_is_decline(question, event.get("text", ""), config.betterbrain):
        logger.info(f"Guru answered confidently in {channel}; staying silent (no cascade)")
        return

    logger.info(f"Guru declined in {channel}; running BetterBrain cascade for: {question[:120]}")
    answer = run_betterbrain_cascade(question, config.betterbrain)

    # Log the gap regardless of whether the cascade found an answer -- every Guru
    # miss becomes a reviewable PKR-draft candidate (skill Step 4). Human still
    # runs Step 5 interactively to promote any of these into an actual PKR.
    log_gap(question, answer, channel, config.betterbrain)

    if not answer:
        logger.info("Cascade produced nothing to report; staying silent (not DMing a non-answer)")
        return

    responder = SlackResponder(config.slack.bot_token)
    dm_text = (
        f"🧠 *Guru couldn't answer this one, so I ran BetterBrain's cascade:*\n"
        f"<https://betterworks.slack.com/archives/{channel}/p{parent_ts.replace('.', '')}|Original question>\n\n"
        f"{answer}\n\n"
        f"_Not posted anywhere -- forward or reply yourself if it's worth sharing._"
    )
    responder.post_message(channel_config.notify_user_id, dm_text)
    logger.info(f"DMed {channel_config.notify_user_id} with cascade result for: {question[:80]}")
