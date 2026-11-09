# ResellingBot — Web Server
# Runs the bot in a background thread and exposes a small REST API
# so the frontend can start/stop the bot and change the interval.
# Usage: python server.py, then open http://localhost:5000 in your browser.

import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, jsonify, render_template, request

from bot.main import (
    CONFIG_FILE,
    config_without_credentials,
    load_config,
    load_seen_listings,
    run_all_searches,
    setup_logging,
)
from bot.notifier import send_telegram_message
from bot.ratings import all_records, set_rating

app = Flask(
    __name__,
    template_folder="frontend",
    static_folder="frontend",
    static_url_path="/static",
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {"running": False, "interval": 5}   # interval in minutes
_stop_event = threading.Event()
_bot_thread = None
_seen: set = set()
_config: dict = {}
_interval_overridden: bool = False  # True when interval was set via API rather than config
_next_run_at: float = 0.0           # Unix timestamp of the next scheduled search cycle

# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

# Background thread that reloads config, runs all searches, then sleeps until the next cycle.
def _bot_loop() -> None:
    global _config, _next_run_at
    logger = logging.getLogger("server")
    while not _stop_event.is_set():
        # Reload config before each cycle so price/keyword/search changes take effect without restart.
        # Falls back to the last valid config if config.json is malformed.
        try:
            fresh = load_config()
            with _lock:
                _config = fresh
                if not _interval_overridden:
                    _state["interval"] = fresh.get("settings", {}).get(
                        "check_interval_minutes", _state["interval"]
                    )
            logger.debug("Config reloaded.")
        except Exception as exc:
            logger.warning(f"Config reload failed — using last valid config: {exc}")

        # Signal that a run is in progress (no countdown while searching)
        _next_run_at = 0.0

        with _lock:
            config_snapshot = _config

        try:
            run_all_searches(config_snapshot, _seen, _stop_event)
        except Exception as exc:
            logger.error(f"Unhandled error in bot loop: {exc}", exc_info=True)

        if _stop_event.is_set():
            break

        # Sleep for 'interval' minutes in 10-second chunks so we can stop quickly.
        interval_seconds = _state["interval"] * 60
        _next_run_at = time.time() + interval_seconds
        elapsed = 0
        while elapsed < interval_seconds and not _stop_event.is_set():
            chunk = min(10, interval_seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    _next_run_at = 0.0
    with _lock:
        _state["running"] = False

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Serves the main UI page.
@app.route("/")
def index():
    return render_template("index.html")


# Serves the log viewer page.
@app.route("/log")
def log_page():
    return render_template("log.html")


# Serves the ratings overview page.
@app.route("/ratings")
def ratings_page():
    return render_template("ratings.html")


# Returns current bot state: running flag, check interval, and seconds until the next run.
@app.route("/api/status")
def api_status():
    with _lock:
        data = dict(_state)
    secs = max(0, int(_next_run_at - time.time())) if _next_run_at else None
    data["next_run_in"] = secs  # seconds until next run, None while actively searching
    return jsonify(data)


# Starts the bot loop in a background thread. Returns False if already running.
def _start_bot() -> bool:
    global _bot_thread
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True

    _stop_event.clear()
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True, name="bot-loop")
    _bot_thread.start()
    return True


# Signals the bot loop to stop. Returns False if not running.
def _stop_bot() -> bool:
    with _lock:
        if not _state["running"]:
            return False

    _stop_event.set()
    return True


# Starts the bot loop in a background thread. Returns 409 if already running.
@app.route("/api/start", methods=["POST"])
def api_start():
    if not _start_bot():
        return jsonify({"ok": False, "error": "Already running"}), 409
    return jsonify({"ok": True})


# Signals the bot loop to stop. Returns 409 if not running.
@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not _stop_bot():
        return jsonify({"ok": False, "error": "Not running"}), 409
    return jsonify({"ok": True})


# Updates the check interval (minutes). Overrides the value from config.json for this session.
@app.route("/api/interval", methods=["POST"])
def api_interval():
    global _interval_overridden
    data = request.get_json(silent=True) or {}
    try:
        interval = int(data.get("interval", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "interval must be a number"}), 400

    if interval < 1:
        return jsonify({"ok": False, "error": "Interval must be at least 1 minute"}), 400

    with _lock:
        _state["interval"] = interval
        _interval_overridden = True  # Don't let config reload overwrite this

    return jsonify({"ok": True, "interval": interval})


# Returns the list of configured searches with name, min/max price, and enabled state.
@app.route("/api/searches")
def api_searches():
    with _lock:
        searches = _config.get("searches", [])
    result = [
        {
            "name": s.get("name", s.get("query", "?")),
            "min_price": s.get("min_price", 0),
            "max_price": s.get("max_price", 0),
            "enabled": s.get("enabled", True),
        }
        for s in searches
    ]
    return jsonify(result)


# Write _config back to config.json atomically. Must be called under _lock.
# Credentials merged in from credentials.json are left out so they never end up in config.json.
def _save_config() -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(config_without_credentials(_config), f, indent=2, ensure_ascii=False)
    try:
        tmp.replace(CONFIG_FILE)
    except PermissionError:
        import shutil
        shutil.copy2(tmp, CONFIG_FILE)
        tmp.unlink(missing_ok=True)


# Toggles the enabled state of a search by name and persists the change to config.json.
@app.route("/api/searches/toggle", methods=["POST"])
def api_searches_toggle():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    with _lock:
        new_state = None
        for s in _config.get("searches", []):
            if s.get("name") == name:
                s["enabled"] = not s.get("enabled", True)
                new_state = s["enabled"]
                break
        if new_state is None:
            return jsonify({"ok": False, "error": "Search not found"}), 404
        try:
            _save_config()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "name": name, "enabled": new_state})


# Updates min_price and/or max_price for a search by name and persists the change to config.json.
@app.route("/api/searches/price", methods=["POST"])
def api_searches_price():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    updates = {}
    for field in ("min_price", "max_price"):
        if field in data:
            try:
                val = int(data[field])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{field} must be a number"}), 400
            if val < 0:
                return jsonify({"ok": False, "error": f"{field} must be ≥ 0"}), 400
            updates[field] = val

    if not updates:
        return jsonify({"ok": False, "error": "min_price or max_price required"}), 400

    with _lock:
        found = False
        for s in _config.get("searches", []):
            if s.get("name") == name:
                s.update(updates)
                found = True
                break
        if not found:
            return jsonify({"ok": False, "error": "Search not found"}), 404
        try:
            _save_config()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "name": name, **updates})


# Adds a new search entry and persists it to config.json.
@app.route("/api/searches/add", methods=["POST"])
def api_searches_add():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400

    query = (data.get("query") or "").strip() or name.lower()

    try:
        min_price = int(data.get("min_price") or 0)
        max_price = int(data.get("max_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "prices must be numbers"}), 400

    if min_price < 0 or max_price < 0:
        return jsonify({"ok": False, "error": "prices must be ≥ 0"}), 400

    with _lock:
        for s in _config.get("searches", []):
            if s.get("name") == name:
                return jsonify({"ok": False, "error": "A search with that name already exists"}), 409

        new_search = {
            "name": name,
            "query": query,
            "min_price": min_price,
            "max_price": max_price,
            "enabled": True,
        }
        _config.setdefault("searches", []).append(new_search)
        try:
            _save_config()
        except Exception as exc:
            _config["searches"].pop()
            return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "search": new_search})


# Clears the seen listings in memory and on disk so the bot re-evaluates all listings.
# If the bot is running it is stopped first, the set is cleared, then the bot
# is restarted — this prevents a concurrent save_seen_listings call from
# immediately re-writing the cleared file with in-flight IDs.
@app.route("/api/seen/clear", methods=["POST"])
def api_seen_clear():
    global _bot_thread, _seen
    seen_file = _config.get("settings", {}).get("seen_listings_file", "seen_listings.json")
    seen_path = CONFIG_FILE.parent / seen_file

    was_running = False
    with _lock:
        was_running = _state.get("running", False)

    # Stop the bot so no in-flight search can overwrite the file after we clear it
    if was_running:
        _stop_event.set()
        if _bot_thread:
            _bot_thread.join(timeout=60)
            # Restarting while the old loop is still finishing would leave two loops running
            if _bot_thread.is_alive():
                return jsonify({"ok": False, "error": "Bot is still stopping — please try again in a moment"}), 409

    with _lock:
        _seen.clear()
        _state["running"] = False

    try:
        seen_path.write_text("{}", encoding="utf-8")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    # Restart the bot if it was running before
    if was_running:
        _stop_event.clear()
        with _lock:
            _state["running"] = True
        _bot_thread = threading.Thread(target=_bot_loop, daemon=True, name="bot-loop")
        _bot_thread.start()

    return jsonify({"ok": True})


# Returns the last N lines of the bot log file.
@app.route("/api/logs")
def api_logs():
    lines = int(request.args.get("lines", 100))
    log_file = _config.get("settings", {}).get("log_file", "logs/bot.log")
    log_path = CONFIG_FILE.parent / log_file
    if not log_path.exists():
        return jsonify({"lines": []})
    try:
        with log_path.open(encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Returns every notified listing with its 👍/👎 ratings, newest first.
@app.route("/api/ratings")
def api_ratings():
    records = []
    for listing_id, rec in all_records().items():
        ratings = rec.get("ratings", {})
        latest = max(ratings.values(), key=lambda r: r.get("rated_at", ""), default=None)
        records.append({
            "listing_id": listing_id,
            "title": rec.get("title", ""),
            "price": rec.get("price"),
            "url": rec.get("url", ""),
            "search_name": rec.get("search_name", ""),
            "notified_at": rec.get("notified_at", ""),
            "rating": latest.get("rating") if latest else None,
            "rated_at": latest.get("rated_at") if latest else None,
        })
    records.sort(key=lambda r: r["notified_at"], reverse=True)
    return jsonify({"records": records})


# Clears the bot log file.
@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    log_file = _config.get("settings", {}).get("log_file", "logs/bot.log")
    log_path = CONFIG_FILE.parent / log_file
    try:
        log_path.write_text("", encoding="utf-8")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Telegram polling — lets a recognised chat control the bot by texting it
# (stop/start/status), instead of needing the web dashboard. Uses long
# polling (getUpdates) rather than a webhook, so no public HTTPS endpoint
# is required — works fine behind a home router / NAT.
# ---------------------------------------------------------------------------

_COMMANDS_HELP = (
    "Befehle:\n"
    "• stop / pause — Bot anhalten\n"
    "• start / weiter — Bot fortsetzen\n"
    "• status — aktueller Zustand\n"
    "• liste — alle Suchen mit Preis & Status\n"
    "• aktivieren <name> — Suche einschalten\n"
    "• deaktivieren <name> — Suche ausschalten\n"
    "• preis <name> <min> <max> — Preisspanne setzen"
)


def _allowed_chat_ids(tg_config: dict) -> set[str]:
    raw = tg_config.get("chat_id", [])
    ids = raw if isinstance(raw, list) else [raw]
    return {str(c) for c in ids if c}


# Case-insensitive lookup of a search entry by name. Must be called under _lock.
def _find_search(name: str) -> Optional[dict]:
    name_low = name.strip().lower()
    for s in _config.get("searches", []):
        if s.get("name", "").lower() == name_low:
            return s
    return None


# Renders the current searches as a Telegram-friendly list.
def _format_search_list() -> str:
    with _lock:
        searches = _config.get("searches", [])
        lines = [
            f"{'✅' if s.get('enabled', True) else '⏸️'} {s.get('name', '?')}: "
            f"{s.get('min_price', 0)}–{s.get('max_price', 0)} EUR"
            for s in searches
        ]
    return "\n".join(lines) if lines else "Keine Suchen konfiguriert."


# Handles a tap on a listing's 👍/👎 rating button: records the rating,
# dismisses the loading spinner on the sender's side, and replaces the
# buttons with the confirmed choice so it can't be tapped twice.
def _handle_rating_callback(token: str, callback_query: dict, allowed: set[str]) -> None:
    cq_id = callback_query.get("id")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    message_id = callback_query.get("message", {}).get("message_id")
    data_str = callback_query.get("data", "")

    if chat_id not in allowed or not data_str.startswith("rate:"):
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                       json={"callback_query_id": cq_id}, timeout=10)
        return

    _, listing_id, rating = data_str.split(":", 2)
    record = set_rating(listing_id, chat_id, rating)
    label = "👍 Kaufen" if rating == "up" else "👎 Nicht kaufen"

    requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": cq_id, "text": f"Gespeichert: {label}"},
        timeout=10,
    )
    if record is not None:
        requests.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[{"text": f"✅ {label}", "callback_data": "noop"}]]},
            },
            timeout=10,
        )


# Background loop: long-polls Telegram for new messages and reacts to
# stop/start/status commands from chat ids listed in telegram.chat_id.
# Runs for the lifetime of the process, independent of whether the bot
# loop itself is running.
def _telegram_poll_loop() -> None:
    logger = logging.getLogger("server")
    offset = None

    while True:
        tg_config = _config.get("telegram", {})
        token = tg_config.get("bot_token")
        if not token:
            time.sleep(30)
            continue

        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=35,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(f"Telegram polling failed: {exc}")
            time.sleep(10)
            continue

        if not data.get("ok"):
            logger.warning(f"Telegram getUpdates error: {data}")
            time.sleep(10)
            continue

        allowed = _allowed_chat_ids(tg_config)

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            # One bad update (network hiccup while replying, malformed callback data)
            # must not kill this thread — commands and rating buttons would stop
            # working until the next restart.
            try:
                _handle_update(token, update, allowed)
            except Exception:
                logger.exception(f"Failed to handle Telegram update {update.get('update_id')}")


# Reacts to a single Telegram update: a rating button tap or a text command.
def _handle_update(token: str, update: dict, allowed: set[str]) -> None:
    if "callback_query" in update:
        _handle_rating_callback(token, update["callback_query"], allowed)
        return

    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip().lower()

    if not text or chat_id not in allowed:
        return

    reply_config = {"bot_token": token, "chat_id": [chat_id]}

    if text in ("stop", "pause", "aus", "halt"):
        if _stop_bot():
            send_telegram_message(reply_config, "🛑 Bot gestoppt. Schreib 'start', um ihn wieder zu starten.")
        else:
            send_telegram_message(reply_config, "Der Bot lief schon nicht.")
    elif text in ("start", "weiter", "an", "los"):
        if _start_bot():
            send_telegram_message(reply_config, "▶️ Bot gestartet.")
        else:
            send_telegram_message(reply_config, "Der Bot lief schon.")
    elif text == "status":
        with _lock:
            running = _state["running"]
            interval = _state["interval"]
        state_str = "läuft" if running else "gestoppt"
        send_telegram_message(reply_config, f"Status: {state_str} (Intervall: {interval} Min.)")
    elif text == "liste":
        send_telegram_message(reply_config, _format_search_list())
    elif text.startswith("aktivieren "):
        name = text[len("aktivieren "):].strip()
        with _lock:
            s = _find_search(name)
            if s is None:
                reply = f"Suche '{name}' nicht gefunden."
            else:
                s["enabled"] = True
                try:
                    _save_config()
                    reply = f"✅ '{s['name']}' aktiviert."
                except Exception as exc:
                    reply = f"Fehler beim Speichern: {exc}"
        send_telegram_message(reply_config, reply)
    elif text.startswith("deaktivieren "):
        name = text[len("deaktivieren "):].strip()
        with _lock:
            s = _find_search(name)
            if s is None:
                reply = f"Suche '{name}' nicht gefunden."
            else:
                s["enabled"] = False
                try:
                    _save_config()
                    reply = f"⏸️ '{s['name']}' deaktiviert."
                except Exception as exc:
                    reply = f"Fehler beim Speichern: {exc}"
        send_telegram_message(reply_config, reply)
    elif text.startswith("preis "):
        parts = text[len("preis "):].rsplit(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            send_telegram_message(reply_config, "Format: preis <name> <min> <max>")
        else:
            name, min_str, max_str = parts
            with _lock:
                s = _find_search(name)
                if s is None:
                    reply = f"Suche '{name}' nicht gefunden."
                else:
                    s["min_price"] = int(min_str)
                    s["max_price"] = int(max_str)
                    try:
                        _save_config()
                        reply = f"💰 '{s['name']}': {min_str}–{max_str} EUR"
                    except Exception as exc:
                        reply = f"Fehler beim Speichern: {exc}"
            send_telegram_message(reply_config, reply)
    else:
        send_telegram_message(reply_config, _COMMANDS_HELP)


if __name__ == "__main__":
    _config = load_config()
    settings = _config.get("settings", {})
    setup_logging(settings.get("log_file", "bot.log"))

    _seen = load_seen_listings(settings.get("seen_listings_file", "seen_listings.json"))
    _state["interval"] = settings.get("check_interval_minutes", 5)

    logger = logging.getLogger("server")
    logger.info("Starting web server — open http://localhost:5000")

    threading.Thread(target=_telegram_poll_loop, daemon=True, name="telegram-poll").start()

    # Start searching right away so a reboot, power cut or service restart doesn't
    # leave the bot silently stopped until someone sends "start".
    if settings.get("autostart", True):
        _start_bot()
        logger.info("Bot started automatically (settings.autostart)")

    app.run(host="0.0.0.0", port=5000, debug=False)
