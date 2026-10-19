"""
Telegram side of the Moodle notifier: message formatting and delivery.

This module knows nothing about Moodle. It takes a bot token and a chat id and
gives back a `notify(text)` function. If the token or chat id is missing it
prints the message to the terminal instead, which is handy for dry runs.
"""
import html
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


def make_notifier(token, chat_id):
    """Returns notify(text) -> bool (True if delivered, or printed when
    Telegram is not configured). A False result means "try again later"."""

    def notify(text):
        if not token or not chat_id:
            plain = re.sub(r"</?b>", "", html.unescape(text))
            print("\n--- (Telegram not configured, printing) ---\n" + plain)
            return True
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

    return notify


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