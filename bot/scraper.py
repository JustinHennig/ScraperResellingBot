# Kleinanzeigen scraper: fetches search result pages and listing detail pages,
# parses them into Listing objects, and applies deal-quality filters.
import json
import requests
from bs4 import BeautifulSoup
import logging
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# Mapping from German month names to month numbers, used when parsing seller join dates.
GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}

# Browser-like HTTP headers sent with every request to avoid bot detection.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    # Accept Kleinanzeigen's GDPR consent wall so detail pages load properly.
    # Without this the bot gets the cookie-consent page instead of the listing.
    "Cookie": "ANON_CONSENT_AGREED=1; ANON_CONSENT_VERSION=1",
}

# Retryable HTTP status codes (rate-limited or server-side transient errors)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


# GET request with exponential backoff retry.
# Retries on network errors and retryable HTTP status codes.
# Raises requests.RequestException after all attempts are exhausted.
def _http_get(url: str, retries: int = 3, base_delay: float = 2.0) -> requests.Response:
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code not in _RETRY_STATUSES:
                response.raise_for_status()
                return response
            # Retryable status — treat like a transient error
            raise requests.HTTPError(
                f"HTTP {response.status_code}", response=response
            )
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)  # 2s, 4s, 8s …
            logger.warning(f"Request failed ({e}), retrying in {delay:.0f}s …")
            time.sleep(delay)
    raise RuntimeError("unreachable")


# Represents a single Kleinanzeigen listing with all parsed fields.
@dataclass
class Listing:
    listing_id: str
    title: str
    price: Optional[int]
    location: str
    url: str
    description: str
    image_url: str
    posted_at: Optional[datetime] = None
    negotiable: bool = False  # True when the price has "VB" (Verhandlungsbasis)
    ai_score: Optional[int] = None  # 1-10 resale value score from Groq (None = not scored)
    ai_warning: str = ""  # Non-empty when Groq detected non-original parts or modifications
    estimated_sell_price: Optional[float] = None  # Median eBay sold price from PricerBot API
    estimated_profit: Optional[int] = None  # estimated_sell_price - listing price


# Builds the Kleinanzeigen search URL.
def build_search_url(query: str, page: int = 0) -> str:
    encoded_query = quote_plus(query)
    page_segment = f"/seite:{page + 1}" if page > 0 else ""
    return (
        f"https://www.kleinanzeigen.de/s-anzeige:angebote"
        f"/{encoded_query}"
        f"/k0{page_segment}"
    )


# Parses Kleinanzeigen timestamp strings into a datetime.
# Examples: 'Heute, 14:30', 'Gestern, 09:15', '22.03.2026', 'Gerade eben'
def parse_posted_at(text: str) -> Optional[datetime]:
    if not text:
        return None
    # Strip HTML entities and tags before processing
    text = re.sub(r'&#?\w+;', '', text)   # remove HTML entities like &#8203;
    text = re.sub(r'<[^>]+>', '', text)    # remove any stray HTML tags
    text = text.strip()
    if not text:
        return None

    now = datetime.now()

    if "gerade" in text.lower():
        return now

    # "Heute, HH:MM"
    m = re.match(r"(?i)heute,?\s*(\d{1,2}):(\d{2})", text)
    if m:
        try:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        except ValueError:
            return None

    # "Gestern, HH:MM"
    m = re.match(r"(?i)gestern,?\s*(\d{1,2}):(\d{2})", text)
    if m:
        try:
            yesterday = now - timedelta(days=1)
            return yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        except ValueError:
            return None

    # "DD.MM.YYYY"
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    return None


# Extracts integer price from a price string like '350 €' or 'VB 350 €'.
# Returns None if the text looks like location data, HTML, or anything other than a price.
def parse_price(price_text: str) -> Optional[int]:
    if not price_text:
        return None
    # Reject HTML content immediately
    if "<" in price_text or ">" in price_text:
        return None
    # Reject if text contains letters but no currency/price indicator
    # (catches location names like "82031 Freimann")
    has_currency = re.search(r'[€]|\bEUR\b|\bVB\b|\bvb\b', price_text, re.IGNORECASE)
    if re.search(r'[a-zA-ZäöüÄÖÜß]', price_text) and not has_currency:
        return None
    # Reduced-price listings render both the struck-through old price and the
    # new price inside the same element, e.g. "470 €399 €" once get_text()
    # merges them. Extract each number separately and keep the last one (the
    # current price) instead of concatenating every digit into one huge number.
    matches = re.findall(r'\d[\d.]*', price_text)
    if not matches:
        return None
    digits = re.sub(r'[^0-9]', '', matches[-1])
    if not digits:
        return None
    try:
        price = int(digits)
    except ValueError:
        return None
    # Sanity check: no phone has a plausible price above 9999 €
    return price if price <= 9999 else None


# Fetches listings from Kleinanzeigen for a given search query.
# Returns a list of Listing objects.
def fetch_listings(query: str, page: int = 0) -> list[Listing]:
    url = build_search_url(query, page=page)
    logger.debug(f"Fetching: {url}")

    try:
        response = _http_get(url)
    except requests.RequestException as e:
        logger.error(f"Request failed for query '{query}': {e}")
        return []

    # Python 3.14's html.parser crashes on malformed numeric entities like &#8203 (missing semicolon).
    # Fix them before parsing.
    html = re.sub(r'&#(\d+)(?!;)', r'&#\1;', response.text)
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # Kleinanzeigen wraps each ad in an <article> with class 'aditem'
    articles = soup.select("article.aditem")

    if not articles:
        # Fallback: try li elements
        articles = soup.select("li[data-adid]")

    for article in articles:
        try:
            listing = _parse_article(article)
            if listing:
                listings.append(listing)
        except Exception as e:
            logger.debug(f"Failed to parse article: {e}")
            continue

    logger.info(f"Found {len(listings)} listings for '{query}' (page {page + 1})")
    return listings


# Parses a single article/li element into a Listing.
# Strategy: extract title, description, and image from the JSON-LD block that
# Kleinanzeigen embeds inside every <article> — this is far more stable than
# navigating CSS class names that change on redesigns.  Only price, location,
# and date still come from the surrounding HTML, using fixed schema-driven
# class names that have been stable for years.
def _parse_article(article) -> Optional[Listing]:

    # --- ID (data attribute — very stable) ---
    listing_id = (
        article.get("data-adid")
        or article.get("data-ad-id")
        or article.get("id", "")
    )
    if not listing_id:
        return None

    # --- URL (data-href attribute — no need to hunt for <a>) ---
    relative_url = article.get("data-href", "")
    if not relative_url:
        link_el = article.select_one("a[href]")
        relative_url = link_el["href"] if link_el else ""
    full_url = (
        f"https://www.kleinanzeigen.de{relative_url}"
        if relative_url.startswith("/")
        else relative_url
    )

    # --- JSON-LD block inside the article (title, description, image) ---
    # Kleinanzeigen embeds a structured ImageObject JSON-LD in every article.
    # It contains reliable title + full description, independent of HTML layout.
    ld_script = article.select_one('script[type="application/ld+json"]')
    ld: dict = {}
    if ld_script and ld_script.string:
        try:
            ld = json.loads(ld_script.string)
        except (ValueError, TypeError):
            pass

    title = ld.get("title", "").strip()
    description = ld.get("description", "").strip()
    # contentUrl is the full-resolution image; fall back to <img src> if absent
    image_url = ld.get("contentUrl", "")

    if not title:
        # Fallback to HTML if JSON-LD is missing (shouldn't happen, but be safe)
        title_el = (
            article.select_one("h2.text-module-begin a")
            or article.select_one("a.ellipsis")
            or article.select_one("h2 a")
        )
        title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    if not image_url:
        img_el = article.select_one("img[src]")
        image_url = img_el["src"] if img_el else ""

    # --- Price (stable class name) ---
    price_el = (
        article.select_one("p.aditem-main--middle--price-shipping--price")
        or article.select_one(".aditem-main--middle--price")
    )
    price_text = price_el.get_text(strip=True) if price_el else ""
    price = parse_price(price_text)
    negotiable = bool(re.search(r'\bVB\b', price_text, re.IGNORECASE))

    # --- Location (stable class name) ---
    location_el = article.select_one(".aditem-main--top--left")
    location = location_el.get_text(strip=True) if location_el else "Unbekannt"
    location = re.sub(r'&#?\w+;', '', location).strip() or "Unbekannt"

    # --- Posted at (stable class name) ---
    date_el = article.select_one(".aditem-main--top--right")
    posted_at = parse_posted_at(date_el.get_text(strip=True)) if date_el else None

    return Listing(
        listing_id=str(listing_id),
        title=title,
        price=price,
        negotiable=negotiable,
        location=location,
        url=full_url,
        description=description,
        image_url=image_url,
        posted_at=posted_at,
    )


# Parses a German date like 'Aktiv seit 15. März 2024' into a datetime.
def _parse_german_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    m = re.search(r'(\d{1,2})\.\s*(\w+)\s+(\d{4})', text)
    if not m:
        return None
    day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = GERMAN_MONTHS.get(month_str)
    if not month:
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


# Fetches the listing detail page and returns (full_description, seller_join_date).
# Makes a single HTTP request used for both the full keyword check and new-seller detection.
def fetch_listing_details(listing_url: str) -> tuple[str, Optional[datetime]]:
    try:
        time.sleep(1)
        response = _http_get(listing_url)
    except requests.RequestException as e:
        logger.debug(f"Could not fetch listing detail page: {e}")
        return "", None

    html = re.sub(r'&#(\d+)(?!;)', r'&#\1;', response.text)

    # Detect the GDPR consent wall — if we got it, the cookie bypass failed
    if ("ANON_CONSENT" in html or "Datenschutzeinstellungen" in html) and "viewad" not in html:
        logger.warning("Got GDPR consent wall on detail page — seller date check unavailable")
        return "", None

    soup = BeautifulSoup(html, "html.parser")

    # --- Full description ---
    desc_el = (
        soup.select_one("#viewad-description-text")
        or soup.select_one(".addetailslist")
        or soup.select_one("[id*='description']")
    )
    full_description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # --- Seller join date ---
    join_date: Optional[datetime] = None
    date_el = (
        soup.select_one(".userprofile-vip-membershipdate")
        or soup.select_one("[class*='membershipdate']")
    )
    if date_el:
        join_date = _parse_german_date(date_el.get_text(strip=True))
    else:
        for el in soup.find_all(string=re.compile(r'(?i)(aktiv|mitglied)\s+seit')):
            join_date = _parse_german_date(el)
            if join_date:
                break

    return full_description, join_date


# Returns True if the seller's account was created today or yesterday (likely a scammer).
def is_new_seller(join_date: Optional[datetime]) -> bool:
    if join_date is None:
        return False  # Can't determine — give benefit of the doubt
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    return join_date.date() >= yesterday


# Returns True only if ALL query words appear within a short token-position
# window of each other in the text.  Prevents false positives where a model
# number (e.g. "14" or "s23") appears in an unrelated swap/trade list
# ("iPhone 12 Tausch 13 14 15 Pro" or "Samsung S21 Tausch S22 S23").
# Window = number of query words + 2 extra tokens (tolerates filler words like
# "galaxy", colours, or punctuation tokens between query terms).
def _query_phrase_matches(query: str, text: str) -> bool:
    words = query.lower().split()
    if not words:
        return True

    # Normalise spaced model numbers before splitting into tokens:
    # "S 24" → "s24", "A 55" → "a55".  Sellers often write Samsung model
    # numbers with a space between the letter prefix and the digits.
    text = re.sub(r'\b([a-z])\s+(\d{2,})\b', r'\1\2', text.lower())
    # Normalise standalone "galaxy" → "samsung galaxy" so that listings
    # written as "Galaxy S24" (without "Samsung") still match query "samsung s24".
    text = re.sub(r'\bgalaxy\b', 'samsung galaxy', text)
    tokens = text.split()
    n = len(tokens)
    window_size = len(words) + 2

    def _pat(w: str) -> str:
        if w.isdigit():
            return rf'\b{re.escape(w)}\b(?!\s*(?:gb|tb))'
        return rf'\b{re.escape(w)}\b'

    def _find_positions(w: str, pattern: str) -> list:
        # First verify the word itself is in this token (prevents matching in
        # the *next* token when ctx = tok + next_tok).  Then re-check the full
        # pattern with the next token appended so that (?!\s*gb) lookaheads work
        # even when "gb" is a separate token.
        base = rf'\b{re.escape(w)}\b'
        result = []
        for idx, tok in enumerate(tokens):
            if not re.search(base, tok):
                continue
            ctx = tok + (" " + tokens[idx + 1] if idx + 1 < n else "")
            if re.search(pattern, ctx):
                result.append(idx)
        return result

    patterns = [_pat(w) for w in words]
    all_positions = [_find_positions(w, p) for w, p in zip(words, patterns)]

    # Anchor at each occurrence of the first word; check all remaining words
    # fall within window_size tokens.
    for start in all_positions[0]:
        end = start + window_size
        if all(any(start <= pos < end for pos in positions) for positions in all_positions[1:]):
            return True
    return False


# Checks whether a listing matches the configured criteria.
# Returns (is_good, reason_string).
def is_good_deal(listing: Listing, search_config: dict, global_blocked: Optional[list[str]] = None) -> tuple[bool, str]:
    if global_blocked is None:
        global_blocked = []
    min_price = search_config.get("min_price", 0)
    max_price = search_config.get("max_price", 0)
    keywords_required: list[str] = search_config.get("keywords_required", [])
    keywords_blocked: list[str] = list(global_blocked) + search_config.get("keywords_blocked", [])

    text = f"{listing.title} {listing.description}".lower()

    # Price check
    if listing.price is None:
        return False, "Kein Preis angegeben"

    if max_price > 0 and listing.price > max_price:
        return False, f"Preis {listing.price}€ zu hoch (Max: {max_price}€)"

    if min_price > 0 and listing.price < min_price:
        return False, f"Preis {listing.price}€ zu niedrig (Min: {min_price}€)"

    # Blocked keywords
    for kw in keywords_blocked:
        if kw.lower() in text:
            return False, f"Gesperrtes Keyword gefunden: '{kw}'"

    # Verify listing is actually about the searched product.
    # All query words must appear as a near-phrase (within 30 chars of each other).
    # This catches "iPhone 14 Pro" correctly while rejecting listings that only
    # mention "14" and "Pro" in unrelated swap/trade lists ("Tausch 13 14 15 … Pro").
    query = search_config.get("query", "")
    if query and not _query_phrase_matches(query, text):
        return False, f"Suchbegriff '{query}' nicht als zusammenhängende Phrase gefunden"

    # Required keywords
    for kw in keywords_required:
        if kw.lower() not in text:
            return False, f"Pflicht-Keyword fehlt: '{kw}'"

    return True, "Passt allen Kriterien"
