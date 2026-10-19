#!/usr/bin/env python3
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

Config comes from environment variables or a .env file (see .env.example).

Layout: this file holds the Moodle logic (API client, state, checks, scheduling).
Telegram delivery and message formatting live in telegram_notifier.py.
"""
import argparse
import base64
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

from telegram_notifier import fmt, get_chat_id, make_notifier

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
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


class Cfg:
    def __init__(self):
        self.base = os.environ["MOODLE_URL"].rstrip("/")
        self.token = parse_token(os.environ["MOODLE_TOKEN"])
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.poll_minutes = int(os.environ.get("POLL_MINUTES", "15"))
        self.db_path = os.environ.get("DB_PATH", "moodle_bot.db")
        self.reminder_hours = sorted(
            int(x) for x in os.environ.get("REMINDER_HOURS", "24,3").split(",") if x.strip()
        )
        # Reminders for assignments that are not submitted yet (hours before the deadline)
        self.assign_reminder_hours = sorted(
            int(x)
            for x in (os.environ.get("ASSIGN_REMINDER_HOURS") or "48,24").split(",")
            if x.strip()
        )
        self.full_check_hours = int(os.environ.get("FULL_CHECK_HOURS", "3"))
        self.skip_courses = {
            int(x) for x in os.environ.get("SKIP_COURSE_IDS", "").split(",") if x.strip()
        }
        # India Standard Time by default (UTC+5:30)
        self.tz = timezone(timedelta(minutes=int(os.environ.get("TZ_OFFSET_MINUTES", "330"))))


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


def fmt_time(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%a %d %b, %I:%M %p")


def fmt_span(seconds):
    """1 day 3 h / 5 h 20 min / 40 min"""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{max(mins, 1)} min"


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

    items = []
    for e in events:
        due = e.get("timesort") or e.get("timestart") or 0
        course = short((e.get("course") or {}).get("shortname") or (e.get("course") or {}).get("fullname", ""))
        fp = f"event:{e['id']}:{e.get('timemodified', 0)}"
        body = f"{e.get('name', '')}\n{course}\nDue: {fmt_time(due, cfg.tz)}"
        items.append((fp, fmt("📅 New / changed deadline", body, e.get("url", ""))))
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
            if h == smallest:
                course = short((e.get("course") or {}).get("shortname", ""))
                left = f"{hours_left:.0f}h" if hours_left >= 1 else f"{hours_left * 60:.0f} min"
                body = f"{e.get('name', '')}\n{course}\nDue: {fmt_time(due, cfg.tz)}"
                if not notify(fmt(f"⏰ Due in ~{left}", body, e.get("url", ""))):
                    continue
            db.add(fp)


STATUS_TEXT = {
    "submitted": "✅ Submitted",
    "draft": "📝 Draft saved, NOT submitted yet",
    "new": "❌ Not submitted",
    "reopened": "❌ Reopened, not submitted",
    "unknown": "❓ Unknown",
}


def submission_state(m, assign_id, userid):
    """Returns (status, can_submit, extension_due).
    status is new / draft / submitted / reopened / unknown."""
    try:
        s = m.call("mod_assign_get_submission_status", assignid=assign_id, userid=userid)
    except MoodleError:
        return "unknown", None, 0
    last = s.get("lastattempt") or {}
    sub = last.get("teamsubmission") or last.get("submission") or {}
    return (
        sub.get("status", "new"),
        last.get("submissionsenabled"),
        int(last.get("extensionduedate") or 0),
    )


def assignment_message(cfg, a, cname, state, title):
    status, _, ext = state
    due = max(a.get("duedate") or 0, ext)
    body = (
        f"{strip_html(a.get('name', ''))}\n{cname}\n"
        f"Deadline: {fmt_time(due, cfg.tz) if due else 'not set'}\n"
        f"Time left: {time_left(due, time.time())}\n"
        f"Status: {STATUS_TEXT.get(status, status)}"
    )
    url = f"{cfg.base}/mod/assign/view.php?id={a.get('cmid', '')}"
    return fmt(title, body, url)


def check_assignments(m, db, cfg, notify, courses, userid, now):
    if not courses:
        return
    params = {f"courseids[{i}]": c["id"] for i, c in enumerate(courses)}
    data = m.call("mod_assign_get_assignments", **params)
    assigns = []
    for co in data.get("courses", []):
        cname = short(co.get("shortname") or co.get("fullname", ""))
        for a in co.get("assignments", []):
            assigns.append((cname, a))

    # 1) new assignments and changed deadlines (message is built only when sent)
    items = []
    for cname, a in assigns:
        due = a.get("duedate") or 0
        fp = f"assign:{a['id']}:{due}"
        title = (
            "🔁 Assignment deadline changed"
            if db.seen_prefix(f"assign:{a['id']}:")
            else "📝 New assignment"
        )
        items.append(
            (
                fp,
                lambda a=a, cname=cname, title=title: assignment_message(
                    cfg, a, cname, submission_state(m, a["id"], userid), title
                ),
            )
        )
    process(db, "assignments", items, notify)

    # 2) reminders, only while the assignment is NOT submitted yet.
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
            continue  # already reminded for this window, no API call needed

        state = submission_state(m, a["id"], userid)
        status, can_submit, ext = state
        eff_due = max(due, ext)
        if eff_due != due:  # you were granted an extension: judge by that deadline
            hours_left = (eff_due - now) / 3600
            if hours_left <= 0 or hours_left > max_h:
                continue
            fitting = [h for h in windows if hours_left <= h]
            fps = {h: f"aremind:{a['id']}:{eff_due}:{h}" for h in fitting}
            if db.seen(fps[min(fitting)]):
                continue
        if status == "submitted" or can_submit is False:
            for fp in fps.values():  # nothing to remind about
                db.add(fp)
            continue

        title = f"⏰ Assignment due in {fmt_span(eff_due - now)}"
        if not notify(assignment_message(cfg, a, cname, state, title)):
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
        check_assignments(m, db, cfg, notify, courses, userid, now)
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


def token_alert(db, notify, msg):
    last = int(db.get("token_alert_ts") or 0)
    if time.time() - last > 86400:
        notify(
            fmt(
                "🔑 Moodle token expired",
                f"{msg}\nGet a new one via the launch.php step and update MOODLE_TOKEN.",
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
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram's URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env()

    if args.get_chat_id:
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            sys.exit("Set TELEGRAM_BOT_TOKEN first.")
        get_chat_id(os.environ["TELEGRAM_BOT_TOKEN"])
        return

    try:
        cfg = Cfg()
    except KeyError as e:
        sys.exit(f"Missing setting {e}. Copy .env.example to .env and fill it in.")
    notify = make_notifier(cfg.tg_token, cfg.chat_id)

    if args.test_telegram:
        ok = notify(fmt("✅ Test message", "Telegram is wired up correctly."))
        sys.exit(0 if ok else "Telegram send failed, check the bot token and chat id.")

    db = DB(cfg.db_path)
    m = Moodle(cfg.base, cfg.token)

    while True:
        try:
            userid = m.call("core_webservice_get_site_info")["userid"]
            run_cycle(cfg, m, db, notify, userid)
        except TokenExpired as e:
            log.error("Token expired: %s", e)
            token_alert(db, notify, str(e))
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