"""Slack event handler."""

import json
import logging
import subprocess
import time
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


# Guru's Slack bot user ID -- confirmed live 2026-07-18 from a real decline message
# in #product. Bot messages from any OTHER bot (including our own replies, which
# would otherwise create a self-trigger loop) are still dropped below.
GURU_BOT_USER_ID = "U028VSYP9CZ"

# Both phrases must appear (case-insensitive) for a message to count as a Guru
# decline. Matched against real Guru bot output, not guessed.
GURU_DECLINE_PHRASES = ("don't have a verified answer", "look here instead")


def looks_like_guru_decline(text: str) -> bool:
    lowered = (text or "").lower()
    return all(phrase in lowered for phrase in GURU_DECLINE_PHRASES)


def normalize_event(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize Slack event to a consistent format."""
    event = body.get("event", {})
    event_type = event.get("type")

    # Bot messages are dropped UNLESS they're from an explicitly allow-listed bot
    # (currently just Guru) -- this lets should_process_event() react to Guru's
    # decline messages specifically, without opening the door to every bot in the
    # workspace (including our own replies, which would otherwise self-trigger).
    is_bot_message = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
    if is_bot_message and event.get("user") != GURU_BOT_USER_ID:
        return None

    # Ignore message edits/deletes if configured
    if event.get("subtype") in ("message_changed", "message_deleted"):
        return None

    # Handle app_mention and message events
    if event_type in ("app_mention", "message"):
        return {
            "type": event_type,
            "channel": event.get("channel"),
            "user": event.get("user"),
            "text": event.get("text", ""),
            "ts": event.get("ts"),
            "thread_ts": event.get("thread_ts"),
            "is_bot_message": is_bot_message,
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
        # normalize_event() already dropped every bot message except Guru's, so
        # the only trigger a bot message can match is guruDecline.
        return bool(triggers.get("guruDecline", False)) and looks_like_guru_decline(event.get("text", ""))

    if event_type == "app_mention" and triggers.get("appMention", True):
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
    
    # Check for duplicate events
    event_id = f"{normalized_event.get('channel')}_{normalized_event.get('ts')}"
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
        else:
            process_question(normalized_event, config)
    except Exception as e:
        logger.error(f"Failed to process question: {e}")
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

    logger.info(f"Guru declined in {channel}; running BetterBrain cascade for: {question[:120]}")
    answer = run_betterbrain_cascade(question, config.betterbrain)
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
