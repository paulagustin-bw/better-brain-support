#!/usr/bin/env python3
"""
Local dev server for testing BetterSupport end-to-end with ngrok.

Usage:
  SLACK_SIGNING_SECRET=1234 \
  BETTERCODE_SLACK_BOT_TOKEN=xoxb1234 \
  BETTERCODE_LLM_OPENAI_API_KEY=1234 \
  python local-server.py

Then:
  1. In another terminal: ngrok http 3000
  2. Copy ngrok URL (e.g., https://abc123.ngrok.io)
  3. Go to Slack app settings → Event Subscriptions → Request URL
  4. Paste ngrok URL + /slack/events (e.g., https://abc123.ngrok.io/slack/events)
  5. Subscribe to: app_mention, message.channels
  6. Reinstall app in your workspace
  7. In Slack, @mention BetterCode in a channel and ask a question
"""

import json
import logging
import os
import sys
from pathlib import Path

from flask import Flask, request, jsonify

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.slack.handler import handle

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    """Handle Slack events."""
    try:
        # Get raw body for signature verification
        body = request.get_data(as_text=True)
        
        # Parse JSON
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in request body")
            return jsonify({"error": "Invalid JSON"}), 400
        
        # Get headers
        headers = dict(request.headers)
        
        # Log request
        logger.info(f"Received event: {event.get('type', 'unknown')}")
        
        # Handle URL verification challenge
        if event.get("type") == "url_verification":
            logger.info("Slack URL verification challenge")
            return jsonify({"challenge": event.get("challenge")}), 200
        
        # Pass to handler
        result = handle(event, headers, body)
        
        status_code = result.get("statusCode", 200)
        response_body = json.loads(result.get("body", "{}"))
        
        return jsonify(response_body), status_code
    
    except Exception as e:
        logger.error(f"Error handling request: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({"ok": True, "service": "BetterCode"}), 200


@app.route('/', methods=['GET'])
def index():
    """Index endpoint."""
    return jsonify({
        "ok": True,
        "service": "BetterSupport",
        "endpoints": {
            "/slack/events": "POST - Slack event handler",
            "/health": "GET - Health check"
        }
    }), 200


def main():
    """Run the local development server."""
    port = 3000
    
    print("\n" + "="*60)
    print("🚀 BetterSupport Local Server")
    print("="*60)
    print(f"\n📡 Server listening on http://localhost:{port}\n")
    print("📋 Setup Instructions:")
    print("   1. In another terminal, run: ngrok http 3000")
    print("   2. Copy the ngrok URL (e.g., https://abc123.ngrok.io)")
    print("   3. Go to Slack App Settings → Event Subscriptions")
    print("   4. Set Request URL to: https://abc123.ngrok.io/slack/events")
    print("   5. Subscribe to bot events: app_mention, message.channels")
    print("   6. Reinstall the app in your workspace")
    print("\n💬 Usage:")
    print("   In Slack, @mention BetterSupport and ask a question\n")
    print("="*60 + "\n")
    
    # Serve via waitress (production WSGI): no debug reloader (which caused stale
    # code reloads) and no Werkzeug debugger (a remote code-exec surface on a
    # tunnel-exposed endpoint). threads gives real concurrency for slow requests
    # (the multi-minute /author-article pass). Falls back to a non-debug Flask
    # server if waitress isn't installed.
    # Bind loopback only. cloudflared runs on this same host and connects over
    # localhost, so listening on 0.0.0.0 bought nothing and exposed the endpoint
    # to every device on the LAN -- a path that bypasses the tunnel entirely.
    # The Slack signature check is the whole security boundary here, so keep the
    # number of ways to reach it as small as possible. Override with BIND_HOST
    # if this ever runs somewhere the tunnel is not host-local (e.g. a container
    # where the proxy is a separate network namespace).
    host = os.getenv("BIND_HOST", "127.0.0.1")
    try:
        from waitress import serve
        logger.info(f"Serving on {host}:{port} via waitress (threads=8)")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        logger.warning("waitress not installed; using Flask server with debug OFF")
        app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
