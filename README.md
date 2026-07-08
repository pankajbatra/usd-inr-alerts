# USD/INR Telegram Rate Alert

Monitors USD/INR via Twelve Data, Mon–Fri 9:30 AM–3:30 PM IST, and sends a
Telegram message whenever the rate crosses above your threshold — but only
on **new highs**, not on every poll and not on drops-then-recovers-to-same-level.

## How the alert logic works

- Each day tracks a `threshold` and `alerted_high` (starts at `None`).
- If `rate > threshold` AND (`alerted_high` is `None` OR `rate > alerted_high`):
  send alert, set `alerted_high = rate`.
- Otherwise: stay silent.
- `alerted_high` resets automatically each new IST calendar day.
- `threshold` persists across days (so if you don't set a new one, yesterday's
  stays active) — falls back to `DEFAULT_THRESHOLD` only if never set.

Example matching your spec: threshold 95.5, rate hits 95.5 → alert. Rises to
95.6 → alert. Drops to 95.55 → silent (below day's high of 95.6). Rises to
95.65 → alert (new high).

## Setting the threshold via Telegram

Message your bot any time:
```
/set 95.5
```
or just `95.5`. It replies with confirmation and resets the day's "alerted
high" so the new threshold takes effect cleanly. Also supports `/status` to
check current threshold and day's high.

You don't have to send this every day — if you don't, the previous value
carries over. If you want a hard requirement to set it fresh each morning,
say so and I'll change `ensure_today()` to fall back to `DEFAULT_THRESHOLD`
every new day instead of carrying over.

## One-time setup

### 1. Create a Telegram bot
- Message **@BotFather** on Telegram → `/newbot` → follow prompts → copy the
  **bot token**.

### 2. Get your chat ID
- Message your new bot anything (e.g. "hi").
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
- Find `"chat":{"id": 123456789, ...}` — that number is your `TELEGRAM_CHAT_ID`.

### 3. Deploy to your DigitalOcean VPS
```bash
ssh you@your-vps-ip
sudo mkdir -p /opt/usdinr-alert
sudo chown $USER:$USER /opt/usdinr-alert
cd /opt/usdinr-alert
# copy usdinr_alert.py, requirements.txt, usdinr-alert.service, .env.example here
# (scp from your machine, or git clone if you push this to a repo)

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate

cp .env.example .env
nano .env   # fill in TWELVEDATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

### 4. Run as a systemd service (auto-restart, survives reboot)
```bash
sudo cp usdinr-alert.service /etc/systemd/system/
sudo nano /etc/systemd/system/usdinr-alert.service   # set User=you (your linux username)
sudo systemctl daemon-reload
sudo systemctl enable usdinr-alert
sudo systemctl start usdinr-alert
sudo systemctl status usdinr-alert
```

Logs:
```bash
journalctl -u usdinr-alert -f
# or
tail -f /opt/usdinr-alert/usdinr_alert.log
```

## Notes / things worth knowing

- **API rate limits**: Twelve Data's free tier is 8 requests/minute, 800/day.
  At a 90-second poll interval over the 6-hour window (9:30–15:30) that's
  ~240 calls/day for the rate itself — fine. If you drop to 1-minute polling
  it's ~360/day, still fine. Telegram's `getUpdates` long-poll doesn't count
  against Twelve Data's limit at all.
- **Outside market hours**: the script keeps running (so it can still receive
  your `/set` commands and reply to `/status`), it just skips rate fetching
  outside 9:30–15:30 IST or on weekends.
- **Restart safety**: state (threshold + today's alerted high) is saved to
  `state.json` after every change, so a VPS reboot or crash mid-day won't
  lose your progress or cause a duplicate/missed alert logic reset.
- **Timezone**: uses `Asia/Kolkata` via Python's `zoneinfo` — no dependency
  on the VPS's system timezone.
