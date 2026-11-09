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
                # Kleinanzeigen serves UTF-8 without a charset in Content-Type, so
                # requests falls back to ISO-8859-1 and mangles € / umlauts.
                response.encoding = "utf-8"
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
    defect_note: str = ""  # Set when a defective device is sent because it's cheap enough, e.g. "sprung — 45% unter eBay-Median"
    seller_info: str = ""  # e.g. "TOP Zufriedenheit · Freundlich · aktiv seit 18.01.2020"
    battery_capacity: Optional[int] = None  # Battery health in % as stated by the seller, e.g. 78


# Builds the Kleinanzeigen search URL, e.g.
# https://www.kleinanzeigen.de/s-anzeige:angebote/preis:35:130/seite:2/iphone+13/k0c173+handy_telekom.art_s:apple
# "anzeige:angebote" drops wanted ads ("Suche iPhone 13"). Price and page
# filters must sit between "/s-" and the query — a trailing "/seite:2" after
# "k0" is ignored by Kleinanzeigen and returns unrelated listings.
# category_id (e.g. 173 = Handy & Telekom) and category_filter (e.g.
# "handy_telekom.art_s:apple") restrict results to actual phones of one brand,
# which keeps monitors, cases and repair ads out.
def build_search_url(
    query: str,
    page: int = 0,
    min_price: int | None = None,
    max_price: int | None = None,
    category_id: int | None = None,
    category_filter: str = "",
) -> str:
    filters = ["anzeige:angebote"]
    if min_price or max_price:
        filters.append(f"preis:{min_price or ''}:{max_price or ''}")
    if page > 0:
        filters.append(f"seite:{page + 1}")
    filter_path = "".join(f"{f}/" for f in filters)
    category = f"c{category_id}" if category_id else ""
    if category and category_filter:
        category += f"+{category_filter}"
    return f"https://www.kleinanzeigen.de/s-{filter_path}{quote_plus(query)}/k0{category}"


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
    # Drop decimal cents ("1.099,00 €" → "1.099 €") so they aren't read as a separate number
    price_text = re.sub(r',\d{1,2}(?!\d)', '', price_text)
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
def fetch_listings(
    query: str,
    page: int = 0,
    min_price: int | None = None,
    max_price: int | None = None,
    category_id: int | None = None,
    category_filter: str = "",
) -> list[Listing]:
    url = build_search_url(query, page, min_price, max_price, category_id, category_filter)
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

    # Kleinanzeigen currently serves two different result templates side by
    # side (old class="aditem" markup and a newer Tailwind-utility layout,
    # switched per-listing rather than per-page) — both carry data-adid, so
    # that attribute is the one stable hook across both.
    articles = soup.select("article[data-adid]")

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


# Scans an article's leaf <p>/<span> elements (no nested p/span children) in
# document order and returns the text of the first one matching any pattern.
# Used as a fallback for fields that have no stable class name in the newer
# Kleinanzeigen template.
def _find_leaf_text(article, patterns: list[str]) -> str:
    compiled = [re.compile(p) for p in patterns]
    for tag in article.find_all(["p", "span"]):
        if tag.find(["p", "span"]):
            continue
        text = " ".join(tag.get_text(" ", strip=True).split())
        if text and any(p.search(text) for p in compiled):
            return text
    return ""


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

    # --- Price ---
    # Old layout: fixed class names. New layout: no stable class, just a plain
    # <p> — fall back to scanning leaf text nodes for a € amount or "VB".
    price_el = (
        article.select_one("p.aditem-main--middle--price-shipping--price")
        or article.select_one(".aditem-main--middle--price")
        or article.select_one("p.text-title3")
    )
    price_text = price_el.get_text(strip=True) if price_el else ""
    if not price_text:
        price_text = _find_leaf_text(article, ["€", r"\bVB\b"]) or ""
    price = parse_price(price_text)
    negotiable = bool(re.search(r'\bVB\b', price_text, re.IGNORECASE))

    # --- Location ---
    # Old layout: fixed class name. New layout: a bare leading <span> with a
    # German postal code ("12345 Ort").
    location_el = article.select_one(".aditem-main--top--left")
    if location_el:
        location = location_el.get_text(strip=True)
    else:
        location = _find_leaf_text(article, [r"^\d{4,5}\s+\S"]) or ""
    location = re.sub(r'&#?\w+;', '', location).strip() or "Unbekannt"

    # --- Posted at ---
    # Old layout: fixed class name. New layout: a bare <span> reading
    # "Heute, HH:MM" / "Gestern, HH:MM" / "DD.MM.YYYY" / "Gerade eben".
    date_el = article.select_one(".aditem-main--top--right")
    if date_el:
        date_text = date_el.get_text(strip=True)
    else:
        date_text = _find_leaf_text(
            article, [r"(?i)heute", r"(?i)gestern", r"(?i)gerade eben", r"^\d{1,2}\.\d{1,2}\.\d{4}$"]
        )
    posted_at = parse_posted_at(date_text) if date_text else None

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


# Parses a German date like 'Aktiv seit 30.03.2011' (current format),
# 'Aktiv seit 15. März 2024' (old format) or 'Aktiv seit heute/gestern' into a datetime.
def _parse_german_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    today = datetime.combine(datetime.now().date(), datetime.min.time())
    if re.search(r'(?i)\bheute\b', text):
        return today
    if re.search(r'(?i)\bgestern\b', text):
        return today - timedelta(days=1)
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
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


# Fetches the listing detail page and returns (full_description, seller_join_date, seller_summary).
# Makes a single HTTP request used for the full keyword check, new-seller detection
# and the seller line in the notification.
def fetch_listing_details(listing_url: str) -> tuple[str, Optional[datetime], str]:
    try:
        time.sleep(1)
        response = _http_get(listing_url)
    except requests.RequestException as e:
        logger.warning(f"Could not fetch detail page {listing_url}: {e} — full description and seller date unchecked")
        return "", None, ""

    html = re.sub(r'&#(\d+)(?!;)', r'&#\1;', response.text)

    # Detect the GDPR consent wall — if we got it, the cookie bypass failed
    if ("ANON_CONSENT" in html or "Datenschutzeinstellungen" in html) and "viewad" not in html:
        logger.warning("Got GDPR consent wall on detail page — seller date check unavailable")
        return "", None, ""

    soup = BeautifulSoup(html, "html.parser")

    # --- Full description ---
    desc_el = (
        soup.select_one("#viewad-description-text")
        or soup.select_one(".addetailslist")
        or soup.select_one("[id*='description']")
    )
    full_description = desc_el.get_text(" ", strip=True) if desc_el else ""
    if not full_description:
        logger.warning(f"No description found on detail page {listing_url} — page layout may have changed")

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

    return full_description, join_date, _seller_summary(soup, join_date)


# Builds the seller line for the notification from the detail page, e.g.
# "TOP Zufriedenheit · Freundlich · aktiv seit 18.01.2020". Kleinanzeigen's
# satisfaction badge is "TOP", "OK" or "NA JA Zufriedenheit"; sellers without
# enough ratings have none.
def _seller_summary(soup, join_date: Optional[datetime]) -> str:
    badges = []
    for el in soup.select(".profile-userbadges .userbadge-tag, .userbadges-profile-rating"):
        text = " ".join(el.get_text(" ", strip=True).split())
        if text and text not in badges:
            badges.append(text)
    parts = [" · ".join(badges) or "Noch keine Bewertung"]
    if join_date:
        parts.append(f"aktiv seit {join_date:%d.%m.%Y}")
    return " · ".join(parts)


# Returns True if the seller's account is younger than max_age_days (likely a scammer).
# With the default 5, an account created 4 days ago is new, one created 5 days ago isn't.
def is_new_seller(join_date: Optional[datetime], max_age_days: int = 5) -> bool:
    if join_date is None:
        return False  # Can't determine — give benefit of the doubt
    return (datetime.now().date() - join_date.date()).days < max_age_days


# Model-variant words that sellers often glue onto the model number
# ("13pro", "s21fe", "s24ultra"); _normalize_text splits them off.
_VARIANT_SUFFIXES = "pro|plus|max|mini|ultra|fe|xl|edge"


# Lowercases listing text and unifies the many ways sellers write model names,
# so query matching and keyword checks only ever see one spelling:
#   "iPhone12" → "iphone 12", "13Pro" → "13 pro", "S21FE" → "s21 fe",
#   "ProMax" → "pro max", "16 e" → "16e", "S24+" → "s24 plus",
#   "S 24" → "s24", "Galaxy S24" → "samsung galaxy s24".
def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r'(?<=\w)\+', ' plus', text)
    text = re.sub(r'\b(iphone|pixel)(\d)', r'\1 \2', text)
    text = re.sub(r'\bpromax\b', 'pro max', text)
    text = re.sub(rf'(\d)({_VARIANT_SUFFIXES})\b', r'\1 \2', text)
    text = re.sub(r'\b(\d{1,2})\s+([ae])\b', r'\1\2', text)
    # Sellers often write Samsung model numbers with a space: "S 24" → "s24", "A 55" → "a55"
    text = re.sub(r'\b([a-z])\s+(\d{2,})\b', r'\1\2', text)
    # "Galaxy S24" (without "Samsung") should still match query "samsung s24"
    text = re.sub(r'\bgalaxy\b', 'samsung galaxy', text)
    return text


# A bare model number token: "13", "16e", "s24", "10a," …
_MODEL_NUMBER_TOKEN_RE = re.compile(r'^[a-z]?\d{1,2}[a-z]?[,/;]?$')


# Returns True if tokens[idx] sits in a run of 3+ bare model numbers, i.e. a
# space-separated list like "iPhone 14 15 16 17" or "S23 S24 S25".
def _in_model_number_run(tokens: list[str], idx: int) -> bool:
    lo = idx
    while lo > 0 and _MODEL_NUMBER_TOKEN_RE.match(tokens[lo - 1]):
        lo -= 1
    hi = idx
    while hi + 1 < len(tokens) and _MODEL_NUMBER_TOKEN_RE.match(tokens[hi + 1]):
        hi += 1
    return hi - lo + 1 >= 3


# Returns the index of the first model mention in the title tokens — the
# query's first word ("iphone", "samsung", "pixel") followed within three
# tokens by a model number shaped like the query's ("17", "s24", "8a") — or
# None if the title names no specific model.
def _first_model_mention(query: str, tokens: list[str]) -> Optional[int]:
    words = query.lower().split()
    model_word = next((w for w in words if any(ch.isdigit() for ch in w)), None)
    if not words or model_word is None:
        return None
    prefix = re.match(r'[a-z]*', model_word).group()
    shape = re.compile(rf'^{prefix}\d{{1,2}}[a-z]?$')
    for idx, tok in enumerate(tokens):
        if tok != words[0]:
            continue
        for j in range(idx + 1, min(idx + 4, len(tokens))):
            if shape.match(tokens[j].strip(",.;:!?()*/|–-")) and not _in_model_number_run(tokens, j):
                return idx
    return None


# Finds where the listing mentions the searched model: the first occurrence of
# the first query word that has all remaining query words within a short
# token window after it. Returns the (start, end) token span (end exclusive),
# or None if the model isn't mentioned as a near-phrase. The window (number of
# query words + 2) tolerates filler like "galaxy" or colours but rejects
# unrelated swap lists ("iPhone 12 Tausch 13 14 15 Pro" for "iphone 14 pro").
def _query_phrase_span(query: str, tokens: list[str]) -> Optional[tuple[int, int]]:
    words = query.lower().split()
    if not words:
        return 0, 0
    n = len(tokens)
    window_size = len(words) + 2

    # Model numbers must not be storage sizes: "iphone 12" shouldn't match "iPhone 64 GB 12"
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
            # A model number inside a number list ("11,12,13,17", "12/13" or
            # "14 15 16 17") is a swap/compatibility list, not the model being sold
            if any(ch.isdigit() for ch in w) and (len(re.findall(r'\d+', tok)) > 1 or _in_model_number_run(tokens, idx)):
                continue
            ctx = tok + (" " + tokens[idx + 1] if idx + 1 < n else "")
            if re.search(pattern, ctx):
                result.append(idx)
        return result

    all_positions = [_find_positions(w, _pat(w)) for w in words]

    for start in all_positions[0]:
        end = start + window_size
        matched = [next((p for p in positions if start <= p < end), None) for positions in all_positions[1:]]
        if all(p is not None for p in matched):
            return start, max([start, *matched]) + 1
    return None


# Returns True if all query words appear as a near-phrase in the text (see _query_phrase_span).
def _query_phrase_matches(query: str, text: str) -> bool:
    return _query_phrase_span(query, _normalize_text(text).split()) is not None


# Regex for a blocked keyword. Short keywords (≤5 chars, e.g. model suffixes
# 'fe', 'pro', '10a') must stand on their own so 'fe' doesn't hit 'Kaffee' or
# 'pro' hit 'Problem'. A letter edge only needs a non-letter neighbour and a
# digit edge a non-digit one, so 's24fe', '13pro' and 'pixel10a' still match.
# Longer keywords match as substrings so 'defekt' still catches
# 'Displaydefekt' and 'defekte'.
def _keyword_pattern(kw: str) -> str:
    kw = kw.lower()
    if len(kw) > 5:
        return re.escape(kw)
    letters = "a-zäöüß"
    left = f"(?<![{letters}])" if kw[0].isalpha() else ("(?<![0-9])" if kw[0].isdigit() else "")
    right = f"(?![{letters}])" if kw[-1].isalpha() else ("(?![0-9])" if kw[-1].isdigit() else "")
    return left + re.escape(kw) + right


# Returns True if a blocked keyword occurs in the (lowercased) text.
def _blocked_keyword_in(kw: str, text: str) -> bool:
    return re.search(_keyword_pattern(kw), text) is not None


# Words that negate a keyword when one of them is among the two words right
# before it: "kein Wasserschaden", "nicht defekt", "noch nie in Reparatur".
_NEGATIONS = {
    "kein", "keine", "keinen", "keinem", "keiner", "keines", "keinerlei",
    "nicht", "nichts", "ohne", "nie", "niemals",
}
# Conjunctions don't count towards those two words, so the negation carries
# over a list: "keine Kratzer oder Pixelfehler", "kein Wasser- oder Displayschaden".
_CONJUNCTIONS = {"oder", "und", "bzw", "bzw.", "sowie"}
# Text right after a keyword that says the problem is absent: "iCloud frei",
# "defektfrei", "Wasserschaden: nein", "Defekte: keine", "Sperre deaktiviert".
_NEGATING_SUFFIX_RE = re.compile(
    r'^\w*(?:[\s-]*frei\b'
    r'|\s*:\s*(?:nein|keine?|frei|nicht vorhanden)\b'
    r'|\s+nein\b'
    r'|\s+(?:ist\s+)?(?:deaktiviert|entfernt|aufgehoben)\b)'
)
# A negation never reaches across a clause boundary, so in
# "kein Original-Display, Akku defekt" the 'kein' doesn't cancel 'defekt'.
_CLAUSE_BREAK_RE = re.compile(r'[,.;:!?()|/\n–]|\s-\s')


# Returns True if the keyword occurrence at text[start:end] is negated.
def _is_negated(text: str, start: int, end: int) -> bool:
    if _NEGATING_SUFFIX_RE.match(text[end:]):
        return True
    if re.search(r'\w*$', text[:start]).group() == "un":  # "unbeschädigt"
        return True
    before = re.sub(r'\w*$', '', text[:start])  # drop the start of the word the match sits in
    clause = _CLAUSE_BREAK_RE.split(before)[-1]
    if re.search(r'\bfrei von\s*$', clause):  # "frei von Pixelfehlern"
        return True
    raw = clause.split()
    words = [w for w in raw if w not in _CONJUNCTIONS]
    # In a coordinated list the negation reaches one word further:
    # "keine auffälligen Kratzer oder Beschädigungen"
    reach = 3 if any(w in _CONJUNCTIONS for w in raw[-4:]) else 2
    return any(w in _NEGATIONS for w in words[-reach:])


# Returns True if a blocked keyword occurs in the text at least once without being negated.
def _blocked_keyword_unnegated(kw: str, text: str) -> bool:
    return any(not _is_negated(text, m.start(), m.end()) for m in re.finditer(_keyword_pattern(kw), text))


# Things whose damage isn't a defect of the phone itself: the charging port
# ("Ladebuchse defekt", "Wackelkontakt" — almost always lint, a 2-minute fix)
# and accessories or packaging ("Panzerglas gesprungen", "Hülle kaputt", "OVP beschädigt").
_NOT_A_PHONE_DEFECT_RE = re.compile(
    r'lade(?:buchse|anschluss|port|stecker|kabel|gerät)|lightning|usb[- ]?c\b|wackelkontakt'
    r'|panzerglas|schutzglas|folie|hülle|huelle|case\b|ovp|verpackung|karton|box\b|kabel|netzteil'
)
# Like _CLAUSE_BREAK_RE, but "und"/"oder" also separate the parts, so in
# "Display defekt und Ladebuchse wackelt" the display defect still counts.
_PHRASE_BREAK_RE = re.compile(r'[,.;:!?()|/\n–]|\s-\s|\s(?:und|oder|sowie|aber)\s')


# Returns True if the keyword occurrence at text[start:end] is about the charging
# port or an accessory: such a term in the same word or within two words before or after it.
def _is_not_a_phone_defect(text: str, start: int, end: int) -> bool:
    word_start = re.search(r'\w*$', text[:start]).group()
    word_end = re.match(r'\w*', text[end:]).group()
    before = _PHRASE_BREAK_RE.split(text[:start - len(word_start)])[-1].split()[-2:]
    after = _PHRASE_BREAK_RE.split(text[end + len(word_end):])[0].split()[:2]
    word = word_start + text[start:end] + word_end
    return any(_NOT_A_PHONE_DEFECT_RE.search(w) for w in [word, *before, *after])


# Returns the defect keywords that describe real damage of the phone — not
# negated ("kein Sprung"), not the charging port ("Ladebuchse defekt") and not
# an accessory ("Panzerglas gesprungen").
def find_defects(listing: Listing, defect_keywords: list[str]) -> list[str]:
    text = _normalize_text(f"{listing.title} {listing.description}")
    found = []
    for kw in defect_keywords:
        for m in re.finditer(_keyword_pattern(kw), text):
            if not _is_negated(text, m.start(), m.end()) and not _is_not_a_phone_defect(text, m.start(), m.end()):
                found.append(kw)
                break
    return found


# Battery health as sellers state it: "Akku 78%", "Batteriekapazität: 78 %",
# "Akkuzustand liegt bei 81%", "Battery Health 79%" or "86% Akku". The number must
# sit right at the battery word with no comma or full stop in between, so
# "Akku hält 2 Tage, 50% Rabatt" isn't read as 50 %.
_BATTERY_WORD = r'(?:akku|batterie|battery|kapazität|kapazitaet)\w*(?:\s+health)?'
_PERCENT = r'\s*(?:%|prozent\b)'
_BATTERY_RES = [
    re.compile(rf'\b{_BATTERY_WORD}[^,.;!?\n%\d]{{0,25}}?(\d{{1,3}}){_PERCENT}'),
    re.compile(rf'(?<![\d.,])(\d{{1,3}}){_PERCENT}\s*(?:akku|batterie|battery|kapazität|kapazitaet)'),
]


# Returns the battery health in % stated in the listing, or None if it doesn't say.
def find_battery_capacity(listing: Listing) -> Optional[int]:
    text = f"{listing.title} {listing.description}".lower()
    hits = [(m.start(), int(m.group(1))) for rx in _BATTERY_RES for m in rx.finditer(text) if 1 <= int(m.group(1)) <= 100]
    return min(hits)[1] if hits else None


# Checks whether a listing matches the configured criteria.
# Returns (is_good, reason_string).
def is_good_deal(listing: Listing, search_config: dict, global_blocked: Optional[list[str]] = None) -> tuple[bool, str]:
    if global_blocked is None:
        global_blocked = []
    min_price = search_config.get("min_price", 0)
    max_price = search_config.get("max_price", 0)
    keywords_required: list[str] = search_config.get("keywords_required", [])
    variant_blocked: list[str] = search_config.get("keywords_blocked", [])

    text = _normalize_text(f"{listing.title} {listing.description}")

    # Price check
    if listing.price is None:
        return False, "Kein Preis angegeben"

    if max_price > 0 and listing.price > max_price:
        return False, f"Preis {listing.price}€ zu hoch (Max: {max_price}€)"

    if min_price > 0 and listing.price < min_price:
        return False, f"Preis {listing.price}€ zu niedrig (Min: {min_price}€)"

    # Global blocked keywords (damage, locks, pickup-only …) count anywhere in
    # the listing unless negated ("kein Wasserschaden", "iCloud frei").
    for kw in global_blocked:
        if _blocked_keyword_unnegated(kw, text):
            return False, f"Gesperrtes Keyword gefunden: '{kw}'"

    # Verify listing is actually about the searched product.
    query = search_config.get("query", "")
    tokens = text.split()
    span = _query_phrase_span(query, tokens) if query else None
    if query and span is None:
        return False, f"Suchbegriff '{query}' nicht als zusammenhängende Phrase gefunden"

    # When the title names a specific model, that first-named model is what's
    # sold — "iPhone 16 Pro, Tausch möglich ab iPhone 17" is no iPhone 17 and
    # "iPhone 12 Pro – Top Zustand" no 12 Pro Max, whatever the description says.
    if query:
        title_tokens = _normalize_text(listing.title).split()
        mention = _first_model_mention(query, title_tokens)
        if mention is not None:
            title_span = _query_phrase_span(query, title_tokens[mention:])
            if title_span is None or title_span[0] != 0:
                return False, f"Titel nennt ein anderes Modell als '{query}'"

    # Per-search blocked keywords are model variants ('pro', 'plus', 'fe' …).
    # They only count at the model mention — inside the matched phrase or the
    # two tokens after it — so "iPhone 13 Pro" is caught by search "iPhone 13",
    # but "iPhone 13, Hülle für 13 Pro dabei" or "S24, Tausch gegen S24 Ultra" are not.
    model_context = " ".join(tokens[span[0]:span[1] + 2]) if span else text
    for kw in variant_blocked:
        if _blocked_keyword_in(kw, model_context):
            return False, f"Gesperrtes Keyword gefunden: '{kw}'"

    # Required keywords
    for kw in keywords_required:
        if kw.lower() not in text:
            return False, f"Pflicht-Keyword fehlt: '{kw}'"

    return True, "Passt allen Kriterien"
