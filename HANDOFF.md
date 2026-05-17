# S.T.A.R. UNIT 7 — Claude-to-Claude Handoff

This document is written for a Claude instance (or another AI assistant) that is helping a new user set up and run this project. Read it before touching anything.

---

## What this project is

A personal task management system called **S.T.A.R.** — named after the "Starling" replika type in the video game *Signalis*. In that game, STAR units are state-assigned monitors that watch over "gestaults" (humans) and enforce compliance with their directives. The user is roleplaying as a gestault in the Eusan Nation accessing a state-controlled terminal.

This is not cosmetic. It directly shapes the UI copy, the Claude prompt language, and the tone of all Telegram notifications. Maintain it throughout all work on this project.

---

## Aesthetic — Non-Negotiable

**Cassette futurism / retro-futurism.** Think Blade Runner terminals, the Nostromo in Alien, Soviet-era industrial computers. The reference game *Signalis* uses cold, sparse, bureaucratic language.

Rules for any UI or copy work:
- **No friendly language.** Tasks are "directives." Completion is "confirmed." Deletion is "purged."
- **Uppercase everywhere** in labels, badges, system copy.
- **No warm colors** except red for alerts/overdue.
- **Sparse.** If it doesn't need to be there, it isn't there.

Color palette (CSS variables in `star.html`):
```
--bg:          #040904   page background
--bg-panel:    #050e05   panel backgrounds
--bg-item:     #030703   task item backgrounds
--border:      #1a521a   default borders
--border-hi:   #2cdd2c   bright accent borders
--green-hi:    #33ee33   primary text / active elements
--green-mid:   #1aaa1a   secondary text / panel titles
--green-dim:   #0d600d   dim text / labels
--green-dark:  #072207   very dim / decorative
--alert:       #dd2222   overdue / follow-up active / delete
--alert-dim:   #6b0f0f   alert borders
--warn:        #b89000   due-soon state (< 1hr)
```

Fonts:
- `VT323` — display/headers, clock, panel titles, submit button
- `Share Tech Mono` — all body text, form inputs, task content

Visual effects in the HTML:
- CSS scanline overlay on `body::before`
- Moving scan bar (`.scanbar`, CSS animation)
- Subtle screen flicker on `body`
- Blinking cursor (`.cur`, step-end animation)
- Corner bracket decorations on panels (`.cbox::before` / `::after`)

---

## Architecture

```
star/
├── star.html          # Frontend — served by Flask at http://127.0.0.1:7823
├── star_server.py     # Flask server + notification daemon (single process, 3 threads)
├── tasks.json         # Task persistence (auto-created on first run)
├── notifications.json # Notification state per task (auto-created)
├── .env               # Secrets — NEVER commit this
├── env.example        # Config template — copy to .env and fill in
├── requirements.txt   # Python deps: flask, anthropic, python-dotenv
├── star.service       # systemd unit file
└── README.md          # Setup instructions for the user
```

### Flask server (`star_server.py`)
- Runs on `127.0.0.1:PORT` (default 7823, set via `STAR_PORT` in `.env`)
- Serves `star.html` at `/`
- `GET /api/tasks` → returns `{ tasks: [...], counter: N }`
- `POST /api/tasks` → accepts same shape, writes to `tasks.json`
- Spawns notification daemon and Telegram receive loop as background daemon threads on startup

### Notification daemon (background thread)
- Wakes every `STAR_CHECK_INTERVAL` seconds (default 900 = 15 min)
- For each overdue task with `followUp: true`: calls Claude API → sends Telegram message. Repeats every `STAR_ESCALATION_INTERVAL` seconds with escalating tone until task is marked complete or `STAR_MAX_ESCALATIONS` is reached.
- For each overdue task with `followUp: false`: sends one plain-text Telegram message, never repeats.
- State persisted in `notifications.json`. Stale entries (tasks that no longer exist) are auto-cleaned.

### Telegram receive loop (background thread)
- Polls `getUpdates` every `STAR_RECEIVE_INTERVAL` seconds (default 60s)
- Handles inbound commands from the configured Telegram chat:
  - `CONFIRM [ID]` — marks a directive complete
  - `SNOOZE [ID] [30m|1h|2h]` — defers a directive
  - `LIST` — returns all active directives sorted by urgency
  - `ADD: <natural language>` — creates a new directive via Claude NLP parsing
- Command responses are plain system messages. STAR voice is reserved for escalation alerts and snooze notices only.

### Frontend (`star.html`)
- Single HTML file, no build step, no external JS dependencies.
- Fetches task data from the local Flask server on load.
- All mutations POST the full updated task array to the server.
- Re-syncs with server every 60 seconds.
- Shows SERVER: CONNECTED / OFFLINE indicator in the status bar.
- Task list split into SINGLE-USE / RECURRING tabs with per-tab active counts.

---

## Task data model

```json
{
  "id": 1,
  "title": "SUBMIT QUARTERLY COMPLIANCE REPORT",
  "desc": "Optional description string",
  "dueDate": "2025-05-10T09:00:00.000Z",
  "recur": "none | daily | weekly | monthly",
  "priority": "normal | high | critical",
  "followUp": true,
  "completed": false,
  "createdAt": "2025-05-10T08:00:00.000Z",
  "completedAt": null,
  "snoozeCount": 0
}
```

Recurring tasks: when confirmed complete, a new task instance is created with the next due date. The completed original is retained (greyed out) until purged.

---

## Claude API usage

- Model: `claude-haiku-4-5-20251001` (cost-efficient for short messages)
- Used only by `star_server.py` — the frontend makes no direct API calls
- Key stored in `.env` as `ANTHROPIC_API_KEY`, loaded via `python-dotenv`
- Falls back gracefully to plain-text alerts if key is missing

Three uses:
1. **Escalation alerts** — in-character STAR messages for overdue follow-up tasks
2. **Snooze notices** — in-character STAR messages when a task is snoozed 2+ times
3. **NLP task parsing** — structured JSON extraction from natural language ADD commands

### Escalation tiers (0-indexed, maps to `ESCALATION_TONE` list in `star_server.py`):
- 0: Clinical, matter-of-fact. First contact.
- 1: Firm. Notes directive is outstanding.
- 2: Cold, curt. Notes persistent non-compliance.
- 3+: Authoritarian. States non-compliance is being logged. Holds here indefinitely.

The system prompt frames Claude as "S.T.A.R. Unit 7, a Eusan Nation automated gestault oversight system." Messages must be ≤3 sentences, no pleasantries, no markdown, begin with `[S.T.A.R. UNIT 7]`.

### Tone boundary
STAR character voice is used only for escalation alerts and snooze notices (2+ snoozes). All command responses (CONFIRM, LIST, ADD, SNOOZE acknowledgments, errors) are plain system text — no STAR framing.

---

## Telegram integration

Pure stdlib `urllib.request` — no extra dependencies. A bot is created via @BotFather and added to a private chat or group.

Config in `.env`:
```
STAR_TELEGRAM_TOKEN=<bot token from BotFather>
STAR_TELEGRAM_CHAT_ID=<chat ID — negative for groups, positive for private chats>
```

If Telegram is not configured, alerts print to stdout instead — useful during development.

---

## Snooze escalation behavior

- `snoozeCount` is stored on the task object and incremented on each snooze
- Snooze 1: plain system confirmation only
- Snooze 2: plain confirmation + hardcoded cold one-liner from STAR
- Snooze 3+: plain confirmation + Claude-generated escalation message
- Count resets when a recurring task re-issues

---

## What is complete

- [x] Full UI — task list, entry form, status panel, header clock, status bar
- [x] Task CRUD — add, complete (with recurring re-issue), edit/amend, delete/purge
- [x] Task list split into SINGLE-USE / RECURRING tabs
- [x] Task states — overdue, due-soon (< 1hr), active, completed; correct sort order
- [x] Follow-up toggle — persisted per task, shown as badge, drives notification behavior
- [x] Priority levels — normal / high / critical (visual badge only)
- [x] Snooze — UI (30m/1h/2h selector) and Telegram command; STAR escalates after 2nd snooze
- [x] Flask server with task persistence to JSON
- [x] Notification daemon with escalation logic and per-task cap
- [x] Claude API integration — escalation alerts, snooze notices, NLP task parsing
- [x] Telegram send/receive via Bot API (no extra deps)
- [x] Telegram inline buttons — CONFIRM button on alert messages
- [x] Graceful fallback if API key or Telegram not configured
- [x] systemd service file
- [x] README with full setup instructions

## Known gaps / possible next tasks

- [ ] **Priority does nothing functional** — high/critical show a badge but don't affect sort order, escalation tone, or notification frequency
- [ ] **No authentication** on the Flask server — binds to 127.0.0.1 only; LAN access would require binding to `0.0.0.0` plus basic auth or a firewall rule
- [ ] **Completed tasks accumulate** — no bulk-purge for old completed items
- [ ] **No due-soon notifications** — daemon only fires on overdue, not "due in 30 minutes"

---

## Dev notes

- No npm, no webpack, no frontend build step. The HTML is entirely self-contained.
- Python venv at `./venv/`. Run with `./venv/bin/python star_server.py`.
- Server, notification daemon, and Telegram receive loop all start in one process (three threads).
- `tasks.json` and `notifications.json` are auto-created if missing; safe to delete for a clean slate.
- `.env` must have `chmod 600`. The README instructs the user to do this.
- Do not add `flask-cors` — CORS is not needed since the HTML is served by the same Flask process.
- `STAR_CHECK_INTERVAL` controls daemon wakeup frequency. `STAR_ESCALATION_INTERVAL` controls how often a single follow-up task gets re-escalated (can be set independently).
