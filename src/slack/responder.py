"""Slack message responder."""

import logging
from typing import Dict, Any, Optional
import requests

from src.slack.markdown_formatter import markdown_to_slack

logger = logging.getLogger(__name__)


class SlackResponder:
    """Handle Slack message responses."""
    
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = "https://slack.com/api"
    
    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        blocks: Optional[list] = None,
        format_markdown: bool = True
    ) -> Dict[str, Any]:
        """
        Post a message to Slack.
        
        Args:
            channel: Slack channel ID
            text: Message text (Markdown will be converted to Slack format)
            thread_ts: Thread timestamp to reply in thread
            blocks: Optional Slack blocks
            format_markdown: If True, convert Markdown to Slack format
        
        Returns:
            API response
        """
        url = f"{self.base_url}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json"
        }
        
        # Format markdown for Slack
        formatted_text = markdown_to_slack(text) if format_markdown else text
        
        payload = {
            "channel": channel,
            "text": formatted_text[:4000],  # Slack API limit for message text
        }
        
        if thread_ts:
            payload["thread_ts"] = thread_ts
        
        if blocks:
            payload["blocks"] = blocks
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("ok"):
                logger.error(f"Slack API error: {data.get('error')}")
            
            return data
        except Exception as e:
            logger.error(f"Failed to post message: {e}")
            return {"ok": False, "error": str(e)}
    
    def post_interim_message(
        self,
        channel: str,
        thread_ts: Optional[str] = None
    ) -> Dict[str, Any]:
        """Post an interim 'thinking' message."""
        return self.post_message(
            channel,
            "🔍 Investigating your question...",
            thread_ts=thread_ts,
            format_markdown=False  # Simple message, no formatting needed
        )
    
    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: Optional[list] = None,
        format_markdown: bool = True
    ) -> Dict[str, Any]:
        """
        Update an existing message.
        
        Args:
            channel: Slack channel ID
            ts: Message timestamp
            text: New message text (Markdown will be converted to Slack format)
            blocks: Optional Slack blocks
            format_markdown: If True, convert Markdown to Slack format
        
        Returns:
            API response
        """
        url = f"{self.base_url}/chat.update"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json"
        }
        
        # Format markdown for Slack
        formatted_text = markdown_to_slack(text) if format_markdown else text
        
        payload = {
            "channel": channel,
            "ts": ts,
            "text": formatted_text,
        }
        
        if blocks:
            payload["blocks"] = blocks
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("ok"):
                logger.error(f"Slack API error: {data.get('error')}")
            
            return data
        except Exception as e:
            logger.error(f"Failed to update message: {e}")
            return {"ok": False, "error": str(e)}

    def upload_file(self, channel: str, content: str, filename: str,
                    title: Optional[str] = None, thread_ts: Optional[str] = None) -> bool:
        """Upload a text file to a channel/thread via Slack's external-upload flow
        (getUploadURLExternal -> PUT -> completeUploadExternal). Requires the
        files:write scope. Returns True on success, False otherwise so the caller
        can fall back to posting the content as text."""
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        data = content.encode("utf-8")
        try:
            r1 = requests.get(
                f"{self.base_url}/files.getUploadURLExternal", headers=headers,
                params={"filename": filename, "length": len(data)}, timeout=10,
            ).json()
            if not r1.get("ok"):
                logger.warning(f"files.getUploadURLExternal failed: {r1.get('error')}")
                return False
            put = requests.post(r1["upload_url"], data=data, timeout=30)
            if put.status_code != 200:
                logger.warning(f"file upload PUT failed: {put.status_code}")
                return False
            payload: Dict[str, Any] = {
                "files": [{"id": r1["file_id"], "title": title or filename}],
                "channel_id": channel,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts
            r3 = requests.post(
                f"{self.base_url}/files.completeUploadExternal",
                headers={**headers, "Content-Type": "application/json"},
                json=payload, timeout=10,
            ).json()
            if not r3.get("ok"):
                logger.warning(f"files.completeUploadExternal failed: {r3.get('error')}")
                return False
            return True
        except requests.RequestException as e:
            logger.warning(f"file upload error: {e}")
            return False
