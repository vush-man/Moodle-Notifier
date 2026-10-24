# Moodle Notifier

A small, read-only bot that watches your Moodle account and sends you **Telegram** messages for new deadlines, announcements, course materials and Moodle notifications, so you stop missing quizzes and assignments when Moodle's own emails and push notifications don't arrive.

It talks to Moodle through the same **mobile web service API** the official Moodle app uses, so it needs no browser automation and no admin access. It runs happily on a free **GitHub Actions** schedule.

> Unofficial project. Not affiliated with or endorsed by Moodle or any university. Use it only with your own account and in line with your institution's rules.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start (local)](#quick-start-local)
- [Getting a Moodle token](#getting-a-moodle-token)
- [Setting up Telegram](#setting-up-telegram)
- [Configuration](#configuration)
- [Deploying on GitHub Actions](#deploying-on-github-actions)
- [Checking your Moodle supports it](#checking-your-moodle-supports-it)
- [Moodle API functions used](#moodle-api-functions-used)
- [Behaviour details](#behaviour-details)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Project structure](#project-structure)

---

## Features

| Source | What you get |
| --- | --- |
| **Assignments** | A dedicated alert for each new assignment, or a changed deadline, showing the **deadline and time left** |
| **Assignment reminders** | Reminders **2 days and 1 day** before the deadline (configurable). They are sent **regardless of submission status**: the bot never checks it |
| **Other deadlines** | New or changed quizzes and other calendar deadlines (next 90 days), with reminders 24 h and 3 h before (completed ones are skipped) |
| **Announcements** | New or edited posts in every course's announcements (news) forum |
| **Course materials** | New or updated files, pages, links and folders in any of your courses |
| **Notification bell** | The same notifications Moodle shows in the web UI |
| **Health alerts** | A Telegram message if your token expires, or if the bot fails several runs in a row |
| **Token renewal** | Optional: send `/token ...` to the bot in a private chat to replace an expired Moodle token, stored encrypted |

Other design points:

- **Read-only.** The bot only calls Moodle functions that read data. It never writes, submits or marks anything.
- **No spam on first run.** The first run stores everything that already exists silently, and only *new* things notify you afterwards.
- **Safe retries.** An item is only marked as seen after Telegram accepts the message, so a failed delivery is retried on the next run.
- **Polite polling.** Cheap "what changed?" checks first, full scans only when needed, short pauses between calls.
- **Runs anywhere.** A local machine, a VPS, or scheduled GitHub Actions.

## How it works

```mermaid
flowchart LR
    A[Scheduler<br/>cron / Actions / loop] --> B[moodle_bot.py]
    B -->|REST calls with token| C[(Moodle<br/>web service)]
    B <-->|seen items, timestamps| D[(SQLite state)]
    B -->|new items only| E[Telegram bot]
    E --> F[Your phone]
```

Each run:

1. Looks up your user and your enrolled courses (courses that ended more than a week ago are skipped).
2. Checks **deadlines** and sends reminders for those coming due.
3. Checks the **notification bell**.
4. Checks each course's **announcements forum**.
5. For each course, asks Moodle *"has anything changed since I last looked?"*. A full contents scan only happens if so, plus a safety-net scan every few hours.
6. Compares everything against the state database and sends Telegram messages for anything it hasn't seen.

Every item gets a fingerprint, such as `announce:<discussion id>:<time modified>` or `module:<module id>:<file time modified>`. A new ID means something new appeared, and a changed timestamp means something was edited or re-uploaded.

## Requirements

- Python 3.9+ (3.12 recommended)
- A Moodle site (tested against Moodle 4.5) with the **mobile web service enabled**
- A Telegram account
- `httpx` and `cryptography` (see `requirements.txt`; `cryptography` is only used for token renewal over Telegram)

## Quick start (local)

```bash
git clone https://github.com/vush-man/Moodle-Notifier.git
cd moodle-notifier
python -m venv venv
# Windows: venv\Scripts\activate      macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill it in (see Configuration)

python moodle_bot.py --test-telegram   # should send a test message to your phone
python moodle_bot.py --once            # first run: seeds silently, sends one "bot is running" message
python moodle_bot.py                   # keep running, checking every POLL_MINUTES
```

If Telegram settings are left empty, notifications are printed to the terminal instead, which is handy for a dry run.

### Command-line options

| Option | Purpose |
| --- | --- |
| `--once` | Run one check and exit (used by GitHub Actions and cron) |
| `--test-telegram` | Send a test message and exit |
| `--get-chat-id` | Print the chat IDs your Telegram bot has seen |
| `--gen-key` | Print a new `TOKEN_ENCRYPTION_KEY` |
| `-v`, `--verbose` | Debug logging |

## Getting a Moodle token

The token is what the bot uses instead of your password. **Treat it like a password.**

### If you log in with a Moodle username and password

```bash
curl -X POST "https://YOUR-MOODLE-URL/login/token.php" \
  -d "username=YOUR_USER" -d "password=YOUR_PASS" -d "service=moodle_mobile_app"
```

A response like `{"token":"..."}` is your token. An error such as `enablewsdisabled` or `servicenotavailable` means the site admin has turned the mobile service off.

### If you log in with SSO (Microsoft, Google, CAS...)

SSO accounts have no Moodle password, so use the same launch flow as the mobile app:

1. Log in to Moodle in a desktop browser. Open DevTools (F12) and go to the **Network** tab with **Preserve log** ticked.
2. In the same tab open:
   ```
   https://YOUR-MOODLE-URL/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile
   ```
3. The page will look blank or fail, which is expected. The browser can't open the `moodlemobile://` address it is redirected to. In the Network tab, find the request whose URL starts with `moodlemobile://token=...` (or the `launch.php` request and its `Location` response header) and copy the whole string.
4. Put that string in `MOODLE_TOKEN`. The bot accepts either the raw 32-character token or the full `moodlemobile://token=...` string.

If your browser hides the redirect, copy your `MoodleSession` cookie and run
`curl -i -H "Cookie: MoodleSession=VALUE" "<the launch URL>"`, then read the `location:` header. Don't share that cookie with anyone.

### Token lifetime

Tokens expire on a schedule set by your site admin. Check **Profile → Preferences → User account → Security keys** for the "Moodle mobile web service" key and its valid-until date. When the token expires the bot sends you a Telegram alert (at most once a day). Repeat the steps above and update `MOODLE_TOKEN`, or send it to the bot (see below).

### Renewing the token from Telegram (optional)

Instead of editing the GitHub secret by hand, send the new token to the bot from your phone. The bot checks it with Moodle, stores it **encrypted**, deletes your message and confirms.

**One-time setup**

1. Generate an encryption key: `python moodle_bot.py --gen-key`. Save the output as a repository secret named `TOKEN_ENCRYPTION_KEY`. Keep a copy somewhere safe.
2. If `TELEGRAM_CHAT_ID` is a **group**, also add a secret `TELEGRAM_ADMIN_ID` with your own Telegram user ID. Message the bot privately, run `python moodle_bot.py --get-chat-id`, and use the number for your private chat. (If `TELEGRAM_CHAT_ID` is already your private chat, this is automatic.)

**Renewing**

1. Get a token as described above (the `moodlemobile://token=...` string).
2. Open a **private chat** with your bot and send `/token moodlemobile://token=...`. Pasting the bare string also works.
3. You'll get a confirmation. The new token is used from the next run, which can take up to 15 to 30 minutes on GitHub's schedule. To apply it immediately, run the workflow manually from the Actions tab.

**How it's protected**

- Only your Telegram user ID can send commands, and only in a private chat. Messages from anyone else are ignored without a reply.
- The token is verified against Moodle first, and must belong to the same Moodle account as before.
- It is stored in the state database only in encrypted form. This matters on public repos, where Actions caches can be read by workflows triggered from forks. They can't read your secrets, so they can't decrypt it.
- Your message is deleted right after processing. If you paste a token in a group by mistake, the bot warns you and you should reset it in Moodle.
- If you later update the `MOODLE_TOKEN` secret by hand, the newer secret takes priority over the stored token.

**Caveats**

- The token passes through Telegram's servers. If you're not comfortable with that, update the GitHub secret instead.
- Nothing else may read this bot's updates (`getUpdates`) or have a webhook set on it, or the two will conflict.
- If you change `TOKEN_ENCRYPTION_KEY`, the stored token can't be decrypted and the bot falls back to `MOODLE_TOKEN`.

## Setting up Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Copy the bot token it gives you.
2. Open a chat with your new bot and send it any message (for example `/start`).
3. Run `python moodle_bot.py --get-chat-id` and copy the number it prints. That's your `TELEGRAM_CHAT_ID`.
4. Run `python moodle_bot.py --test-telegram` to confirm it works.

**Group chats:** add the bot to the group, send a message that mentions it (for example `@YourBot hi`, since bots only see commands and mentions by default), then run `--get-chat-id` again. Group IDs are negative, often starting with `-100`; keep the minus sign. Everyone in the group will see every alert.

## Configuration

Settings come from environment variables, or from a `.env` file in the working directory.

| Variable | Required | Default | Description |
| --- | :---: | --- | --- |
| `MOODLE_URL` | yes | | Base URL, e.g. `https://lms.example.edu` (no trailing slash) |
| `MOODLE_TOKEN` | yes | | Raw token or `moodlemobile://token=...` string |
| `TELEGRAM_BOT_TOKEN` | no* | | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | no* | | Chat to send alerts to |
| `TELEGRAM_ADMIN_ID` | no | chat id if private | Your Telegram user ID: the only user allowed to send `/token`. Required if `TELEGRAM_CHAT_ID` is a group |
| `TOKEN_ENCRYPTION_KEY` | no | | Enables `/token` renewal. Generate with `--gen-key` |
| `POLL_MINUTES` | no | `15` | Interval in loop mode (ignored with `--once`) |
| `REMINDER_HOURS` | no | `24,3` | Reminder windows (hours before) for non-assignment deadlines such as quizzes |
| `ASSIGN_REMINDER_HOURS` | no | `48,24` | Reminder windows (hours before the deadline) for assignments |
| `FULL_CHECK_HOURS` | no | `3` | How often each course is fully rescanned as a safety net |
| `SKIP_COURSE_IDS` | no | | Comma-separated course IDs to ignore |
| `TZ_OFFSET_MINUTES` | no | `330` | Offset from UTC for displayed times (330 = India, IST) |
| `DB_PATH` | no | `moodle_bot.db` | Where the state database is stored |

\* Without Telegram settings the bot prints alerts to the terminal.

A template is provided as `.env.example`. **Never commit `.env`.**

## Deploying on GitHub Actions

This runs the bot for free on a schedule, with no server.

1. Create a new repository and push the project (the included `.gitignore` keeps `.env` and `*.db` out).
2. In **Settings → Secrets and variables → Actions**, add these repository **secrets**:
   `MOODLE_URL`, `MOODLE_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   Optional secrets: `TOKEN_ENCRYPTION_KEY` and `TELEGRAM_ADMIN_ID` (for [token renewal from Telegram](#renewing-the-token-from-telegram-optional)).
   Optionally add a repository **variable** `SKIP_COURSE_IDS`.
3. Open the **Actions** tab, choose **moodle-bot**, and click **Run workflow**.

How the workflow works (`.github/workflows/moodle-bot.yml`):

- It runs `python moodle_bot.py --once` on a cron schedule at offset minutes (`:07, :22, :37, :52`), because GitHub delays or drops runs at the busy `:00/:15/:30/:45` marks.
- A runner starts fresh every time, so the SQLite state file is saved and restored with `actions/cache`. Caches can't be overwritten, so each run saves under a unique key (`moodle-db-<run id>`) and the next run restores the newest one by prefix.
- A `concurrency` group prevents two runs from overlapping and racing on the state.

**Where the state lives.** The state is a small SQLite file, `moodle_bot.db`, in the runner's working directory. It is **not** stored in your repository and is never committed. At the start of each run it is restored from the GitHub Actions cache, and at the end it is saved back. You can see the saved copies in your repo under **Actions → Caches**. The file holds only item fingerprints and timestamps, no message text. If you use token renewal over Telegram it also holds your Moodle token, but only in encrypted form. GitHub deletes caches that haven't been used for 7 days, so if the schedule is paused for longer than that, the next run starts from scratch and seeds silently. Anything posted during the gap won't be reported.

**Verify state persistence.** Run the workflow twice by hand. The first run should log `Seeded ... existing items` and send one "bot is running" message. The second run must **not** seed again. If it does, the state isn't being restored and every run would be treated as a first run.

**Minutes and visibility.**
- *Public repo:* free unlimited minutes, so the 15-minute schedule is fine. Secrets are never exposed, and GitHub masks them in logs, but the code and run logs are public.
- *Private repo:* the free plan has a monthly minutes allowance, and a 15-minute schedule can exceed it. Switch to the commented 30-minute cron in the workflow, and check your plan's current allowance.

GitHub pauses scheduled workflows in repos with no activity for about 60 days. Push a small commit or re-enable the workflow if that happens. Scheduled runs can also start a few minutes late, which is fine for reminders measured in hours.

### Running on a server instead

Any always-on Linux machine works. Run the loop mode under systemd:

```ini
[Unit]
Description=Moodle notifier
After=network-online.target

[Service]
WorkingDirectory=/home/YOURUSER/moodle-notifier
ExecStart=/home/YOURUSER/moodle-notifier/venv/bin/python moodle_bot.py
Restart=always
RestartSec=30
User=YOURUSER

[Install]
WantedBy=multi-user.target
```

Keep `.env` readable only by that user (`chmod 600 .env`). Don't run the bot in two places at once with the same Telegram chat, because each copy has its own state and you will get duplicate alerts.

## Checking your Moodle supports it

`moodle_test.py` is an optional read-only diagnostic. It gets a token (password login, `--token`, or `--sso`), lists which web service functions are enabled for your account, and live-tests each one the bot needs.

```bash
python moodle_test.py --url https://YOUR-MOODLE-URL --sso "moodlemobile://token=..."
```

It prints `[ OK ]`, `[FAIL]` or `[SKIP]` per function and a summary. Run it before setting up the bot to see what your site allows.

## Moodle API functions used

All calls are made to `/webservice/rest/server.php`. They are all read-only.

| Function | Used for |
| --- | --- |
| `core_webservice_get_site_info` | Your user ID and the functions enabled for you |
| `core_enrol_get_users_courses` | Your enrolled courses |
| `core_calendar_get_action_events_by_timesort` | Upcoming quiz and other calendar deadlines |
| `mod_assign_get_assignments` | Assignments, with their deadlines |
| `message_popup_get_popup_notifications` | The notification bell |
| `mod_forum_get_forums_by_courses` | Finding each course's announcements forum |
| `mod_forum_get_forum_discussions` | Announcement posts |
| `core_course_get_updates_since` | Cheap "anything changed?" check per course |
| `core_course_get_contents` | Full course content snapshot for diffing |

Function availability differs between sites and versions. The notification bell function, for example, appears under different names on some sites. If your site exposes it differently, `moodle_test.py` prints the notification-related functions that are enabled.

## Behaviour details

- **Seeding.** Each source and course is seeded silently the first time it's seen. A course you enrol in later is seeded without flooding you.
- **Assignments vs materials.** Assignments are never reported as "new material". They have their own alert type and their own reminders. Calendar events for assignments are skipped by the generic deadline check, so you don't get duplicates. If the assignment check fails, the generic check covers them as a fallback.
- **Assignment reminders.** For each assignment with a deadline, you get a reminder 2 days and 1 day before it (`ASSIGN_REMINDER_HOURS`, default `48,24`). Submission status is never checked, so you still get the reminders after you've submitted, and no "submitted" message is ever sent. Only the tightest applicable window fires: an assignment first seen with 30 hours left gets one reminder, not two. Changing a deadline resets its reminders. Deadline extensions granted to you are not known to the bot, so reminders follow the assignment's original due date.
- **Reminders.** For each deadline, only the tightest applicable window fires. A deadline first seen with 2 hours left gets one reminder, not both the 24 h and 3 h ones. Changing a due date resets its reminders.
- **Completed work.** For quizzes and other calendar deadlines, events Moodle marks as not actionable (already done) are not reported. Assignments are the exception: they are handled separately and reminded about regardless.
- **Ended courses.** Courses whose end date passed more than 7 days ago are skipped. Use `SKIP_COURSE_IDS` for others.
- **Your own name is removed.** Before anything is sent, the bot strips the account owner's name from the text: the full name (in either order) and greetings such as "Hi NAME," that Moodle puts in some of its own notifications. Other people's names, including classmates or teachers who share your first or last name, are left alone.
- **Hidden content.** Items you can't see (hidden or restricted) aren't reported until they become visible to you, at which point they count as new.
- **Failure handling.** An expired token triggers a Telegram alert (at most once a day) and the run stops. The alert goes to your private chat with the bot (`TELEGRAM_ADMIN_ID`) rather than the group, and falls back to the main chat if the bot can't message you privately. Other errors are logged, and three consecutive failing runs trigger a warning.

Example messages:

```
⏰ Assignment due in 1 day 6 h

📝 Assignment: Mini Project
📚 Course: CSN304
📅 Deadline: Sat, 16 Jan 2027 (tomorrow)
🕒 Due at: 7:30 PM
⏳ Time left: 1 day 6 h

🔗 https://lms.example.edu/mod/assign/view.php?id=123
```

```
📢 Announcement · CSN203
Graded Lab 03
...

📚 New material · CSN202
Lecture 5 slides
Section: Week 5
```

Deadline messages (assignments, quizzes and other calendar events) use the same card layout. Dates always show the weekday, the month as a word and the year, plus a hint such as *(today)*, *(tomorrow)* or *(in 5 days)*, and the time is shown on its own line in 12-hour format. Times use `TZ_OFFSET_MINUTES`.

## Security notes

- Your token gives read access to your Moodle account. Keep it in environment variables or GitHub Secrets, never in code, issues or screenshots.
- If a token is exposed, revoke it under **Preferences → User account → Security keys** and generate a new one.
- Token renewal over Telegram only works for the Telegram user ID you configure, only in a private chat, and the token is stored encrypted. Keep `TOKEN_ENCRYPTION_KEY` as a secret and never commit it.
- `.gitignore` excludes `.env` and `*.db`. Check `git status` before your first push.
- The bot is read-only and polls gently. Keep it that way, and if you ever share it with other people, store their tokens encrypted and tell your institution.
- Telegram alerts contain course names, deadlines and snippets of announcements. In a group chat, everyone in the group can read them.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `enablewsdisabled` / `servicenotavailable` | The site admin disabled the mobile web service. This bot can't work on that site. |
| `invalidlogin` when requesting a token | Wrong password, or you use SSO. Use the launch-URL method. |
| Launch URL page is blank | Normal. Read the `moodlemobile://token=...` redirect from DevTools. Firefox is more reliable than Chrome for this. |
| `invalidtoken` / "token expired" alert | Get a new token, then update `MOODLE_TOKEN` or send `/token ...` to the bot. |
| `/token` gets no reply | It is applied at the next scheduled run (run the workflow manually to speed it up). Check that you're in a private chat, your user ID matches `TELEGRAM_ADMIN_ID`, and `TOKEN_ENCRYPTION_KEY` is set. |
| `/token` says "Can't store a token yet" | `TOKEN_ENCRYPTION_KEY` is missing or malformed. Generate one with `--gen-key`. |
| `invalidparameter` on forum discussions | Don't pass `sortby`/`sortdirection`. The bot uses the default ordering on Moodle 4.x. |
| A function shows `SKIP` in `moodle_test.py` | That function isn't enabled for your account. The bot's other sources still work. |
| Telegram test fails | Check the bot token and chat ID belong to the same bot, and that you've messaged the bot first. |
| Group messages don't arrive | Chat ID must include the minus sign, and the bot must be a member of the group. |
| Duplicate alerts | Two copies of the bot are running, or the Actions state isn't being restored. |
| Alert flood on every run (Actions) | The cache isn't restoring. Check the "Restore state" step log. |
| Scheduled workflow stopped | GitHub pauses idle repos after about 60 days. Re-enable it in the Actions tab. |

## Limitations

- It only sees what Moodle exposes to your account through the mobile service. Anything the admin has disabled isn't available.
- With SSO there's no way to refresh the token automatically, so you renew it by hand when it expires (the bot tells you).
- Detecting edits depends on Moodle's `timemodified` fields. Some plugin types don't update them reliably.
- GitHub Actions schedules are best-effort: runs can be delayed by several minutes.
- Very large course lists mean more API calls per run. Use `SKIP_COURSE_IDS` to trim them.

## Project structure

```
moodle-notifier/
├── moodle_bot.py                     # Moodle logic: API client, state, checks, scheduling, CLI
├── telegram_notifier.py              # Telegram delivery + message formatting (no Moodle code)
├── moodle_test.py                    # optional: checks what your Moodle allows
├── requirements.txt
├── .env.example                      # config template (copy to .env, never commit .env)
├── .gitignore
├── LICENSE
└── .github/
    └── workflows/
        └── moodle-bot.yml            # scheduled run + state caching
```

### Swapping Telegram for something else

`moodle_bot.py` only needs a `notify(text) -> bool` function (return `True` once the message is delivered, `False` to retry on the next run) and a `fmt(title, body, url)` helper. `telegram_notifier.py` provides both. To use another channel (Discord, email, ntfy...), write a module with the same two functions and change the import line at the top of `moodle_bot.py`.

## License

This project is licensed under the MIT License.
You are free to use, modify, and distribute this project, subject to the terms of the license.