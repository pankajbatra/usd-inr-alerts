# USD/INR Telegram Rate Alert

Monitors USD/INR via Twelve Data, Mon–Fri 9:30 AM–3:30 PM IST, and sends a
Telegram message whenever the rate crosses above your high threshold (on **new
highs**) or below your low threshold (on **new lows**) — not on every poll and
not on revisits of a level already alerted.

## How the alert logic works

- Each day tracks a `threshold` (high) and `alerted_high`, plus a
  `low_threshold` and `alerted_low` (all start at `None`/day-reset).
- High: if `rate > threshold` AND (`alerted_high` is `None` OR `rate` has
  reached the **next 0.1 band** above the last alert): send alert, set
  `alerted_high = rate`.
- Low: if `low_threshold` is set (non-zero) AND `rate < low_threshold` AND
  (`alerted_low` is `None` OR `rate` has reached the **next 0.1 band** below
  the last alert): send alert, set `alerted_low = rate`.
- Otherwise: stay silent.
- **Band gating**: re-alerts ignore sub-band wiggles. A rate's band is
  `floor(rate / ALERT_GAP)`, where `ALERT_GAP` defaults to `0.05` (5 paise), so
  96.6047 and 96.6287 are both the 96.60 band. A re-alert fires only when the
  rate moves into a different band — a *higher* band for highs, a *lower* band
  for lows. So after a high alert in band 96.60 the next needs `rate >= 96.65`;
  after a low alert in band 95.20 the next needs the rate to drop below 95.20
  (any 95.15–95.19x or lower). The first alert of the day still fires as soon as
  the threshold is crossed, regardless of band.
- `alerted_high` and `alerted_low` reset automatically each new IST calendar day.
- Thresholds persist across days (so if you don't set new ones, yesterday's
  stay active) — fall back to `DEFAULT_THRESHOLD` / `DEFAULT_LOW_THRESHOLD`
  only if never set. `DEFAULT_LOW_THRESHOLD=0` disables low alerts.

Example (`ALERT_GAP=0.05`): high threshold 95.5 — rate first crosses at
96.6047 → alert; rises to 96.62 or 96.6487 → silent (same 96.60 band); rises to
96.65 → alert (reached 96.65 band). Low threshold 94 — rate first drops to
94.3877 → alert; dips to 94.37 or 94.355 → silent (same 94.35 band); drops to
94.34 → alert (moved into the 94.30 band).

## Setting the thresholds via Telegram

Message your bot any time:
```
/set 95.5      # high threshold (or just send a bare number like 95.5)
/setlow 94     # low threshold (0 disables low alerts)
```
It replies with confirmation and resets that side's day progress so the new
threshold takes effect cleanly. Also supports `/status` to check current
thresholds and the day's alerted high/low.

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

### 3. Deploy to your VPS
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
- **Log rotation**: `usdinr_alert.log` rotates weekly (Monday midnight) so it
  never grows unbounded. `LOG_BACKUP_COUNT` (default `1`) sets how many old
  weeks to keep; set it to `0` to clear the log each week with no history.

## Claude Raw Prompt

This has been created completely using Claude with the following raw prompt:

```
I want a utility script which sends me a Telegram message whenever the USD to INR conversion rate goes higher than a defined value (e.g. 95.5) anytime between 9:30 am and 3:30 pm IST, Monday to Friday.
It can fetch the rate every 1 or 2 minutes during this time period.

I have signed up for TwelveData and got the API key
This HTTP GET URL gives a JSON response and conversion rate: https://api.twelvedata.com/exchange_rate?symbol=USD/INR&apikey=<TWELVEDATA_API_KEY>

I have a VPS on DigitalOcean. I can deploy this utility on that, so that it keeps running in the background and sends me alerts on Telegram

Script won't send a telegram message if the rate is the same or lower than the rate for which an alert was already sent on the day.
Say, the defined rate is 95.5 and in the morning, the starting rate is 94.8; no alert is sent.
rate reaches this level at 10:30 AM, script sends a telegram message that the rate is now 95.5
At 10:35, it reaches 95.6, then the script again sends a telegram message that the rate is now 95.6
Now, if the rate drops to 95.55, then the script DO NOT send a telegram message. Now it will only send when the rate crosses the day's high of 95.6

Also, how can I set this defined value at the start of the day? Can I send a message on Telegram to set it? Say today I want the alert on a rate greater than 95.5, tomorrow on 94.5.
```
