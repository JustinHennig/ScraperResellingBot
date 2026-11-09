# Orchestration layer: loads config and seen listings, runs all searches in parallel,
# and coordinates notifications via Telegram.
# Configuration is read from config.json (see bot/scraper.py for scraping logic,
# bot/notifier.py for Telegram delivery).

import json
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional

import requests
import schedule

from bot.notifier import notify_new_listing, send_startup_message, validate_telegram_config
from bot.scraper import (
    Listing,
    fetch_listing_details,
    fetch_listings,
    find_battery_capacity,
    find_defects,
    is_good_deal,
    is_new_seller,
)
from bot.ai_scorer import score_listing, payment_red_flag

CONFIG_FILE = Path(__file__).parent.parent / "config.json"
CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.json"
# Live changes made from Telegram or the dashboard (prices, on/off, added
# searches). Kept out of config.json — which is tracked by git — so the Pi's
# checkout stays clean and its auto-updater can always fast-forward.
OVERRIDES_FILE = Path(__file__).parent.parent / "data" / "config_overrides.json"
# Search fields that can be changed live, with the value a missing field means
_OVERRIDE_FIELDS = {"min_price": 0, "max_price": 0, "enabled": True}

logger = logging.getLogger(__name__)
_seen_lock = Lock()

# ---------------------------------------------------------------------------
# PricerBot API helper
# ---------------------------------------------------------------------------

# Fetches the latest median and avg eBay sold price for a phone model from the
# PricerBot REST API.  Returns (avg_price, median_price) or (None, None) on any
# failure (API unavailable, model not yet priced, network error).
def _normalize_pricer_model(name: str) -> str:
    # PricerBot stores Samsung models as "Samsung Galaxy S24", "Samsung Galaxy A55", etc.
    # ScraperBot config uses shorter names like "Samsung S24", "Samsung A54".
    # Insert "Galaxy" when it is missing so the LIKE lookup matches.
    # Also normalise " Plus" → "+" to match PricerBot's config (e.g. "S24+" not "S24 Plus").
    import re as _re
    name = _re.sub(
        r'^(Samsung)\s+(?!Galaxy\b)([A-Z]\d)',
        r'\1 Galaxy \2',
        name,
    )
    return name.replace(" Plus", "+")


def fetch_price_estimate(model_name: str, pricer_api_url: str) -> tuple[Optional[float], Optional[float]]:
    try:
        url = f"{pricer_api_url.rstrip('/')}/price"
        response = requests.get(url, params={"model": _normalize_pricer_model(model_name)}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data.get("avg_price"), data.get("median_price")
        if response.status_code != 404:
            logger.debug(f"PricerBot API returned HTTP {response.status_code} for '{model_name}'")
    except requests.RequestException as e:
        logger.debug(f"Could not reach PricerBot API for '{model_name}': {e}")
    return None, None


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# Configures root logger to write to stdout and, optionally, a log file.
def setup_logging(log_file: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

# Reads config.json and merges credentials.json on top (credentials take priority).
# credentials.json is gitignored and holds API keys / tokens.
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[ERROR] Konfigurationsdatei nicht gefunden: {CONFIG_FILE}")
        sys.exit(1)
    with CONFIG_FILE.open(encoding="utf-8") as f:
        config = json.load(f)

    # Merge credentials if the file exists
    if CREDENTIALS_FILE.exists():
        with CREDENTIALS_FILE.open(encoding="utf-8") as f:
            creds = json.load(f)
        # Merge groq_api_key into settings
        if creds.get("groq_api_key"):
            config.setdefault("settings", {})["groq_api_key"] = creds["groq_api_key"]
        # Merge telegram block (deep merge — credentials values win)
        if creds.get("telegram"):
            config.setdefault("telegram", {}).update(
                {k: v for k, v in creds["telegram"].items() if v}
            )

    _apply_overrides(config)
    return config


# Applies data/config_overrides.json on top of config.json: changed prices and
# on/off states of existing searches, plus searches added from the dashboard.
# A live change wins over config.json until it is changed live again.
def _apply_overrides(config: dict) -> None:
    if not OVERRIDES_FILE.exists():
        return
    try:
        with OVERRIDES_FILE.open(encoding="utf-8") as f:
            overrides = json.load(f)
    except (ValueError, OSError) as exc:
        logger.warning(f"Could not read {OVERRIDES_FILE.name}, ignoring live changes: {exc}")
        return
    searches = config.setdefault("searches", [])
    by_name = {s.get("name"): s for s in searches}
    for name, fields in overrides.get("searches", {}).items():
        if name in by_name:
            by_name[name].update({k: v for k, v in fields.items() if k in _OVERRIDE_FIELDS})
    for added in overrides.get("added_searches", []):
        if added.get("name") not in by_name:
            searches.append(added)


# Saves live changes as the difference between the given config and config.json
# on disk, so config.json itself is never written at runtime.
def save_config_overrides(config: dict) -> None:
    with CONFIG_FILE.open(encoding="utf-8") as f:
        base = {s.get("name"): s for s in json.load(f).get("searches", [])}
    changed, added = {}, []
    for search in config.get("searches", []):
        original = base.get(search.get("name"))
        if original is None:
            added.append(search)
            continue
        diff = {
            k: search.get(k, default)
            for k, default in _OVERRIDE_FIELDS.items()
            if search.get(k, default) != original.get(k, default)
        }
        if diff:
            changed[search["name"]] = diff
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"searches": changed, "added_searches": added}, f, indent=2, ensure_ascii=False)
    tmp.replace(OVERRIDES_FILE)


# ---------------------------------------------------------------------------
# Seen-listings persistence
# ---------------------------------------------------------------------------

SEEN_MAX_AGE_DAYS = 7  # IDs older than this are removed from seen_listings.json


# Seen-set key for a listing checked by one search ("iPhone 13|3553249207").
# Plain listing IDs in the same set mark listings already notified by any search.
def _seen_key(search_name: str, listing_id: str) -> str:
    return f"{search_name}|{listing_id}"


# Loads previously seen listing IDs from disk, pruning entries older than SEEN_MAX_AGE_DAYS.
def load_seen_listings(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        # Backward compat: old format was a plain list with no timestamps
        if isinstance(data, list):
            return set(data)
        # New format: {id: iso_timestamp} — prune entries older than SEEN_MAX_AGE_DAYS
        cutoff = datetime.now() - timedelta(days=SEEN_MAX_AGE_DAYS)
        result: set[str] = set()
        for listing_id, ts_str in data.items():
            try:
                if datetime.fromisoformat(ts_str) >= cutoff:
                    result.add(listing_id)
            except (ValueError, TypeError):
                result.add(listing_id)  # Keep entries with unparseable timestamps
        pruned = len(data) - len(result)
        if pruned:
            logger.info(f"Pruned {pruned} old entries from seen listings ({len(result)} remaining).")
        return result
    except (json.JSONDecodeError, IOError):
        return set()


# Saves seen listing IDs with ISO timestamps to disk using an atomic write.
def save_seen_listings(path: str, seen: set[str]) -> None:
    p = Path(path)
    # Load existing timestamps so first-seen dates are preserved across saves
    existing: dict[str, str] = {}
    if p.exists():
        try:
            with p.open(encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    existing = data
        except (json.JSONDecodeError, IOError):
            pass
    now_iso = datetime.now().isoformat()
    timestamps = {id_: existing.get(id_, now_iso) for id_ in seen}
    # Atomic write: write to .tmp then replace so a crash never corrupts the file
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(timestamps, f, indent=2, sort_keys=True)
    try:
        tmp.replace(p)
    except PermissionError:
        # On Windows, os.replace can fail if the target is briefly locked
        # (e.g. by antivirus).  Fall back to a plain overwrite — the write
        # is already serialised by _seen_lock so this is safe.
        import shutil
        shutil.copy2(tmp, p)
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

# Runs one search cycle for a single search config entry.
def check_search(
    search_config: dict,
    seen: set[str],
    tg_config: dict,
    seen_file: str,
    global_blocked: list[str],
    stop_event,
    groq_api_key: str = "",
    pricer_api_url: str = "",
    ai_score_skip_threshold: int = 2,
    defect_keywords: Optional[list[str]] = None,
    defect_discount_percent: int = 30,
    new_seller_max_age_days: int = 5,
) -> None:
    name = search_config.get("name", search_config.get("query", "?"))
    query = search_config.get("query", "")

    if not query:
        return

    min_price = search_config.get("min_price")
    max_price = search_config.get("max_price")
    category_id = search_config.get("category_id")
    category_filter = search_config.get("category_filter", "")

    logger.info(f"Checking search: '{name}'")
    cutoff = datetime.now() - timedelta(minutes=30)

    # Fetch pages (newest first) until a page reaches listings this search has
    # already checked or that are older than 30 minutes, or we hit the 3-page cap.
    # Paid TOP ads carry no date and stay pinned above the newest results, so
    # only dated listings decide when to stop — otherwise a known TOP ad at the
    # top would end the search before any new listing is looked at.
    listings = []
    for page in range(3):
        if stop_event.is_set():
            return
        if page > 0:
            time.sleep(1)
        page_listings = fetch_listings(query, page, min_price, max_price, category_id, category_filter)
        if not page_listings:
            break
        listings.extend(page_listings)
        dated = [l for l in page_listings if l.posted_at is not None]
        with _seen_lock:
            reached_seen = any(_seen_key(name, l.listing_id) in seen for l in dated)
        if reached_seen or not dated or any(l.posted_at < cutoff for l in dated):
            break

    new_count = 0
    checked_count = 0
    seen_changed = False

    for listing in listings:
        if stop_event.is_set():
            break

        # Each search keeps its own seen entries: a listing that search
        # "iPhone 13" rejects as a Pro model must still be checked by search
        # "iPhone 13 Pro". Duplicate notifications are prevented separately below.
        key = _seen_key(name, listing.listing_id)
        with _seen_lock:
            if key in seen:
                continue
            seen.add(key)
            seen_changed = True
            already_notified = listing.listing_id in seen
        checked_count += 1

        logger.info(f"Listing {listing.listing_id} '{listing.title}' — {listing.price}€ ({name})")

        # Filter: ignore listings older than 30 minutes
        if listing.posted_at is not None and listing.posted_at < cutoff:
            logger.info(f"Skipped listing {listing.listing_id}: older than 30 minutes (posted {listing.posted_at:%d.%m. %H:%M})")
            continue

        if already_notified:
            logger.info(f"Skipped listing {listing.listing_id}: already notified by another search")
            continue

        good, reason = is_good_deal(listing, search_config, global_blocked)
        if not good:
            logger.info(f"Skipped listing {listing.listing_id} '{listing.title[:40]}': {reason}")
            continue

        if stop_event.is_set():
            break

        # Fetch detail page for full description, seller join date and seller rating
        full_desc, join_date, listing.seller_info = fetch_listing_details(listing.url)
        if full_desc:
            listing.description = full_desc
        listing.battery_capacity = find_battery_capacity(listing)

        # Skip sellers whose account was created today or yesterday (likely scammers)
        if is_new_seller(join_date, new_seller_max_age_days):
            logger.info(f"Skipped listing {listing.listing_id}: new seller account (aktiv seit {join_date:%d.%m.%Y})")
            continue

        # Re-run keyword check against the full description
        if full_desc:
            good, reason = is_good_deal(listing, search_config, global_blocked)
            if not good:
                logger.info(f"Skipped listing {listing.listing_id} after full desc check: {reason}")
                continue

        # Payment red flags (PayPal Freunde, nur Überweisung) — no buyer protection,
        # skip outright regardless of whether a Groq key is configured.
        flag = payment_red_flag(f"{listing.title} {listing.description or ''}")
        if flag:
            logger.info(f"Skipped listing {listing.listing_id}: {flag}")
            continue

        # Fetch estimated eBay sell price from PricerBot API
        if pricer_api_url:
            _, median_price = fetch_price_estimate(name, pricer_api_url)
            listing.estimated_sell_price = median_price
            if listing.price is not None and median_price is not None:
                listing.estimated_profit = round(median_price - listing.price)
            if median_price is not None:
                logger.info(f"Price estimate for '{name}': median {median_price:.0f} EUR, profit ~{listing.estimated_profit} EUR")

        # Defective devices (crack, dead pixels, "defekt" …) are only worth it when
        # they're at least defect_discount_percent below the search's max_price.
        defects = find_defects(listing, defect_keywords or [])
        if defects:
            discount = round(100 * (1 - listing.price / max_price)) if max_price else None
            if discount is None or discount < defect_discount_percent:
                logger.info(
                    f"Skipped listing {listing.listing_id}: defect ({', '.join(defects)}) but only "
                    f"{discount}% below max price ({max_price} EUR), need {defect_discount_percent}%"
                )
                continue
            listing.defect_note = f"{', '.join(defects)} — {discount}% unter Höchstpreis der Suche ({max_price} EUR)"
            logger.info(f"Listing {listing.listing_id} has a defect but is cheap enough: {listing.defect_note}")

        # AI scoring — only runs if a Groq API key is configured
        if groq_api_key:
            score, warning = score_listing(listing, groq_api_key, name)
            listing.ai_score = score
            listing.ai_warning = warning
            logger.info(f"AI score for {listing.listing_id}: {score}/10 — warning: '{warning}'")

            # Auto-skip clear rejects (e.g. counterfeit/grey-market devices) instead
            # of just displaying a low score — borderline scores still get sent so
            # they can be 👍/👎-rated.
            if score is not None and score <= ai_score_skip_threshold:
                logger.info(f"Skipped listing {listing.listing_id} — AI score {score}/10 too low: {warning}")
                continue

        # Reserve the plain listing ID so a parallel search can't notify the same listing again
        with _seen_lock:
            if listing.listing_id in seen:
                logger.info(f"Skipped listing {listing.listing_id}: already notified by another search")
                continue
            seen.add(listing.listing_id)

        logger.info(f"Good deal found: {listing.title} — {listing.price}€ ({name})")
        if notify_new_listing(tg_config, listing, name):
            new_count += 1
        else:
            # Telegram unreachable — forget the listing so the next cycle tries again
            with _seen_lock:
                seen.discard(listing.listing_id)
                seen.discard(key)
            logger.warning(f"Notification for listing {listing.listing_id} failed — retrying next cycle")

    if seen_changed:
        with _seen_lock:
            save_seen_listings(seen_file, seen)
    logger.info(
        f"Search '{name}' done. {checked_count} listings checked, {new_count} notifications sent."
    )


# Runs all enabled searches from config in parallel using a thread pool.
def run_all_searches(config: dict, seen: set[str], stop_event=None) -> None:
    import threading
    if stop_event is None:
        stop_event = threading.Event()  # never-set fallback for main.py standalone use
    tg_config = config["telegram"]
    seen_file = config["settings"].get("seen_listings_file", "seen_listings.json")
    global_blocked = config["settings"].get("keywords_blocked", [])
    max_workers = config["settings"].get("max_workers", 6)
    groq_api_key = config["settings"].get("groq_api_key", "")
    pricer_api_url = config["settings"].get("pricer_api_url", "")
    ai_score_skip_threshold = config["settings"].get("ai_score_skip_threshold", 2)
    defect_keywords = config["settings"].get("defect_keywords", [])
    defect_discount_percent = config["settings"].get("defect_discount_percent", 30)
    new_seller_max_age_days = config["settings"].get("new_seller_max_age_days", 5)

    active_searches = [s for s in config.get("searches", []) if s.get("enabled", True)]

    def _run(search):
        try:
            check_search(
                search, seen, tg_config, seen_file, global_blocked, stop_event, groq_api_key, pricer_api_url,
                ai_score_skip_threshold, defect_keywords, defect_discount_percent, new_seller_max_age_days,
            )
        except Exception as e:
            logger.error(
                f"Unexpected error in search '{search.get('name')}': {e}\n"
                + traceback.format_exc()
            )

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="search") as executor:
        futures = {executor.submit(_run, s): s.get("name", "?") for s in active_searches}
        for future in as_completed(futures):
            name = futures[future]
            if future.exception():
                logger.error(f"Thread for '{name}' raised: {future.exception()}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    settings = config.get("settings", {})
    setup_logging(settings.get("log_file", "bot.log"))

    global logger
    logger = logging.getLogger("main")

    tg_config = config.get("telegram", {})

    if not validate_telegram_config(tg_config):
        logger.error("Telegram nicht konfiguriert. Bot wird nicht gestartet.")
        sys.exit(1)

    seen_file = settings.get("seen_listings_file", "seen_listings.json")
    seen: set[str] = load_seen_listings(seen_file)
    logger.info(f"Loaded {len(seen)} previously seen listings.")

    interval = settings.get("check_interval_minutes", 5)
    active_names = [
        s["name"]
        for s in config.get("searches", [])
        if s.get("enabled", True)
    ]
    send_startup_message(tg_config, active_names)

    # Run immediately on start, then on schedule
    run_all_searches(config, seen)

    schedule.every(interval).minutes.do(
        run_all_searches, config=config, seen=seen
    )

    logger.info(f"Bot läuft. Intervall: alle {interval} Minuten. Strg+C zum Beenden.")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
