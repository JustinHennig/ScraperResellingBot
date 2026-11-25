# Stores improvement suggestions sent to the bot on Telegram: any text that
# isn't a command. A reply to a listing notification also keeps that listing
# (title, price, link, description and the notification text), so it's clear
# what the suggestion is about. Shown on the /feedback page.
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FEEDBACK_FILE = Path(__file__).parent.parent / "data" / "feedback.json"
_lock = threading.Lock()


def _load() -> list:
    if not FEEDBACK_FILE.exists():
        return []
    try:
        with FEEDBACK_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # Keep the broken file instead of overwriting every saved suggestion
        backup = FEEDBACK_FILE.with_name(f"feedback.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
        FEEDBACK_FILE.replace(backup)
        logger.error(f"{FEEDBACK_FILE} is corrupt ({exc}) — moved to {backup.name}, starting fresh")
        return []
    except OSError as exc:
        logger.warning(f"Could not read {FEEDBACK_FILE}: {exc}")
        return []


def _save(entries: list) -> None:
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FEEDBACK_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    tmp.replace(FEEDBACK_FILE)


# Saves a suggestion. listing is None or a dict like {"listing_id", "title",
# "price", "url", "search_name", "description", "message"}. Returns the entry.
def add_feedback(chat_id: str, text: str, listing: dict | None = None) -> dict:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "text": text,
        "listing": listing,
        "done": False,
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        _save(entries)
    return entry


# Marks a suggestion as done (implemented) or open again. Returns False if not found.
def set_done(feedback_id: str, done: bool) -> bool:
    with _lock:
        entries = _load()
        for entry in entries:
            if entry.get("id") == feedback_id:
                entry["done"] = done
                _save(entries)
                return True
    return False


# Returns all suggestions, newest first.
def all_feedback() -> list:
    with _lock:
        entries = _load()
    return sorted(entries, key=lambda e: e.get("created_at", ""), reverse=True)
