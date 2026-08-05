#!/usr/bin/env python3
"""
USD/INR rate alert bot.

- Polls Twelve Data for USD/INR every POLL_INTERVAL_SECONDS, but only
  between MARKET_START and MARKET_END IST, Mon-Fri.
- Sends a Telegram message the first time the rate crosses the day's
  threshold, and again each time it makes a NEW day's high above the
  last alerted rate. Does not re-alert on drops or on revisits of a
  level already alerted.
- Listens (long-poll) for Telegram messages from you to set the day's
  threshold, e.g. send "/set 95.5" or just "95.5".
- State (threshold, day's alerted high, date) persisted to state.json
  so restarts don't lose today's progress.

Config is via environment variables (see .env.example / README).
"""

import os
import sys
import json
import math
import time
import logging
import signal
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests

# ---------- Configuration ----------

TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # your chat id, as string

# Default thresholds used if none set yet today (can be overridden via Telegram /set, /setlow)
DEFAULT_THRESHOLD = float(os.environ.get("DEFAULT_THRESHOLD", "95.5"))
DEFAULT_LOW_THRESHOLD = float(os.environ.get("DEFAULT_LOW_THRESHOLD", "0"))  # 0 = disabled

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "90"))  # 1.5 min default
TELEGRAM_POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "20"))  # long-poll seconds

IST = ZoneInfo("Asia/Kolkata")
MARKET_START = dtime(9, 30)
MARKET_END = dtime(15, 30)

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))
LOG_FILE = os.environ.get("LOG_FILE", os.path.join(os.path.dirname(__file__), "usdinr_alert.log"))

TWELVEDATA_URL = "https://api.twelvedata.com/exchange_rate"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("usdinr_alert")

# ---------- State ----------

def load_state():
    default = {
        "date": None,          # ISO date string, IST, for which thresholds/high/low apply
        "threshold": DEFAULT_THRESHOLD,
        "low_threshold": DEFAULT_LOW_THRESHOLD,  # alert when rate drops below this (0 = disabled)
        "alerted_high": None,  # highest rate already alerted today (None = no alert sent yet)
        "alerted_low": None,   # lowest rate already alerted today (None = no alert sent yet)
        "last_update_id": 0,   # telegram getUpdates offset
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
            default.update(loaded)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read state file (%s), starting fresh", e)
    return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def today_ist_str():
    return datetime.now(IST).date().isoformat()


def ensure_today(state):
    """Reset the day's alerted_high if we've rolled over to a new day.
    Threshold persists across days unless explicitly changed via Telegram,
    UNLESS this is a fresh day with no threshold set for it yet, in which
    case we fall back to DEFAULT_THRESHOLD."""
    today = today_ist_str()
    if state["date"] != today:
        log.info("New trading day detected (%s). Resetting day's alerted high/low.", today)
        state["date"] = today
        state["alerted_high"] = None
        state["alerted_low"] = None
        # Keep whatever threshold was last set; if you want a fresh default
        # each day instead, uncomment the next line:
        # state["threshold"] = DEFAULT_THRESHOLD
    return state


# ---------- Telegram ----------

def send_telegram_message(text):
    url = f"{TELEGRAM_API}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        r.raise_for_status()
        log.info("Telegram message sent: %s", text)
    except requests.RequestException as e:
        log.error("Failed to send Telegram message: %s", e)


def poll_telegram_commands(state):
    """Long-poll for new messages, process any /set commands from the
    authorized chat id. Returns updated state."""
    url = f"{TELEGRAM_API}/getUpdates"
    params = {
        "offset": state["last_update_id"] + 1,
        "timeout": TELEGRAM_POLL_TIMEOUT,
        "allowed_updates": json.dumps(["message"]),
    }
    try:
        r = requests.get(url, params=params, timeout=TELEGRAM_POLL_TIMEOUT + 10)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("Telegram getUpdates failed: %s", e)
        return state
    except ValueError:
        log.warning("Telegram getUpdates returned non-JSON")
        return state

    if not data.get("ok"):
        return state

    for update in data.get("result", []):
        state["last_update_id"] = update["update_id"]
        msg = update.get("message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if chat_id != str(TELEGRAM_CHAT_ID):
            log.info("Ignoring message from unauthorized chat_id=%s", chat_id)
            continue

        new_low = parse_setlow_command(text)
        new_threshold = None if new_low is not None else parse_set_command(text)
        if new_low is not None:
            state = ensure_today(state)
            state["low_threshold"] = new_low
            state["alerted_low"] = None  # new low threshold => reset today's low alert progress
            save_state(state)
            if new_low > 0:
                send_telegram_message(
                    f"✅ Low threshold set to {new_low} for {state['date']}. "
                    f"You'll be alerted when USD/INR drops below this."
                )
            else:
                send_telegram_message(f"✅ Low-threshold alerts disabled for {state['date']}.")
            log.info("Low threshold updated via Telegram to %s", new_low)
        elif new_threshold is not None:
            state = ensure_today(state)
            state["threshold"] = new_threshold
            state["alerted_high"] = None  # new threshold => reset today's alert progress
            save_state(state)
            send_telegram_message(
                f"✅ Threshold set to {new_threshold} for {state['date']}. "
                f"You'll be alerted when USD/INR crosses this."
            )
            log.info("Threshold updated via Telegram to %s", new_threshold)
        elif text.lower() in ("/status", "status"):
            send_status(state)
        elif text.startswith("/"):
            send_telegram_message(
                "Commands:\n"
                "/set <rate>     high threshold, e.g. /set 95.5\n"
                "/setlow <rate>  low threshold, e.g. /setlow 94 (0 to disable)\n"
                "/status         show current thresholds & day's high/low"
            )

    return state


def parse_set_command(text):
    """Accepts '/set 95.5', 'set 95.5', or a bare number like '95.5'."""
    text = text.strip()
    lowered = text.lower()
    if lowered.startswith("/set"):
        rest = text[4:].strip()
    elif lowered.startswith("set"):
        rest = text[3:].strip()
    else:
        rest = text
    try:
        value = float(rest)
        if 30 <= value <= 200:  # sanity bound for USD/INR
            return value
    except ValueError:
        pass
    return None


def parse_setlow_command(text):
    """Accepts '/setlow 94' or 'setlow 94'. A value of 0 disables low alerts."""
    text = text.strip()
    lowered = text.lower()
    if lowered.startswith("/setlow"):
        rest = text[7:].strip()
    elif lowered.startswith("setlow"):
        rest = text[6:].strip()
    else:
        return None
    try:
        value = float(rest)
        if value == 0 or (30 <= value <= 200):  # 0 disables; else sanity bound for USD/INR
            return value
    except ValueError:
        pass
    return None


def send_status(state):
    low = state.get("low_threshold", 0)
    send_telegram_message(
        f"📊 Status ({state['date']}):\n"
        f"High threshold: {state['threshold']}\n"
        f"Low threshold: {low if low else 'disabled'}\n"
        f"Day's alerted high: {state['alerted_high'] if state['alerted_high'] is not None else 'none yet'}\n"
        f"Day's alerted low: {state['alerted_low'] if state['alerted_low'] is not None else 'none yet'}"
    )


# ---------- Rate fetching ----------

def fetch_usdinr_rate():
    params = {"symbol": "USD/INR", "apikey": TWELVEDATA_API_KEY}
    r = requests.get(TWELVEDATA_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "rate" not in data:
        raise ValueError(f"Unexpected response from Twelve Data: {data}")
    return float(data["rate"])


# ---------- Alert-band helpers ----------
#
# We only re-alert when the new extreme reaches the NEXT 0.1 band, not on tiny
# moves in the second decimal. The band of a rate is floor(rate*10)/10, e.g.
# 96.6047 and 96.6887 both sit in the 96.6 band. After alerting inside the 96.6
# band, the next high alert requires rate >= 96.7; the next low alert (from,
# say, a 94.3 band) requires rate <= 94.2.

def _band_tenths(rate):
    return math.floor(rate * 10 + 1e-9)  # integer number of 0.1 units, float-safe


def next_high_needed(alerted_high):
    return (_band_tenths(alerted_high) + 1) / 10.0


def next_low_needed(alerted_low):
    return (_band_tenths(alerted_low) - 1) / 10.0


# ---------- Market hours check ----------

def within_market_hours(now_ist):
    if now_ist.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return MARKET_START <= now_ist.time() <= MARKET_END


# ---------- Main loop ----------

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s, shutting down gracefully...", signum)
    _shutdown = True


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = load_state()
    state = ensure_today(state)
    save_state(state)

    log.info("USD/INR alert bot started. Threshold=%s, poll interval=%ss",
              state["threshold"], POLL_INTERVAL_SECONDS)
    low_threshold = state.get("low_threshold", 0)
    send_telegram_message(
        f"🤖 USD/INR alert bot started.\n"
        f"High threshold: {state['threshold']}\n"
        f"Low threshold: {low_threshold if low_threshold else 'disabled'}\n"
        f"Send /set <rate> or /setlow <rate> anytime to change them."
    )

    last_rate_check = 0

    while not _shutdown:
        state = ensure_today(state)

        # Always listen for Telegram commands (long-poll doubles as our sleep)
        state = poll_telegram_commands(state)
        save_state(state)

        now_ist = datetime.now(IST)
        if not within_market_hours(now_ist):
            continue  # getUpdates long-poll already paced us; loop back

        now_ts = time.time()
        if now_ts - last_rate_check < POLL_INTERVAL_SECONDS:
            continue
        last_rate_check = now_ts

        try:
            rate = fetch_usdinr_rate()
        except (requests.RequestException, ValueError) as e:
            log.error("Failed to fetch rate: %s", e)
            continue

        low_threshold = state.get("low_threshold", 0)
        log.info("USD/INR = %s (threshold=%s, low_threshold=%s, alerted_high=%s, alerted_low=%s)",
                  rate, state["threshold"], low_threshold,
                  state["alerted_high"], state["alerted_low"])

        if rate > state["threshold"]:
            if state["alerted_high"] is None or rate >= next_high_needed(state["alerted_high"]):
                send_telegram_message(
                    f"🚨 USD/INR is now {rate:.4f} (above threshold {state['threshold']})"
                )
                state["alerted_high"] = rate
                save_state(state)

        if low_threshold and rate < low_threshold:
            if state["alerted_low"] is None or rate <= next_low_needed(state["alerted_low"]):
                send_telegram_message(
                    f"🔻 USD/INR is now {rate:.4f} (below threshold {low_threshold})"
                )
                state["alerted_low"] = rate
                save_state(state)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
