"""
Moodle -> Telegram notifier (read-only).

Watches, via Moodle's mobile web service:
  - deadlines (assignments/quizzes) + reminders before they are due
  - announcements (news forums of every course)
  - new/updated course materials
  - the Moodle notification bell

Usage:
  python moodle_bot.py --get-chat-id     # find your Telegram chat id
  python moodle_bot.py --test-telegram   # send a test message
  python moodle_bot.py --once            # one check (first run seeds silently)
  python moodle_bot.py                   # loop forever, every POLL_MINUTES
  python moodle_bot.py --gen-key         # make a TOKEN_ENCRYPTION_KEY (for /token renewal)

Config comes from environment variables or a .env file (see .env.example).

Layout: this file holds the Moodle logic (API client, state, checks, scheduling).
Telegram delivery and message formatting live in telegram_notifier.py.
"""
import argparse
import base64
import hashlib
import html
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx

from telegram_notifier import (
    delete_message,
    fmt,
    fmt_card,
    get_chat_id,
    get_updates,
    make_notifier,
    send_message,
)

log = logging.getLogger("moodle-bot")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:  # a blank value means "not set"
            os.environ.setdefault(k.strip(), v)


def parse_token(value):
    """Accept a raw 32-char token or the moodlemobile://token=BASE64 string."""
    value = unquote(value.strip())
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return value
    if "token=" in value:
        value = value.split("token=", 1)[1]
    value += "=" * (-len(value) % 4)
    parts = base64.b64decode(value).decode("utf-8", errors="replace").split(":::")
    if len(parts) < 2:
        raise ValueError("MOODLE_TOKEN is neither a raw token nor an SSO token string")
    return parts[1]


def env(name, default=""):
    """Read a setting. Empty or blank counts as "not set": GitHub Actions passes
    undefined secrets and variables to the program as empty strings."""
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_int(name, default):
    value = env(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None


def env_int_list(name, default):
    value = env(name, default)
    try:
        return sorted(int(x) for x in value.split(",") if x.strip())
    except ValueError:
        raise ValueError(f"{name} must be comma-separated whole numbers, got {value!r}") from None


class Cfg:
    def __init__(self):
        self.base = env("MOODLE_URL").rstrip("/")
        if not self.base:
            raise KeyError("MOODLE_URL")
        raw_token = env("MOODLE_TOKEN")
        if not raw_token:
            raise KeyError("MOODLE_TOKEN")
        self.token = parse_token(raw_token)
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.chat_id = env("TELEGRAM_CHAT_ID")
        # Only this Telegram user may send commands (like /token) to the bot.
        # Defaults to TELEGRAM_CHAT_ID when that is a private chat (a private chat id
        # equals the user id). For a group chat you must set TELEGRAM_ADMIN_ID.
        raw_admin = env("TELEGRAM_ADMIN_ID")
        if raw_admin.lstrip("-").isdigit():
            self.tg_admin_id = int(raw_admin)
        elif self.chat_id.isdigit():
            self.tg_admin_id = int(self.chat_id)
        else:
            self.tg_admin_id = None
        # Key used to encrypt a token received over Telegram before it is stored.
        self.token_key = env("TOKEN_ENCRYPTION_KEY")
        self.poll_minutes = env_int("POLL_MINUTES", 15)
        self.db_path = env("DB_PATH", "moodle_bot.db")
        self.reminder_hours = env_int_list("REMINDER_HOURS", "24,3")
        # Assignment reminders, in hours before the deadline. Sent regardless of whether
        # you have submitted: the bot never checks submission status.
        self.assign_reminder_hours = env_int_list("ASSIGN_REMINDER_HOURS", "48,24")
        self.full_check_hours = env_int("FULL_CHECK_HOURS", 3)
        self.skip_courses = set(env_int_list("SKIP_COURSE_IDS", ""))
        # India Standard Time by default (UTC+5:30)
        self.tz = timezone(timedelta(minutes=env_int("TZ_OFFSET_MINUTES", 330)))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def strip_html(s):
    s = re.sub(r"(?i)<(br|/p|/div|/li|/h\d)[^>]*>", "\n", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def short(s, n=60):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(ts, tz, now):
    """Sat, 16 Jan 2027 (tomorrow): weekday, month as a word, year, plus a relative hint.
    Fixed English names, so the output doesn't depend on the machine's locale."""
    dt = datetime.fromtimestamp(ts, tz)
    days = (dt.date() - datetime.fromtimestamp(now, tz).date()).days
    rel = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or (
        f"in {days} days" if days > 1 else f"{-days} days ago"
    )
    return f"{DAYS[dt.weekday()]}, {dt.day} {MONTHS[dt.month - 1]} {dt.year} ({rel})"


def fmt_clock(ts, tz):
    """7:30 PM"""
    return datetime.fromtimestamp(ts, tz).strftime("%I:%M %p").lstrip("0")


def fmt_span(seconds):
    """1 day 6 h / 5 h 20 min / 2 h / 40 min"""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")
    if hours:
        return f"{hours} h" + (f" {mins} min" if mins else "")
    return f"{max(mins, 1)} min"


def deadline_rows(label, name, course, due, tz, now):
    """The rows shared by every deadline message (assignments, quizzes, ...)."""
    return [
        ("📝", label, name),
        ("📚", "Course", course),
        ("📅", "Deadline", fmt_date(due, tz, now) if due else "not set", True),
        ("🕒", "Due at", fmt_clock(due, tz) if due else "", True),
        ("⏳", "Time left", time_left(due, now)),
    ]


def time_left(due, now):
    if not due:
        return "no deadline set"
    delta = due - now
    if delta <= 0:
        return f"OVERDUE by {fmt_span(-delta)}"
    return fmt_span(delta)


# --------------------------------------------------------------------------
# Moodle client
# --------------------------------------------------------------------------
class MoodleError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


class TokenExpired(Exception):
    pass


class Moodle:
    def __init__(self, base, token):
        self.base = base
        self.token = token
        self.http = httpx.Client(
            timeout=30, follow_redirects=True, headers={"User-Agent": "moodle-notifier/1.0"}
        )

    def call(self, function, **params):
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        data.update(params)
        last = None
        for attempt in range(3):
            try:
                r = self.http.post(f"{self.base}/webservice/rest/server.php", data=data)
                r.raise_for_status()
                j = r.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"{function}: network error {last}")
        if isinstance(j, dict) and "exception" in j:
            code = j.get("errorcode", "")
            if code == "invalidtoken":
                raise TokenExpired(j.get("message", "token invalid or expired"))
            raise MoodleError(code, j.get("message", ""))
        return j


# --------------------------------------------------------------------------
# State (SQLite)
# --------------------------------------------------------------------------
class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (fp TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )

    def seen(self, fp):
        return self.c.execute("SELECT 1 FROM seen WHERE fp=?", (fp,)).fetchone() is not None

    def seen_prefix(self, prefix):
        return (
            self.c.execute("SELECT 1 FROM seen WHERE fp LIKE ? LIMIT 1", (prefix + "%",)).fetchone()
            is not None
        )

    def add(self, fp):
        self.c.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (fp, int(time.time())))
        self.c.commit()

    def get(self, k, default=None):
        row = self.c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
        self.c.commit()


def process(db, scope, items, notify):
    """items: [(fingerprint, message)]. The first time a scope is seen it is
    stored silently (so you don't get hundreds of alerts for old content).
    A fingerprint is only stored after delivery succeeds, so failures retry."""
    seeded = db.get(f"seeded:{scope}") == "1"
    for fp, msg in items:
        if db.seen(fp):
            continue
        if seeded:
            text = msg() if callable(msg) else msg
            if not notify(text):
                continue
        db.add(fp)
    if not seeded:
        db.set(f"seeded:{scope}", "1")
        log.info("Seeded %s with %d existing items", scope, len(items))


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_deadlines(m, db, cfg, notify, now, skip_assign=False):
    j = m.call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=now - 3600,
        timesortto=now + 90 * 86400,
        limitnum=50,  # Moodle rejects anything above 50
    )
    events = [
        e
        for e in j.get("events", [])
        if (e.get("action") or {}).get("actionable", True)
        # assignments get their own richer handling in check_assignments()
        and not (skip_assign and e.get("modulename") == "assign")
    ]

    def card(e, title):
        due = e.get("timesort") or e.get("timestart") or 0
        course = short(
            (e.get("course") or {}).get("shortname") or (e.get("course") or {}).get("fullname", "")
        )
        rows = deadline_rows("Event", strip_html(e.get("name", "")), course, due, cfg.tz, time.time())
        return fmt_card(title, rows, e.get("url", ""))

    items = []
    for e in events:
        fp = f"event:{e['id']}:{e.get('timemodified', 0)}"
        items.append((fp, lambda e=e: card(e, "📅 New / changed deadline")))
    process(db, "events", items, notify)

    # Reminders: only the tightest applicable window fires, larger ones are marked done.
    for e in events:
        due = e.get("timesort") or e.get("timestart") or 0
        hours_left = (due - now) / 3600
        fitting = [h for h in cfg.reminder_hours if 0 < hours_left <= h]
        if not fitting:
            continue
        smallest = min(fitting)
        for h in fitting:
            fp = f"remind:{e['id']}:{due}:{h}"
            if db.seen(fp):
                continue
            if h == smallest and not notify(card(e, f"⏰ Due in {fmt_span(due - now)}")):
                continue
            db.add(fp)


def assignment_message(cfg, a, cname, title):
    rows = deadline_rows(
        "Assignment", strip_html(a.get("name", "")), cname, a.get("duedate") or 0, cfg.tz, time.time()
    )
    return fmt_card(title, rows, f"{cfg.base}/mod/assign/view.php?id={a.get('cmid', '')}")


def check_assignments(m, db, cfg, notify, courses, now):
    if not courses:
        return
    params = {f"courseids[{i}]": c["id"] for i, c in enumerate(courses)}
    data = m.call("mod_assign_get_assignments", **params)
    assigns = []
    for co in data.get("courses", []):
        cname = short(co.get("shortname") or co.get("fullname", ""))
        for a in co.get("assignments", []):
            assigns.append((cname, a))

    # 1) new assignments and changed deadlines
    items = []
    for cname, a in assigns:
        due = a.get("duedate") or 0
        fp = f"assign:{a['id']}:{due}"
        title = (
            "🔁 Assignment deadline changed"
            if db.seen_prefix(f"assign:{a['id']}:")
            else "📝 New assignment"
        )
        items.append((fp, assignment_message(cfg, a, cname, title)))
    process(db, "assignments", items, notify)

    # 2) deadline reminders (default: 2 days and 1 day before). They are sent whether or
    # not you have submitted: the bot never looks at submission status.
    # Only the tightest applicable window fires; larger ones are marked done.
    windows = cfg.assign_reminder_hours
    max_h = max(windows)
    for cname, a in assigns:
        due = a.get("duedate") or 0
        hours_left = (due - now) / 3600
        if not due or hours_left <= 0 or hours_left > max_h:
            continue
        fitting = [h for h in windows if hours_left <= h]
        fps = {h: f"aremind:{a['id']}:{due}:{h}" for h in fitting}
        if db.seen(fps[min(fitting)]):
            continue  # already reminded for this window
        title = f"⏰ Assignment due in {fmt_span(due - now)}"
        if not notify(assignment_message(cfg, a, cname, title)):
            continue  # retry next run
        for fp in fps.values():
            db.add(fp)


def check_notifications(m, db, cfg, notify, userid):
    j = m.call(
        "message_popup_get_popup_notifications",
        useridto=userid,
        newestfirst=1,
        limit=30,
        offset=0,
    )
    items = []
    for n in j.get("notifications", []):
        subject = n.get("subject") or n.get("shortenedsubject", "")
        body = strip_html(n.get("fullmessagehtml") or n.get("fullmessage") or "")
        items.append((f"notif:{n['id']}", fmt("🔔 " + subject, body, n.get("contexturl", ""))))
    process(db, "notifications", list(reversed(items)), notify)  # oldest first


def check_announcements(m, db, cfg, notify, news_forums, course_names):
    for f in news_forums:
        j = m.call("mod_forum_get_forum_discussions", forumid=f["id"])
        course = short(course_names.get(f["course"], ""))
        items = []
        for d in j.get("discussions", []):
            did = d.get("discussion")
            fp = f"announce:{did}:{d.get('timemodified', 0)}"
            body = f"{d.get('name', '')}\n{strip_html(d.get('message', ''))}"
            url = f"{cfg.base}/mod/forum/discuss.php?d={did}"
            items.append((fp, fmt(f"📢 Announcement · {course}", body, url)))
        process(db, f"announce:{f['id']}", list(reversed(items)), notify)
        time.sleep(0.5)


def check_materials(m, db, cfg, notify, courses, now):
    """Cheap check first (get_updates_since); full contents scan when something
    changed, plus a safety-net full scan every FULL_CHECK_HOURS."""
    for c in courses:
        cid = c["id"]
        cname = short(c.get("shortname") or c.get("fullname", ""))
        try:
            last_poll = int(db.get(f"updates_since:{cid}") or 0)
            last_full = int(db.get(f"full_check:{cid}") or 0)
            need = last_poll == 0 or (now - last_full) > cfg.full_check_hours * 3600
            if not need:
                try:
                    upd = m.call("core_course_get_updates_since", courseid=cid, since=last_poll - 60)
                    need = bool(upd.get("instances"))
                except MoodleError:
                    need = True
            if need:
                contents = m.call("core_course_get_contents", courseid=cid)
                items = []
                for sec in contents:
                    for mod in sec.get("modules", []):
                        if mod.get("uservisible") is False:
                            continue
                        if mod.get("modname") == "assign":
                            continue  # handled by check_assignments()
                        files = mod.get("contents") or []
                        tm = max((x.get("timemodified", 0) for x in files), default=0)
                        fp = f"module:{mod['id']}:{tm}"
                        label = "Updated" if db.seen_prefix(f"module:{mod['id']}:") else "New"
                        name = strip_html(mod.get("name", ""))
                        body = f"{name}\nSection: {strip_html(sec.get('name', ''))}"
                        items.append((fp, fmt(f"📚 {label} material · {cname}", body, mod.get("url", ""))))
                process(db, f"materials:{cid}", items, notify)
                db.set(f"full_check:{cid}", now)
            db.set(f"updates_since:{cid}", now)
        except TokenExpired:
            raise
        except Exception as e:
            log.warning("materials check failed for course %s: %s", cid, e)
        time.sleep(0.5)


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------
def wanted_course(c, now, skip):
    if c["id"] in skip:
        return False
    end = c.get("enddate") or 0
    return not (end and end < now - 7 * 86400)


def run_cycle(cfg, m, db, notify, userid):
    now = int(time.time())
    errors = []

    def step(name, fn, *args):
        try:
            fn(*args)
        except TokenExpired:
            raise
        except Exception as e:
            log.exception("%s failed", name)
            errors.append(f"{name}: {e}")

    courses = [
        c
        for c in m.call("core_enrol_get_users_courses", userid=userid)
        if wanted_course(c, now, cfg.skip_courses)
    ]
    course_names = {c["id"]: c.get("shortname") or c.get("fullname", "") for c in courses}

    news_forums = []
    try:
        params = {f"courseids[{i}]": c["id"] for i, c in enumerate(courses)}
        forums = m.call("mod_forum_get_forums_by_courses", **params)
        news_forums = [f for f in forums if f.get("type") == "news"]
    except TokenExpired:
        raise
    except Exception as e:
        errors.append(f"forums list: {e}")

    assign_ok = []

    def do_assignments():
        check_assignments(m, db, cfg, notify, courses, now)
        assign_ok.append(True)

    step("assignments", do_assignments)
    # If the assignment check failed, let the generic deadline check cover them instead.
    step("deadlines", check_deadlines, m, db, cfg, notify, now, bool(assign_ok))
    step("notifications", check_notifications, m, db, cfg, notify, userid)
    step("announcements", check_announcements, m, db, cfg, notify, news_forums, course_names)
    step("materials", check_materials, m, db, cfg, notify, courses, now)

    if db.get("started") is None:
        notify(fmt("✅ Moodle bot is running", f"Tracking {len(courses)} courses, checking every {cfg.poll_minutes} min."))
        db.set("started", now)

    streak = int(db.get("fail_streak") or 0)
    if errors:
        streak += 1
        db.set("fail_streak", streak)
        if streak == 3:
            notify(fmt("⚠️ Moodle bot is failing", "\n".join(errors[:3])))
    else:
        db.set("fail_streak", 0)
    log.info("Cycle done: %d courses, %d errors", len(courses), len(errors))


# --------------------------------------------------------------------------
# Token renewal over Telegram
#
# Send "/token moodlemobile://token=..." to the bot in a PRIVATE chat. The token is
# checked against Moodle, encrypted with TOKEN_ENCRYPTION_KEY, and stored in the state
# database. It is never stored in plain text: on a public repo the Actions cache can be
# read by fork workflows, so only the encrypted form may live there.
# --------------------------------------------------------------------------
def _fernet(cfg):
    if not cfg.token_key:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(cfg.token_key.encode())
    except Exception as e:  # missing package or malformed key
        log.error("Token encryption unavailable: %s", e)
        return None


def env_token_fp(cfg):
    return hashlib.sha256(cfg.token.encode()).hexdigest()[:16]


def save_token(cfg, db, token):
    f = _fernet(cfg)
    if f is None:
        return False
    db.set("token_enc", f.encrypt(token.encode()).decode())
    db.set("token_env_fp", env_token_fp(cfg))
    return True


def effective_token(cfg, db):
    """The token received over Telegram wins, unless the MOODLE_TOKEN secret has been
    changed since it was stored (then the newer secret wins)."""
    enc = db.get("token_enc")
    if not enc or db.get("token_env_fp") != env_token_fp(cfg):
        return cfg.token
    f = _fernet(cfg)
    if f is None:
        return cfg.token
    try:
        return f.decrypt(enc.encode()).decode()
    except Exception:
        log.warning("Stored token could not be decrypted (key changed?); using MOODLE_TOKEN")
        return cfg.token


def handle_telegram_commands(cfg, db):
    """Process /token messages. Runs BEFORE the Moodle checks, so a new token is used
    straight away even when the old one has already expired."""
    if not cfg.tg_token:
        return
    offset = db.get("tg_offset")
    updates = get_updates(cfg.tg_token, int(offset) if offset else None)
    if updates is None:
        return

    def reply(chat_id, title, body=""):
        send_message(cfg.tg_token, chat_id, fmt(title, body))

    for u in updates:
        db.set("tg_offset", u["update_id"] + 1)  # confirm first: never handled twice
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        parts = text.split(None, 1)
        if not parts:
            continue
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/token":
            arg = parts[1].strip() if len(parts) > 1 else ""
        elif text.startswith("moodlemobile://"):
            arg = text  # pasting the bare token string works too
        else:
            continue

        chat = msg.get("chat") or {}
        chat_id, msg_id = chat.get("id"), msg.get("message_id")
        sender = (msg.get("from") or {}).get("id")
        if cfg.tg_admin_id is None or sender != cfg.tg_admin_id:
            continue  # strangers get no reply and no information

        if chat.get("type") != "private":
            delete_message(cfg.tg_token, chat_id, msg_id)
            reply(
                chat_id,
                "⚠️ Send tokens in a private chat with me",
                "Other group members may have seen that token. Reset it in Moodle "
                "(Preferences → User account → Security keys), get a new one, and send "
                "it to me privately.",
            )
            continue

        def finish(title, body=""):
            delete_message(cfg.tg_token, chat_id, msg_id)  # keep the token out of the chat
            reply(chat_id, title, body)

        if _fernet(cfg) is None:
            finish(
                "❌ Can't store a token yet",
                "Set a valid TOKEN_ENCRYPTION_KEY secret first (python moodle_bot.py --gen-key).",
            )
            continue
        try:
            new_token = parse_token(arg)
        except Exception:
            finish("❌ I couldn't read that token", "Send: /token moodlemobile://token=...")
            continue
        try:
            info = Moodle(cfg.base, new_token).call("core_webservice_get_site_info")
        except Exception as e:
            finish("❌ Moodle rejected that token", str(e)[:200])
            continue
        known = db.get("userid")
        if known and str(info.get("userid")) != known:
            finish("❌ That token belongs to a different Moodle account", "Not stored.")
            continue
        if not save_token(cfg, db, new_token):
            finish("❌ Could not store the token")
            continue
        db.set("userid", info.get("userid"))
        log.info("Moodle token replaced via Telegram")
        finish("✅ Moodle token updated", "It will be used from the next check. Your message was deleted.")


def build_redactor(info):
    """Returns redact(text), which removes the account owner's OWN name from outgoing
    messages: the full name (either order) and greetings like "Hi NAME,". Other people's
    names are left alone, including those sharing your first or last name."""
    words = re.findall(r"[^\W\d_]{2,}", info.get("fullname") or "")
    if not words:
        words = re.findall(
            r"[^\W\d_]{2,}", f"{info.get('firstname') or ''} {info.get('lastname') or ''}"
        )
    if not words:
        return lambda text: text
    first, last = re.escape(words[0]), re.escape(words[-1])
    sep = r"[\s,.]+"
    full = "(?:{}|{}|{})".format(
        sep.join(re.escape(w) for w in words),
        sep.join(re.escape(w) for w in reversed(words)),
        f"{first}{sep}{last}|{last}{sep}{first}",
    )
    anyname = f"(?:{full}|{first})"  # a bare surname is not stripped: it could be a teacher's
    greeting = re.compile(
        rf"\b(?:hi|hello|hey|dear)\s+{anyname}\b[\s,:!.\-]*(?P<next>[a-z])?", re.IGNORECASE
    )
    vocative = re.compile(
        rf"(?m)^([ \t]*)(?:{full}|{first})[ \t]*,[ \t]*(?P<next>[a-z])?", re.IGNORECASE
    )
    bracketed = re.compile(rf"[ \t]*[(\[][ \t]*{full}[ \t]*[)\]]", re.IGNORECASE)
    after_sep = re.compile(rf"[ \t]*[-–—|:,][ \t]*\b{full}\b", re.IGNORECASE)
    fullname = re.compile(rf"\b{full}\b", re.IGNORECASE)

    def cap(m):  # keep the sentence tidy: "Hi Ann, your quiz" -> "Your quiz"
        return (m.group("next") or "").upper()

    def redact(text):
        text = greeting.sub(cap, text)
        text = vocative.sub(lambda m: m.group(1) + cap(m), text)
        text = bracketed.sub("", text)  # "Lab 3 (NAME)"   -> "Lab 3"
        text = after_sep.sub("", text)  # "Quiz - NAME"    -> "Quiz"
        text = fullname.sub("", text)
        text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)  # no stray space before punctuation
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text

    return redact


def make_admin_notifier(cfg, notify):
    """Operational alerts (like an expired token) go to the admin's private chat, so
    group members don't see them. A private chat id equals the user's id. Falls back
    to the normal chat if the admin can't be reached (e.g. they never pressed Start)."""

    def notify_admin(text):
        if cfg.tg_token and cfg.tg_admin_id:
            if send_message(cfg.tg_token, cfg.tg_admin_id, text):
                return True
            log.warning("Could not reach the admin privately; using the main chat instead")
        return notify(text)

    return notify_admin


def token_alert(cfg, db, notify, msg):
    last = int(db.get("token_alert_ts") or 0)
    if time.time() - last > 86400:
        notify(
            fmt(
                "🔑 Moodle token expired",
                f"{msg}\n"
                + (
                    "Send me /token moodlemobile://token=... in a private chat, "
                    "or update the MOODLE_TOKEN secret."
                    if cfg.token_key and cfg.tg_admin_id
                    else "Get a new one via the launch.php step and update MOODLE_TOKEN."
                ),
            )
        )
        db.set("token_alert_ts", int(time.time()))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--get-chat-id", action="store_true")
    ap.add_argument("--gen-key", action="store_true", help="print a new TOKEN_ENCRYPTION_KEY")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram's URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env()

    if args.gen_key:
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            sys.exit("Install it first: pip install cryptography")
        print(Fernet.generate_key().decode())
        return

    if args.get_chat_id:
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            sys.exit("Set TELEGRAM_BOT_TOKEN first.")
        get_chat_id(os.environ["TELEGRAM_BOT_TOKEN"])
        return

    try:
        cfg = Cfg()
    except KeyError as e:
        sys.exit(
            f"Missing setting {e}. Set it as an environment variable (a GitHub secret "
            "on Actions), or put it in a .env file (see .env.example)."
        )
    except ValueError as e:
        sys.exit(f"Bad setting: {e}")
    notify = make_notifier(cfg.tg_token, cfg.chat_id)
    notify_admin = make_admin_notifier(cfg, notify)

    if args.test_telegram:
        ok = notify(fmt("✅ Test message", "Telegram is wired up correctly."))
        sys.exit(0 if ok else "Telegram send failed, check the bot token and chat id.")

    db = DB(cfg.db_path)
    m = Moodle(cfg.base, cfg.token)

    while True:
        try:
            handle_telegram_commands(cfg, db)  # first, so a new token applies immediately
        except Exception:
            log.exception("Telegram command handling failed")
        m.token = effective_token(cfg, db)
        try:
            info = m.call("core_webservice_get_site_info")
            userid = info["userid"]
            if db.get("userid") is None:
                db.set("userid", userid)  # remembered so /token can't switch accounts
            redact = build_redactor(info)  # alerts never contain your own name
            run_cycle(cfg, m, db, lambda text: notify(redact(text)), userid)
        except TokenExpired as e:
            log.error("Token expired: %s", e)
            token_alert(cfg, db, notify_admin, str(e))
        except Exception:
            log.exception("Cycle failed")
        if args.once:
            break
        time.sleep(max(60, cfg.poll_minutes * 60 + random.randint(-60, 60)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)