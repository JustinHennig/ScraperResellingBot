# Telegram notification helpers: formats listing data and delivers messages
# via the Telegram Bot API.
import logging
import requests
from bot.ratings import record_listing
from bot.scraper import Listing

logger = logging.getLogger(__name__)

TG_API_URL = "https://api.telegram.org/bot"

# Battery health (in %) below which the notification shows a 🔋 warning
BATTERY_WARN_BELOW = 80


# Normalizes tg_config["chat_id"] (single id or list) into a list of strings.
def _chat_ids(tg_config: dict) -> list[str]:
    raw = tg_config.get("chat_id", [])
    ids = raw if isinstance(raw, list) else [raw]
    return [str(c) for c in ids if c]


# Sends a Telegram text message via the Bot API.
# chat_id can be a single id or a list of ids.
def send_telegram_message(tg_config: dict, body: str, reply_markup: dict | None = None) -> bool:
    token = tg_config["bot_token"]

    payload_base = {"text": body}
    if reply_markup is not None:
        payload_base["reply_markup"] = reply_markup

    success = True
    for chat_id in _chat_ids(tg_config):
        try:
            response = requests.post(
                f"{TG_API_URL}{token}/sendMessage",
                json={"chat_id": chat_id, **payload_base},
                timeout=10,
            )
            if response.status_code != 200:
                logger.error(f"Telegram notification failed for {chat_id}: HTTP {response.status_code} — {response.text[:200]}")
                success = False
        except requests.RequestException as e:
            logger.error(f"Telegram notification request failed for {chat_id}: {e}")
            success = False
    return success


# Sends a Telegram photo message with caption via the Bot API.
# chat_id can be a single id or a list of ids.
def send_telegram_photo(tg_config: dict, image_url: str, caption: str, reply_markup: dict | None = None) -> bool:
    token = tg_config["bot_token"]

    payload_base = {"photo": image_url, "caption": caption[:1024]}
    if reply_markup is not None:
        payload_base["reply_markup"] = reply_markup

    success = True
    for chat_id in _chat_ids(tg_config):
        try:
            response = requests.post(
                f"{TG_API_URL}{token}/sendPhoto",
                json={"chat_id": chat_id, **payload_base},
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(f"Telegram photo send failed for {chat_id}: HTTP {response.status_code} — {response.text[:200]}")
                success = False
        except requests.RequestException as e:
            logger.warning(f"Telegram photo request failed for {chat_id}: {e}")
            success = False
    return success


# Returns formatted Telegram message body for a listing.
def format_listing_message(listing: Listing, search_name: str) -> str:
    if listing.price is not None:
        price_str = f"{listing.price} EUR (VB)" if listing.negotiable else f"{listing.price} EUR"
    else:
        price_str = "Preis auf Anfrage"

    lines = [f"{search_name}: {listing.title}"]

    # AI score line, e.g. "⭐ Score: 8/10"
    if listing.ai_score is not None:
        lines.append(f"⭐ Score: {listing.ai_score}/10")

    lines.append(price_str)

    # Estimated eBay sell price and profit from PricerBot
    if listing.estimated_sell_price is not None:
        sell_line = f"📊 eBay-Median: {listing.estimated_sell_price:.0f} EUR"
        if listing.estimated_profit is not None:
            profit_emoji = "💰" if listing.estimated_profit >= 10 else ("⚠️" if listing.estimated_profit < 0 else "➡️")
            sell_line += f"  •  {profit_emoji} Gewinn: ~{listing.estimated_profit:+d} EUR"
        lines.append(sell_line)

    # Defect that was let through because the price is low enough
    if listing.defect_note:
        lines.append(f"🔧 Defekt: {listing.defect_note}")

    # Weak battery as stated in title/description
    if listing.battery_capacity is not None and listing.battery_capacity < BATTERY_WARN_BELOW:
        lines.append(f"🔋 Akku nur {listing.battery_capacity} %")

    # Seller rating and account age from the detail page
    if listing.seller_info:
        lines.append(f"👤 {listing.seller_info}")

    # AI warning for non-original/modified parts
    if listing.ai_warning:
        lines.append(f"⚠️ {listing.ai_warning}")

    if listing.description:
        short_desc = listing.description[:200]
        if len(listing.description) > 200:
            short_desc += "…"
        lines.append(short_desc)
    lines.append(listing.url)

    return "\n".join(lines)


# Inline "👍 Kaufen" / "👎 Nicht kaufen" buttons attached to a listing notification.
# Tapping one sends a callback_query the server's Telegram poll loop reacts to.
def _rating_keyboard(listing_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "👍 Kaufen", "callback_data": f"rate:{listing_id}:up"},
            {"text": "👎 Nicht kaufen", "callback_data": f"rate:{listing_id}:down"},
        ]]
    }


# Sends a Telegram notification for a new listing, with photo if available,
# and a 👍/👎 rating keyboard so the recipient can flag whether they'd buy it.
def notify_new_listing(tg_config: dict, listing: Listing, search_name: str) -> bool:
    body = format_listing_message(listing, search_name)
    keyboard = _rating_keyboard(listing.listing_id)

    if listing.image_url:
        success = send_telegram_photo(tg_config, listing.image_url, body, reply_markup=keyboard)
        if success:
            logger.info(f"Notification (photo) sent for listing {listing.listing_id}: {listing.title}")
            record_listing(listing.listing_id, listing.title, listing.price, listing.url, search_name)
            return True
        # Fall back to text if photo send fails
        logger.warning(f"Photo send failed for {listing.listing_id}, falling back to text")

    success = send_telegram_message(tg_config, body, reply_markup=keyboard)
    if success:
        logger.info(f"Notification (text) sent for listing {listing.listing_id}: {listing.title}")
        record_listing(listing.listing_id, listing.title, listing.price, listing.url, search_name)
    return success


# Sends a startup confirmation Telegram message.
def send_startup_message(tg_config: dict, search_names: list[str]) -> None:
    names_str = ", ".join(search_names)
    send_telegram_message(tg_config, f"✅ Reselling Bot gestartet\nAktive Suchen: {names_str}")


# Checks that all required Telegram config fields are present.
def validate_telegram_config(tg_config: dict) -> bool:
    required = ["bot_token", "chat_id"]
    for field in required:
        if not tg_config.get(field):
            logger.error(f"Telegram config fehlt: '{field}' in config.json unter 'telegram' eintragen.")
            return False
    logger.info("Telegram config OK.")
    return True
