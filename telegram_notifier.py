"""
Telegram side of the Moodle notifier: message formatting, delivery, and reading
messages sent to the bot.

This module knows nothing about Moodle. If the bot token or chat id is missing,
notify() prints messages to the terminal instead, which is handy for dry runs.
"""
import html
import json
import logging
import re
import time

import httpx

log = logging.getLogger("moodle-bot.telegram")

API = "https://api.telegram.org"
MAX_LEN = 4000  # Telegram's hard limit is 4096 characters


def fmt(title, body="", url=""):
    """Build a Telegram HTML message: bold title, escaped body, plain URL.
    All text is escaped, so Moodle content can't break the formatting."""
    body = (body or "").strip()
    if len(body) > 700:
        body = body[:700].rstrip() + "…"
    parts = [f"<b>{html.escape(title)}</b>"]
    if body:
        parts.append(html.escape(body))
    if url:
        parts.append(html.escape(url))
    return "\n".join(parts)


def send_message(token, chat_id, text):
    """Send one message. True if delivered; False means "try again later"."""
    for attempt in range(2):
        try:
            r = httpx.post(
                f"{API}/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text[:MAX_LEN],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # rate limited: wait as long as Telegram asks
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(wait, 30))
                continue
            log.error("Telegram error %s: %s", r.status_code, r.text[:200])
            return False
        except httpx.HTTPError as e:
            log.error("Telegram network error: %s", e)
            time.sleep(2)
    return False


def make_notifier(token, chat_id):
    """Returns notify(text) -> bool. Prints instead when Telegram isn't configured."""

    def notify(text):
        if not token or not chat_id:
            plain = re.sub(r"</?b>", "", html.unescape(text))
            print("\n--- (Telegram not configured, printing) ---\n" + plain)
            return True
        return send_message(token, chat_id, text)

    return notify


def get_updates(token, offset=None):
    """New messages sent to the bot (one quick poll, no long-waiting).
    Returns a list, or None on error so the caller doesn't skip anything."""
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        r = httpx.get(f"{API}/bot{token}/getUpdates", params=params, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.error("getUpdates failed: %s", e)
        return None
    if not data.get("ok"):
        log.error("getUpdates refused: %s", str(data)[:200])
        return None
    return data.get("result", [])


def delete_message(token, chat_id, message_id):
    """Delete a message. Bots can delete incoming messages in private chats (and in
    groups where they are admins), for 48 hours after they were sent."""
    try:
        r = httpx.post(
            f"{API}/bot{token}/deleteMessage",
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=20,
        )
        return r.status_code == 200
    except httpx.HTTPError as e:
        log.error("deleteMessage failed: %s", e)
        return False


def get_chat_id(token):
    """Print the chat ids your bot has seen (message the bot first)."""
    r = httpx.get(f"{API}/bot{token}/getUpdates", timeout=20)
    data = r.json()
    seen = set()
    for u in data.get("result", []):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat and chat["id"] not in seen:
            seen.add(chat["id"])
            print(f"chat id: {chat['id']}  ({chat.get('first_name') or chat.get('title')})")
    if not seen:
        print("No messages found. Send any message to your bot in Telegram first, then re-run.")