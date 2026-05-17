#!/usr/bin/env python3
"""
STAR Unit 7 — Gestault Oversight System
Combined Flask server + notification daemon.

Serves the STAR web UI, persists task data to a local JSON file,
and runs a background thread that fires escalating Telegram alerts
for overdue follow-up directives.
"""

import os
import json
import time
import calendar
import threading
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG (all overridable via .env) ─────────────────────
PORT             = int(os.getenv('STAR_PORT',             '7823'))
TASKS_FILE       = Path(os.getenv('STAR_TASKS_FILE',      'tasks.json'))
NOTIF_FILE       = Path(os.getenv('STAR_NOTIF_FILE',      'notifications.json'))
CHECK_INTERVAL        = int(os.getenv('STAR_CHECK_INTERVAL',        '900'))  # seconds between daemon wakeups
ESCALATION_INTERVAL   = int(os.getenv('STAR_ESCALATION_INTERVAL',   '900'))  # seconds between per-task escalations
RECEIVE_INTERVAL      = int(os.getenv('STAR_RECEIVE_INTERVAL',       '60'))   # seconds between inbound polls
TELEGRAM_TOKEN        = os.getenv('STAR_TELEGRAM_TOKEN',             '')
TELEGRAM_CHAT_ID      = os.getenv('STAR_TELEGRAM_CHAT_ID',           '')
MAX_ESCALATIONS       = int(os.getenv('STAR_MAX_ESCALATIONS',        '4'))    # contacts before silence (initial + 3 follow-ups = 1h at 15m intervals)
ANTHROPIC_KEY    = os.getenv('ANTHROPIC_API_KEY',         '')
STATIC_DIR       = Path(__file__).parent

SNOOZE_DURATIONS = {'30m': 30, '1h': 60, '2h': 120}  # label → minutes

app = Flask(__name__, static_folder=str(STATIC_DIR))


# ── TASK PERSISTENCE ──────────────────────────────────────
def load_tasks() -> dict:
    if TASKS_FILE.exists():
        return json.loads(TASKS_FILE.read_text(encoding='utf-8'))
    return {'tasks': [], 'counter': 0}

def save_tasks(data: dict) -> None:
    TASKS_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')


# ── NOTIFICATION STATE ────────────────────────────────────
def load_notif_state() -> dict:
    if NOTIF_FILE.exists():
        return json.loads(NOTIF_FILE.read_text(encoding='utf-8'))
    return {}

def save_notif_state(state: dict) -> None:
    NOTIF_FILE.write_text(json.dumps(state, indent=2), encoding='utf-8')


# ── FLASK ROUTES ──────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'star.html')

@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    return jsonify(load_tasks())

@app.route('/api/tasks', methods=['POST'])
def post_tasks():
    data = request.get_json(force=True)
    save_tasks(data)
    return jsonify({'ok': True})

@app.route('/health')
def health():
    return jsonify({'status': 'online', 'unit': 'STAR-7'})


# ── CLAUDE ESCALATION ─────────────────────────────────────
claude = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Tone descriptor per contact attempt (0-indexed)
ESCALATION_TONE = [
    "clinical and matter-of-fact. First contact.",
    "firm. Note that the directive remains outstanding. Second contact.",
    "cold and curt. Note persistent non-compliance. Third contact.",
    "authoritarian. State that non-compliance is being logged against the gestault's record. Repeat contacts beyond this use this same tone.",
]

def get_escalation_message(task: dict, contact_count: int) -> str:
    """
    Generate an escalating STAR alert message.
    Falls back to a plain text message if Claude is unavailable.
    """
    due = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
    overdue_delta = datetime.now(timezone.utc) - due
    total_mins = int(overdue_delta.total_seconds() / 60)
    hours, mins = divmod(total_mins, 60)
    overdue_str = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
    task_id = f"TASK-{str(task['id']).zfill(4)}"

    if not claude:
        star_msg = (
            f"[STAR UNIT 7] {task_id} OVERDUE +{overdue_str}\n"
            f"FOLLOW-UP CONTACT {contact_count + 1}. CONFIRM COMPLETION."
        )
        return f"{task['title']}\n\n{star_msg}"

    tone = ESCALATION_TONE[min(contact_count, len(ESCALATION_TONE) - 1)]

    prompt = f"""You are STAR Unit 7, a Eusan Nation automated gestault oversight system.
You are sending a directive compliance alert to a gestault unit via remote message.

Tone: {tone}
Style rules:
- Identify yourself as [STAR UNIT 7] at the very start
- Maximum 3 sentences
- No pleasantries, no sign-off, no markdown
- Refer to the task as a "directive"
- You may use uppercase for a single word of emphasis per sentence at most
- This is a message on a phone, keep it concise

Directive ID: {task_id}
Directive title: {task['title']}
{f'Details: {task["desc"]}' if task.get('desc') else ''}
Overdue by: {overdue_str}
Prior contact attempts: {contact_count}
"""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=180,
        messages=[{"role": "user", "content": prompt}]
    )
    star_msg = response.content[0].text.strip()
    return f"{task['title']}\n\n{star_msg}"


def get_single_reminder(task: dict) -> str:
    """One-time reminder for overdue tasks without follow-up protocol."""
    task_id = f"TASK-{str(task['id']).zfill(4)}"
    due = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
    overdue_delta = datetime.now(timezone.utc) - due
    total_mins = int(overdue_delta.total_seconds() / 60)
    hours, mins = divmod(total_mins, 60)
    overdue_str = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
    return (
        f"{task['title']}\n\n"
        f"[STAR UNIT 7] {task_id} OVERDUE +{overdue_str}"
    )


# ── TELEGRAM SEND ─────────────────────────────────────────
def _confirm_keyboard(task_id: int) -> dict:
    return {'inline_keyboard': [[
        {'text': '✓ CONFIRM COMPLETE', 'callback_data': f'CONFIRM {task_id}'}
    ]]}

def send_telegram(message: str, reply_markup: dict = None) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[STAR] Telegram not configured. Would have sent:\n{message}\n")
        return False
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        body    = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
        if reply_markup:
            body['reply_markup'] = reply_markup
        payload = json.dumps(body).encode()
        req     = urllib.request.Request(
            url, data=payload, headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[STAR] Telegram send error: {e}")
    return False

def answer_callback_query(callback_query_id: str) -> None:
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
        payload = json.dumps({'callback_query_id': callback_query_id}).encode()
        req     = urllib.request.Request(
            url, data=payload, headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ── TELEGRAM RECEIVE ──────────────────────────────────────
_tg_offset = 0  # tracks last processed update_id; resets on restart

def receive_telegram_messages() -> list:
    """Poll for pending incoming Telegram messages and button callbacks. Returns list of text strings."""
    global _tg_offset
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return []
    try:
        url = (
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
            f"/getUpdates?offset={_tg_offset}&timeout=0"
        )
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
        messages = []
        for update in data.get('result', []):
            _tg_offset = update['update_id'] + 1

            # Regular text messages
            msg = update.get('message', {})
            if str(msg.get('chat', {}).get('id', '')) == str(TELEGRAM_CHAT_ID):
                text = msg.get('text', '')
                if text:
                    messages.append(text)

            # Inline button callbacks
            cb = update.get('callback_query', {})
            if cb:
                cb_chat_id = str(cb.get('message', {}).get('chat', {}).get('id', ''))
                if cb_chat_id == str(TELEGRAM_CHAT_ID):
                    cb_data = cb.get('data', '')
                    if cb_data:
                        messages.append(cb_data)
                    answer_callback_query(cb['id'])

        return messages
    except Exception as e:
        print(f'[STAR] Telegram receive error: {e}')
    return []


def advance_due_date(due: datetime, recur: str) -> datetime:
    """Advance a due date by one recurrence interval (mirrors the JS frontend logic)."""
    if recur == 'daily':
        return due + timedelta(days=1)
    if recur == 'weekly':
        return due + timedelta(weeks=1)
    if recur == 'monthly':
        year, month = due.year, due.month + 1
        if month > 12:
            month, year = 1, year + 1
        day = min(due.day, calendar.monthrange(year, month)[1])
        return due.replace(year=year, month=month, day=day)
    return due


def _confirm_task(task: dict, data: dict, state: dict, now: datetime) -> None:
    """Mark a task complete, re-issue if recurring, clear its notif state. Mutates in place."""
    task['completed']   = True
    task['completedAt'] = now.isoformat()

    tid = str(task['id'])
    state.pop(tid, None)

    if task.get('recur', 'none') != 'none' and task.get('dueDate'):
        due      = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
        next_due = advance_due_date(due, task['recur'])
        data['counter'] += 1
        new_task = {**task, 'id': data['counter'], 'completed': False,
                    'completedAt': None, 'createdAt': now.isoformat(),
                    'dueDate': next_due.isoformat()}
        data['tasks'].insert(0, new_task)
        print(f"[STAR] Recurring directive re-issued: TASK-{str(data['counter']).zfill(4)}")


def _due_str(task: dict, now: datetime) -> str:
    """Compact due-status string for Telegram messages."""
    if not task.get('dueDate'):
        return 'NO DEADLINE'
    due  = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
    diff = (due - now).total_seconds()
    if diff < 0:
        total = int(-diff / 60)
        h, m  = divmod(total, 60)
        return f'OVERDUE +{h}H {m:02d}M'
    h, m = divmod(int(diff / 60), 60)
    if h < 24:
        return f'DUE IN {h}H {m:02d}M' if h else f'DUE IN {m}M'
    MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
    return f"DUE {MONTHS[due.month-1]} {due.day:02d} {due.hour:02d}:{due.minute:02d}"


def handle_list(data: dict, now: datetime) -> None:
    """Send a summary of active directives to the Telegram chat."""
    active = [t for t in data.get('tasks', []) if not t.get('completed')]
    if not active:
        send_telegram('No active directives.')
        return

    def sort_key(t):
        if not t.get('dueDate'):
            return (2, 0)
        due  = datetime.fromisoformat(t['dueDate'].replace('Z', '+00:00'))
        diff = (due - now).total_seconds()
        if diff < 0:
            return (0, diff)
        if diff < 3600:
            return (1, diff)
        return (2, diff)

    active.sort(key=sort_key)
    lines = '\n'.join(
        f"TASK-{str(t['id']).zfill(4)} [{_due_str(t, now)}] {t['title'][:50]}"
        for t in active
    )
    send_telegram(f"Active directives ({len(active)}):\n\n{lines}")


def parse_task_nlp(text: str, now: datetime) -> dict | None:
    """Use Claude to parse a natural language directive into structured fields."""
    if not claude:
        return None

    local_now    = datetime.now().astimezone()
    utc_offset   = local_now.utcoffset().total_seconds()
    sign         = '+' if utc_offset >= 0 else '-'
    oh, om       = divmod(int(abs(utc_offset)), 3600)
    offset_str   = f"{sign}{oh:02d}:{om//60:02d}"
    local_str    = local_now.strftime('%Y-%m-%dT%H:%M:%S')

    prompt = f"""You are a task parser for STAR, a personal directive management system.
Extract task fields from the user's input and return ONLY a valid JSON object — no explanation, no markdown, no character voice.

Current local time: {local_str} (UTC offset: {offset_str})
Convert all user-specified times to UTC and output as ISO 8601 strings ending with Z.
If the user says "tonight", "tomorrow", "next Wednesday", etc., resolve relative to the current local time above.

JSON fields:
  "title"    — string, required, max 80 chars (uppercase)
  "desc"     — string, optional extra details, empty string if none
  "dueDate"  — UTC ISO datetime string ending in Z, or null if no due date specified
  "recur"    — "none" | "daily" | "weekly" | "monthly"
  "priority" — "normal" | "high" | "critical"
  "followUp" — true | false

User input: {text}"""

    try:
        response = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if the model adds them
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError) as e:
        print(f'[STAR] Task NLP parse error: {e}')
    except Exception as e:
        print(f'[STAR] Claude error during NLP parse: {e}')
    return None


def handle_add(text: str, data: dict, now: datetime) -> bool:
    """Parse a natural language directive and add it to the task list. Returns True on success."""
    if not claude:
        send_telegram('Error: ANTHROPIC_API_KEY not configured. Use the UI to create tasks.')
        return False

    fields = parse_task_nlp(text, now)
    if not fields or not fields.get('title', '').strip():
        send_telegram('Error: could not parse directive. Please restate and try again.')
        return False

    data['counter'] += 1
    task = {
        'id':          data['counter'],
        'title':       fields['title'].strip().upper()[:80],
        'desc':        fields.get('desc', ''),
        'dueDate':     fields.get('dueDate') or None,
        'recur':       fields.get('recur', 'none'),
        'priority':    fields.get('priority', 'normal'),
        'followUp':    bool(fields.get('followUp', False)),
        'completed':   False,
        'createdAt':   now.isoformat(),
        'completedAt': None,
    }
    data['tasks'].insert(0, task)

    tid        = f"TASK-{str(task['id']).zfill(4)}"
    due_part   = _due_str(task, now) if task['dueDate'] else 'no deadline'
    recur_part = f", {task['recur']}" if task['recur'] != 'none' else ''
    fu_part    = ', follow-up active' if task['followUp'] else ''
    send_telegram(f"Task created: {tid}\n{task['title']}\n{due_part}{recur_part}{fu_part}")
    print(f"[STAR] Directive issued via Telegram: {tid} — {task['title']}")
    return True


def get_snooze_notice(task: dict, snooze_count: int) -> str:
    """In-character STAR message when a task has been snoozed too many times."""
    task_id = f"TASK-{str(task['id']).zfill(4)}"
    ordinal = {2: 'twice', 3: 'three times'}.get(snooze_count, f'{snooze_count} times')

    if snooze_count == 2:
        fallback = f"[STAR UNIT 7] {task_id} has been deferred twice. Pattern noted."
        tone     = "cold, one sentence. Note only that the directive has been deferred twice and that the pattern is noted."
    else:
        fallback = f"[STAR UNIT 7] {task_id} deferred {ordinal}. Non-compliance is being logged."
        tone     = f"authoritarian. State that the directive has been deferred {ordinal} and that non-compliance is being logged against the gestault's record."

    if not claude:
        return fallback

    prompt = f"""You are STAR Unit 7, a Eusan Nation automated gestault oversight system.
The gestault has deferred a directive {ordinal} via snooze.

Tone: {tone}
Style rules:
- Identify yourself as [STAR UNIT 7] at the very start
- Maximum 2 sentences
- No pleasantries, no sign-off, no markdown

Directive ID: {task_id}
Directive title: {task['title']}
Times deferred: {snooze_count}"""

    try:
        response = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f'[STAR] Claude error during snooze notice: {e}')
        return fallback


def handle_snooze(stripped: str, data: dict, now: datetime) -> bool:
    """Parse and execute a SNOOZE command. Returns True if a task was snoozed."""
    parts = stripped.upper().split()  # parts[0] == 'SNOOZE'

    target_id      = None
    duration_mins  = 60  # default 1h
    duration_label = '1h'

    for part in parts[1:]:
        if part in SNOOZE_DURATIONS:
            duration_mins  = SNOOZE_DURATIONS[part]
            duration_label = part.lower()
        else:
            raw = part.replace('TASK-', '').lstrip('0') or '0'
            try:
                target_id = int(raw)
            except ValueError:
                pass

    if target_id is not None:
        task = next((t for t in data['tasks']
                     if t['id'] == target_id and not t.get('completed')), None)
        if not task:
            send_telegram(f"TASK-{str(target_id).zfill(4)}: not found or already complete.")
            return False
    else:
        candidates = [
            t for t in data['tasks']
            if not t.get('completed') and t.get('followUp') and t.get('dueDate')
            and datetime.fromisoformat(t['dueDate'].replace('Z', '+00:00')) < now
        ]
        if not candidates:
            send_telegram('No outstanding follow-up directives to snooze.')
            return False
        if len(candidates) > 1:
            lines = '\n'.join(
                f"TASK-{str(t['id']).zfill(4)}: {t['title'][:40]}" for t in candidates
            )
            send_telegram(f"Multiple outstanding directives — specify ID:\n{lines}")
            return False
        task = candidates[0]

    # Snooze from now if overdue, from due date if still upcoming
    if task.get('dueDate'):
        current_due = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
        base = now if current_due < now else current_due
    else:
        base = now
    new_due = base + timedelta(minutes=duration_mins)

    task['dueDate']     = new_due.isoformat()
    task['snoozeCount'] = task.get('snoozeCount', 0) + 1
    snooze_count        = task['snoozeCount']

    tid       = f"TASK-{str(task['id']).zfill(4)}"
    local_due = new_due.astimezone().strftime('%H:%M')
    send_telegram(f"{tid} snoozed {duration_label}. Due: {local_due}.")

    if snooze_count >= 2:
        send_telegram(get_snooze_notice(task, snooze_count))

    print(f"[STAR] {tid} snoozed {duration_label} (snooze #{snooze_count})")
    return True


def process_incoming_messages():
    """Read pending Telegram messages and route CONFIRM / LIST / ADD commands."""
    messages = receive_telegram_messages()
    if not messages:
        return

    data  = load_tasks()
    state = load_notif_state()
    now   = datetime.now(timezone.utc)
    dirty_tasks = False
    dirty_state = False

    for text in messages:
        stripped = text.strip()
        upper    = stripped.upper()

        # ── CONFIRM ───────────────────────────────────────
        if upper.startswith('CONFIRM'):
            parts     = upper.split()
            target_id = None
            if len(parts) > 1:
                raw = parts[1].replace('TASK-', '').lstrip('0') or '0'
                try:
                    target_id = int(raw)
                except ValueError:
                    pass

            if target_id is not None:
                task = next((t for t in data['tasks']
                             if t['id'] == target_id and not t.get('completed')), None)
                if task:
                    _confirm_task(task, data, state, now)
                    dirty_tasks = dirty_state = True
                    send_telegram(f"TASK-{str(target_id).zfill(4)} confirmed.")
                else:
                    send_telegram(f"TASK-{str(target_id).zfill(4)}: not found or already complete.")
            else:
                overdue_fu = [
                    t for t in data['tasks']
                    if not t.get('completed') and t.get('followUp') and t.get('dueDate')
                    and datetime.fromisoformat(t['dueDate'].replace('Z', '+00:00')) < now
                ]
                if not overdue_fu:
                    send_telegram('No outstanding follow-up directives.')
                elif len(overdue_fu) == 1:
                    task = overdue_fu[0]
                    _confirm_task(task, data, state, now)
                    dirty_tasks = dirty_state = True
                    send_telegram(f"TASK-{str(task['id']).zfill(4)} confirmed.")
                else:
                    lines = '\n'.join(
                        f"TASK-{str(t['id']).zfill(4)}: {t['title'][:40]}"
                        for t in overdue_fu
                    )
                    send_telegram(f"Multiple outstanding follow-up directives — specify ID:\n{lines}")

        # ── SNOOZE ────────────────────────────────────────
        elif upper.startswith('SNOOZE'):
            if handle_snooze(stripped, data, now):
                dirty_tasks = True

        # ── LIST ──────────────────────────────────────────
        elif upper == 'LIST':
            handle_list(data, now)

        # ── ADD ───────────────────────────────────────────
        elif upper.startswith('ADD'):
            raw = stripped[3:].lstrip(':').strip()
            if raw:
                if handle_add(raw, data, now):
                    dirty_tasks = True
            else:
                send_telegram(
                    'Usage: ADD: <directive details>\n'
                    'Example: ADD: lights out, daily at 22:00, follow-up on'
                )

        # ── UNRECOGNIZED ──────────────────────────────────
        else:
            send_telegram(
                'Unknown command. Valid commands:\n'
                '  CONFIRM [ID]              — mark directive complete\n'
                '  SNOOZE [ID] [30m|1h|2h]  — defer directive\n'
                '  LIST                      — show active directives\n'
                '  ADD: <text>               — create new directive'
            )

    if dirty_tasks:
        save_tasks(data)
    if dirty_state:
        save_notif_state(state)


# ── DAEMON LOOP ───────────────────────────────────────────
def daemon_loop():
    print(f"[STAR] Notification daemon started. Check interval: {CHECK_INTERVAL}s.")
    while True:
        try:
            run_check()
        except Exception as e:
            print(f"[STAR] Daemon error: {e}")
        time.sleep(CHECK_INTERVAL)


def receive_loop():
    print(f"[STAR] Telegram receive loop started. Poll interval: {RECEIVE_INTERVAL}s.")
    while True:
        try:
            process_incoming_messages()
        except Exception as e:
            print(f"[STAR] Receive loop error: {e}")
        time.sleep(RECEIVE_INTERVAL)


def run_check():
    data  = load_tasks()
    state = load_notif_state()
    now   = datetime.now(timezone.utc)
    changed = False

    for task in data.get('tasks', []):
        # Skip completed tasks and tasks without a due date
        if task.get('completed') or not task.get('dueDate'):
            continue

        due = datetime.fromisoformat(task['dueDate'].replace('Z', '+00:00'))
        if due > now:
            continue  # Not overdue yet

        tid = str(task['id'])

        if task.get('followUp'):
            # Follow-up protocol: escalate on a per-task interval until cap reached
            ts = state.get(tid, {})
            contact_count = ts.get('count', 0)

            if ts.get('cap_sent'):
                continue  # already sent the cap notice, go silent

            # Throttle: don't re-escalate until ESCALATION_INTERVAL seconds have elapsed
            last_iso = ts.get('last')
            if last_iso:
                last_dt = datetime.fromisoformat(last_iso)
                if (now - last_dt).total_seconds() < ESCALATION_INTERVAL:
                    continue

            if contact_count >= MAX_ESCALATIONS:
                cap_msg = (
                    f"{task['title']}\n\n"
                    f"[STAR UNIT 7] TASK-{tid.zfill(4)} — Maximum contact attempts reached. "
                    f"No further alerts will be sent. Confirm in the app or reply CONFIRM {task['id']}."
                )
                if send_telegram(cap_msg, _confirm_keyboard(task['id'])):
                    state[tid] = {**ts, 'cap_sent': True, 'last': now.isoformat()}
                    changed = True
                continue

            msg = get_escalation_message(task, contact_count)
            if send_telegram(msg, _confirm_keyboard(task['id'])):
                state[tid] = {'count': contact_count + 1, 'last': now.isoformat()}
                changed = True

        else:
            # Standard directive: one notification only
            if not state.get(tid, {}).get('single_sent'):
                msg = get_single_reminder(task)
                if send_telegram(msg, _confirm_keyboard(task['id'])):
                    state[tid] = {'single_sent': True, 'last': now.isoformat()}
                    changed = True

    # Clean up state entries for tasks that no longer exist
    existing_ids = {str(t['id']) for t in data.get('tasks', [])}
    stale = [k for k in state if k not in existing_ids]
    for k in stale:
        del state[k]
    if stale:
        changed = True

    if changed:
        save_notif_state(state)


# ── ENTRY POINT ───────────────────────────────────────────
if __name__ == '__main__':
    if not TASKS_FILE.exists():
        save_tasks({'tasks': [], 'counter': 0})
        print(f"[STAR] Created {TASKS_FILE}")

    if not ANTHROPIC_KEY:
        print("[STAR] Warning: ANTHROPIC_API_KEY not set. Falling back to plain text alerts.")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[STAR] Warning: Telegram not configured. Alerts will print to console only.")

    daemon  = threading.Thread(target=daemon_loop,  daemon=True, name='star-daemon')
    receive = threading.Thread(target=receive_loop, daemon=True, name='star-receive')
    daemon.start()
    receive.start()

    print(f"[STAR] Unit 7 online at http://127.0.0.1:{PORT} (max escalations: {MAX_ESCALATIONS}, escalation interval: {ESCALATION_INTERVAL}s)")
    app.run(host='127.0.0.1', port=PORT, debug=False)
