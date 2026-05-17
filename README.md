# STAR
A Signalis-themed task manager
# STAR Unit 7 — Setup Instructions

## Directory structure

Put all files in one folder, e.g. `/home/youruser/star/`:

    star/
    ├── star_server.py
    ├── star.html
    ├── requirements.txt
    ├── .env             ← created from .env.example
    ├── tasks.json       ← created automatically on first run
    └── notifications.json

---

## 1. Python environment

    cd ~/star
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

---

## 2. Config

    nano .env            # fill in your API key and Telegram credentials
    chmod 600 .env       # restrict read access to your user only

---

## 3. Set up Telegram bot

1. Open Telegram and message @BotFather.
2. Send `/newbot` and follow the prompts to create a bot.
3. Copy the token BotFather gives you into `STAR_TELEGRAM_TOKEN` in `.env`.
4. Start a private chat with your new bot (search its username and press Start),
   or add it to a group where you want to receive alerts.
5. Get your chat ID:
   - Send any message to the bot (or in the group).
   - Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   - Find `"chat":{"id":...}` in the response. That number is your chat ID.
   - For groups the ID is negative (e.g. `-1001234567890`).
6. Paste the chat ID into `STAR_TELEGRAM_CHAT_ID` in `.env`.

Test that it works:

    curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=STAR+ONLINE"

---

## 4. Test the server manually

    source venv/bin/activate
    python star_server.py

Open a browser and go to:
    http://127.0.0.1:7823

If Telegram is not yet configured, alerts will print to the terminal console.

---

## 5. (Linux) Install as a systemd service (runs on boot, always-on)

Edit star.service — replace YOUR_USERNAME and the path with your actual values.

    sudo cp star.service /etc/systemd/system/star.service
    sudo systemctl daemon-reload
    sudo systemctl enable star
    sudo systemctl start star

Check it's running:
    sudo systemctl status star

Watch live logs:
    journalctl -u star -f

On macOS or Windows, run `python star_server.py` manually or set up an equivalent startup task.

---

## 6. Bookmark it

Navigate to http://127.0.0.1:7823 and bookmark it.
The SERVER status indicator in the bottom bar will show CONNECTED when
the Python server is reachable, or OFFLINE if the service is down.

---

## Notes

- tasks.json and notifications.json are plain text — back them up if needed.
- The daemon checks for overdue tasks every STAR_CHECK_INTERVAL seconds (default 15m).
- Follow-up tasks get an escalating Claude-generated message every check interval
  until marked complete in the UI.
- Standard overdue tasks (no follow-up toggle) get exactly one Telegram notification.
- Completed and purged tasks are automatically cleaned from notification state.
- To adjust Claude's tone, edit the ESCALATION_TONE list in star_server.py.
