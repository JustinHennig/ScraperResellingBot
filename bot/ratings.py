# Persists buy/no-buy ratings given to notified listings via the Telegram
# inline "👍 Kaufen" / "👎 Nicht kaufen" buttons. Keyed by listing id, then by
# the rating chat, so multiple recipients can rate the same listing independently.
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RATINGS_FILE = Path(__file__).parent.parent / "data" / "ratings.json"
_lock = threading.Lock()


def _load() -> dict:
    if not RATINGS_FILE.exists():
        return {}
    try:
        with RATINGS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # Move the broken file aside — otherwise the next _save() would replace
        # every stored rating with just the one being written.
        backup = RATINGS_FILE.with_name(f"ratings.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
        RATINGS_FILE.replace(backup)
        logger.error(f"{RATINGS_FILE} is corrupt ({exc}) — moved to {backup.name}, starting fresh")
        return {}
    except OSError as exc:
        logger.warning(f"Could not read {RATINGS_FILE}: {exc}")
        return {}


def _save(data: dict) -> None:
    RATINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RATINGS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(RATINGS_FILE)


# Records a newly notified listing so a later rating tap can be matched back to it.
def record_listing(listing_id: str, title: str, price, url: str, search_name: str) -> None:
    with _lock:
        data = _load()
        data[listing_id] = {
            "title": title,
            "price": price,
            "url": url,
            "search_name": search_name,
            "notified_at": datetime.now(timezone.utc).isoformat(),
            "ratings": data.get(listing_id, {}).get("ratings", {}),
        }
        _save(data)


# Records a chat's up/down rating for a listing. Returns the listing record,
# or None if the listing was never recorded (e.g. ratings.json was cleared).
def set_rating(listing_id: str, chat_id: str, rating: str) -> dict | None:
    with _lock:
        data = _load()
        record = data.get(listing_id)
        if record is None:
            return None
        record.setdefault("ratings", {})[chat_id] = {
            "rating": rating,
            "rated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(data)
        return record


# Returns every recorded listing keyed by listing id, each with its ratings
# (if any). Used by bot/ai_scorer.py to learn patterns from past 👍/👎 taps.
def all_records() -> dict:
    with _lock:
        return _load()
