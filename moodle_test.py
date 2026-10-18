#!/usr/bin/env python3
"""
Moodle web service tester (read-only).

Checks whether the mobile web service is enabled for your account and which
of the functions a notifier bot needs actually work.

Install:   pip install httpx
Run:       python moodle_test.py
           python moodle_test.py --url https://lms.college.edu
           python moodle_test.py --url https://lms.college.edu --token YOUR_TOKEN
           python moodle_test.py --url https://lms.college.edu --sso "moodlemobile://token=BASE64..."

Only read functions are called. Nothing is written to Moodle.
The password is typed via getpass and never saved.
"""
import argparse
import base64
import getpass
import sys
import time
from urllib.parse import unquote

import httpx

TIMEOUT = 30
results = []  # (name, status, detail)


def record(name, status, detail=""):
    results.append((name, status, detail))
    icon = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
    print(f"{icon} {name}" + (f"  -> {detail}" if detail else ""))


def mask(token):
    return token[:4] + "..." + token[-4:] if len(token) > 10 else "***"


def parse_sso(value):
    """Accepts 'moodlemobile://token=BASE64' or the bare BASE64 string.
    Decodes to 'siteid:::token:::privatetoken' and returns the token."""
    value = unquote(value.strip())
    if "token=" in value:
        value = value.split("token=", 1)[1]
    value += "=" * (-len(value) % 4)  # fix missing padding
    decoded = base64.b64decode(value).decode("utf-8", errors="replace")
    parts = decoded.split(":::")
    if len(parts) < 2:
        raise ValueError(f"Unexpected SSO payload format: {decoded!r}")
    return parts[1]


def get_token(client, base, username, password):
    r = client.post(
        f"{base}/login/token.php",
        data={"username": username, "password": password, "service": "moodle_mobile_app"},
    )
    r.raise_for_status()
    data = r.json()
    if "token" in data:
        return data["token"]
    raise RuntimeError(f"{data.get('errorcode', 'error')}: {data.get('error', data)}")


def call(client, base, token, function, **params):
    """Call a Moodle web service function. Returns parsed JSON or raises RuntimeError."""
    payload = {"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json"}
    payload.update(params)
    r = client.post(f"{base}/webservice/rest/server.php", data=payload)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and ("exception" in data or "errorcode" in data):
        raise RuntimeError(f"{data.get('errorcode')}: {data.get('message')}")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Moodle base URL, e.g. https://lms.college.edu")
    ap.add_argument("--token", help="Existing web service token (skips login)")
    ap.add_argument("--sso", help="moodlemobile://token=... URL from the SSO flow")
    ap.add_argument("--username")
    args = ap.parse_args()

    base = (args.url or input("Moodle base URL (no trailing slash): ")).strip().rstrip("/")
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": "moodle-notifier-test/0.1"})

    # ---- 1. Get a token -------------------------------------------------
    print(f"\n=== Testing {base} ===\n")
    token = args.token
    try:
        if not token and args.sso:
            token = parse_sso(args.sso)
            record("Parse SSO token", "PASS", mask(token))
        elif not token:
            username = args.username or input("Username: ").strip()
            password = getpass.getpass("Password (hidden): ")
            token = get_token(client, base, username, password)
            record("Login / token request", "PASS", f"token {mask(token)}")
        else:
            record("Using supplied token", "PASS", mask(token))
    except httpx.HTTPError as e:
        record("Login / token request", "FAIL", f"network/HTTP error: {e}")
        print("\nCould not reach the site. Check the URL.")
        return summary()
    except Exception as e:
        record("Login / token request", "FAIL", str(e))
        print(
            "\nHints:\n"
            " - 'enablewsdisabled' / 'servicenotavailable': mobile web services are off. Fall back to the iCal feed.\n"
            " - 'invalidlogin': wrong credentials, or you use SSO (Google/Microsoft/CAS). Re-run with --sso.\n"
        )
        return summary()

    # ---- 2. Site info + enabled functions ------------------------------
    try:
        info = call(client, base, token, "core_webservice_get_site_info")
        record("core_webservice_get_site_info", "PASS",
               f"{info.get('fullname')} (userid {info.get('userid')}), Moodle {info.get('release')}")
    except Exception as e:
        record("core_webservice_get_site_info", "FAIL", str(e))
        return summary()

    userid = info["userid"]
    enabled = {f["name"] for f in info.get("functions", [])}

    wanted = [
        "core_enrol_get_users_courses",
        "core_calendar_get_action_events_by_timesort",
        "core_message_get_popup_notifications",
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
        "mod_forum_get_forum_discussions_paginated",
        "core_course_get_contents",
        "core_course_get_updates_since",
        "mod_assign_get_assignments",
        "mod_quiz_get_quizzes_by_courses",
        "gradereport_user_get_grade_items",
    ]
    print("\n--- Functions enabled for your account ---")
    for fn in wanted:
        print(f"   {'yes' if fn in enabled else 'NO '}  {fn}")
    print()

    related = sorted(f for f in enabled if "popup" in f or "notification" in f)
    print("--- Enabled functions mentioning popup/notification ---")
    for f in related:
        print(f"   {f}")
    print()

    def run(name, **params):
        if name not in enabled:
            record(name, "SKIP", "not enabled for this account")
            return None
        try:
            data = call(client, base, token, name, **params)
            return data
        except Exception as e:
            record(name, "FAIL", str(e))
            return None

    now = int(time.time())

    # ---- 3. Courses ----------------------------------------------------
    courses = run("core_enrol_get_users_courses", userid=userid)
    course_ids = []
    if courses is not None:
        course_ids = [c["id"] for c in courses]
        names = ", ".join(c.get("shortname", str(c["id"])) for c in courses[:5])
        record("core_enrol_get_users_courses", "PASS", f"{len(courses)} courses: {names}")

    # ---- 4. Calendar deadlines ----------------------------------------
    ev = run("core_calendar_get_action_events_by_timesort", timesortfrom=now, limitnum=20)
    if ev is not None:
        events = ev.get("events", [])
        nxt = events[0]["name"] if events else "none upcoming"
        record("core_calendar_get_action_events_by_timesort", "PASS",
               f"{len(events)} upcoming events (next: {nxt})")

    # ---- 5. Popup notifications ---------------------------------------
    pn = run("core_message_get_popup_notifications", useridto=userid)
    if pn is not None:
        record("core_message_get_popup_notifications", "PASS",
               f"{len(pn.get('notifications', []))} notifications")

    # ---- 6. Forums / announcements ------------------------------------
    news_forum_id = None
    if course_ids:
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        forums = run("mod_forum_get_forums_by_courses", **params)
        if forums is not None:
            news = [f for f in forums if f.get("type") == "news"]
            record("mod_forum_get_forums_by_courses", "PASS",
                   f"{len(forums)} forums, {len(news)} announcement forums")
            if news:
                news_forum_id = news[0]["id"]

    if news_forum_id:
        fn = ("mod_forum_get_forum_discussions"
              if "mod_forum_get_forum_discussions" in enabled
              else "mod_forum_get_forum_discussions_paginated")
        # Default ordering is pinned first, then newest. sortby/sortdirection
        # belong to the old _paginated function and are rejected on 4.x.
        d = run(fn, forumid=news_forum_id)
        if d is not None:
            discussions = d.get("discussions", [])
            latest = discussions[0].get("name") if discussions else "none"
            record(fn, "PASS", f"{len(discussions)} announcements (latest: {latest})")

    # ---- 7. Course contents + updates_since ---------------------------
    if course_ids:
        cid = course_ids[0]
        contents = run("core_course_get_contents", courseid=cid)
        if contents is not None:
            modules = sum(len(s.get("modules", [])) for s in contents)
            record("core_course_get_contents", "PASS",
                   f"course {cid}: {len(contents)} sections, {modules} modules")

        upd = run("core_course_get_updates_since", courseid=cid, since=now - 7 * 86400)
        if upd is not None:
            record("core_course_get_updates_since", "PASS",
                   f"{len(upd.get('instances', []))} changed items in last 7 days")

    summary()


def summary():
    print("\n=== Summary ===")
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failed == 0 and passed >= 3:
        print("Looks good: the web service works for you. You can build the bot on these calls.")
    elif passed == 0:
        print("Web service not usable. Paste the exact error message and we'll pick the next route.")
    else:
        print("Partly working. Paste the FAIL/SKIP lines and we'll pick substitutes.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)