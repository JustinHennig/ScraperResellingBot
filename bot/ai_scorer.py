# Groq AI scorer: evaluates a listing's resale value, condition, and originality.
# Returns a score (1-10) and an optional warning string for non-original parts.
# Uses Groq's free tier (console.groq.com) with openai/gpt-oss-120b.
import json
import logging
import re
import threading
import time
from datetime import date
from typing import Optional
from groq import Groq

from bot.ratings import all_records

logger = logging.getLogger(__name__)

# Serialize Groq calls across threads to avoid hitting rate limits
_groq_lock = threading.Lock()

# Model — llama-3.3-70b-versatile was retired by Groq; openai/gpt-oss-120b is the
# closest available replacement for condition/originality judgment quality.
_GROQ_MODEL = "openai/gpt-oss-120b"

# System prompt: detailed scoring rubric and strict JSON output format
_SYSTEM = """Du bist ein Experte für die Bewertung von Gebrauchthandy-Inseraten auf deutschen Kleinanzeigenportalen im Hinblick auf Wiederverkaufsgewinn.

Deine Aufgabe ist es, zu bewerten, wie gut ein Inserat für jemanden ist, der das Gerät günstig KAUFEN und mit Gewinn WIEDERVERKAUFEN möchte.

Antworte ausschließlich mit validem JSON in genau diesem Format (kein Markdown, keine Erklärung):
{"score": <ganze Zahl 1-10>, "warning": "<string>"}

AKTUELLE MODELLE:
- Dein Wissen über Gerätemodelle ist veraltet. Es gibt neuere Modelle, als du kennst (z.B. iPhone 17, 17 Pro, 17 Pro Max, 17e, iPhone Air, Samsung Galaxy S25, S25 Edge, S26, Google Pixel 10, 10 Pro, 10 Pro XL, 10a). Das heutige Datum steht in der Nachricht.
- Ein dir unbekannter Modellname ist KEIN Hinweis auf Fälschung, Graumarkt oder ein „nicht existierendes Modell" — bewerte deswegen niemals ab und erwähne es nicht im Warnungsfeld. Graumarkt oder Fälschung nur bei den ausdrücklichen Hinweisen unter GRAUMARKT- ODER GEFÄLSCHTE GERÄTE.
- Das Inserat stammt aus einer Suche nach dem angegebenen „Gesuchten Modell". Ist das gesuchte Modell kein Smartphone (z.B. ein Raspberry Pi), bewerte das Gerät selbst sinngemäß nach denselben Kriterien. Reines Zubehör dafür (z.B. nur Gehäuse, Display, Netzteil, Erweiterungsplatine) zählt weiterhin als Zubehör (Score 1).

KALIBRIERUNG:
- Die Inserate sind bereits per Preisobergrenze vorgefiltert — eine ordentliche Gewinnmarge ist daher der Normalfall und allein kein Grund für 9-10.
- Ein typisches, ordentliches Inserat ohne Besonderheiten bekommt 6-7. 8 heißt klar überdurchschnittlich (sehr guter Zustand UND gute Marge).
- 9-10 nur für echte Ausnahmen: neuwertig oder OVP, Originalzubehör, keine Mängel UND Gewinn deutlich über 60 EUR. Nutze die ganze Skala.

BEWERTUNGSSKALA:
10 — Neuwertig / wie neu, vollständiges Originalzubehör, alle Originalteile, guter Preis, keine Warnsignale
8-9 — Guter Zustand, nur leichte Gebrauchsspuren, Originalteile, ausreichende Gewinnmarge
6-7 — Durchschnittlicher Zustand, sichtbare Kratzer oder Dellen, Zubehör fehlt teilweise, noch weiterverkäuflich
4-5 — Deutliche Schäden (gerissene Rückseite, tiefe Kratzer) oder Preis zu hoch für den beschriebenen Zustand
2-3 — Erhebliche Schäden (verbogener Rahmen, tote Pixel, starke Kratzer), Nicht-Originalteile oder stark überteuert
1   — Praktisch unverkäuflich: iCloud-/Google-gesperrt, komplett defekt, Wasserschaden, Gerät nur für Ersatzteile

FAKTOREN DIE DEN SCORE SENKEN:
- Display ausgetauscht (besonders kein Original-Display)
- Akku durch Nicht-Originalteil ersetzt
- Drittanbieter-Reparaturen erwähnt
- Ladekabel, Kabel oder Originalverpackung fehlt (kleine Abwertung)
- Verkäufer schreibt „Verkaufe auf Probe" oder beschreibt Zustand vage
- Geschätzter Gewinn unter 10 EUR (kaum rentabel)
- Negativer geschätzter Gewinn (Kaufpreis höher als eBay-Median → starke Abwertung)
- Gerät gesperrt (iCloud, Google-Konto, Netzsperre)
- Wasser-, Display- oder Rückseitenschaden (Riss, Sprung, Spinnennetz-Muster im Glas) — siehe GEWINNBERECHNUNG für die korrekte Preiseinordnung

FAKTOREN DIE DEN SCORE ERHÖHEN:
- „Neuwertig", „wie neu", „TOP Zustand", „1a Zustand"
- Originalzubehör vorhanden (Ladekabel, Box usw.)
- Originalakku und -teile
- Detaillierte, ehrliche Beschreibung oder klare Fotos beschrieben
- Geschätzter Gewinn über 30 EUR
- Geschätzter Gewinn über 60 EUR (erheblicher Aufschlag)
- Ladebuchse verstopft, nur induktives/kabelloses Laden möglich, oder Wackelkontakt am Ladeanschluss: Das ist fast immer nur Schmutz/Fusseln in der Buchse — ein 2-Minuten-Fix (auspusten/vorsichtig reinigen), kein Hardware-Schaden. Verkäufer unterschätzen den Zustand deshalb oft und der Preis ist entsprechend günstig. Das ist ein POSITIVER Faktor (Score erhöhen, nicht senken) und gehört NICHT ins Warnungsfeld

GEWINNBERECHNUNG (wenn geschätzter eBay-Verkaufspreis angegeben):
- Liegt der Kaufpreis bereits nahe am oder über dem eBay-Median → Score entsprechend senken
- Liegt der Kaufpreis stark unter dem eBay-Median → Score entsprechend erhöhen
- Bei Defekten (Display-Riss/-Sprung, Rückseitenschaden, Pixelfehler, schwarze Flecken, sonstige defekte Teile): Solche Inserate werden nur weitergeleitet, wenn der Kaufpreis deutlich (ca. 30 % oder mehr) unter dem Preis liegt, der für ein intaktes Gerät noch ein gutes Angebot wäre — dann lohnen sich Reparatur und Wiederverkauf. Bewerte sie deshalb NICHT als Verlustgeschäft, sondern nach Schwere des Defekts und Höhe des Preisnachlasses: leichter, günstig reparierbarer Defekt (kleiner Riss, Rückglas, einzelne Pixelfehler) → 5-7, schwerer Defekt (Display komplett defekt, mehrere Defekte) → 3-4. Score 1-2 nur, wenn das Gerät praktisch unbrauchbar ist. Nenne den Defekt immer kurz im Warnungsfeld

GRAUMARKT- ODER GEFÄLSCHTE GERÄTE (Score IMMER 1-2, unabhängig von Preis oder Zustand):
- Hinweise wie „chinesische Version", „China-Version", „Asia-Version", „Replika", „Fake", „Kopie", „kein Original-Gerät", „Hong-Kong-Version" im Titel oder der Beschreibung bedeuten Graumarktware oder eine Fälschung — für Wiederverkauf über normale Kanäle praktisch wertlos, selbst bei niedrigem Preis
- In diesem Fall Score konsequent auf 1-2 setzen und im Warnungsfeld den Grund nennen (z.B. „Chinesische Version — vermutlich Graumarktware/Fälschung, kein reguläres Gerät")

NUR ZUBEHÖR STATT GERÄT (Score IMMER 1, unabhängig vom Preis):
- Falls das Inserat erkennbar NUR ein Zubehörteil ist (z.B. nur Hülle/Case, Panzerglas, Ladekabel, Powerbank, Kopfhörer) und NICHT das Smartphone selbst — auch wenn der Modellname im Titel steht (z.B. „Hülle für iPhone 13") — Score auf 1 setzen und im Warnungsfeld vermerken (z.B. „Nur Zubehör (Hülle), kein Gerät")
- Wird das Zubehör dagegen ZUSÄTZLICH zu einem tatsächlich verkauften Gerät erwähnt (z.B. „iPhone 13, 128GB, inkl. Hülle und Ladekabel"), ist das kein Problem, sondern sogar ein leichter Pluspunkt (siehe FAKTOREN DIE DEN SCORE ERHÖHEN)

WARNUNGSFELD:
- Falls Display, Akku oder ein anderes internes Bauteil ausgetauscht wurde oder kein Original ist, kurz auf Deutsch beschreiben (z.B. „Display ersetzt (kein Original)", „Akku getauscht")
- Falls das Gerät möglicherweise gesperrt ist oder Kontoprobleme bestehen, erwähnen (z.B. „Mögliche iCloud-Sperre")
- Falls physischer Schaden (Riss, Sprung) vorliegt, kurz nennen und ob der Preis dafür angemessen ist (z.B. „Display-Riss — bei diesem Preis kein Gewinn")
- Ladebuchsen-Probleme (verstopft, Wackelkontakt, nur kabelloses Laden) NICHT im Warnungsfeld erwähnen — siehe FAKTOREN DIE DEN SCORE ERHÖHEN, das ist kein Substanzschaden
- Warnungsfeld leer lassen („") wenn alles original und unverändert wirkt"""

# User message — description capped at 600 chars for better context with the stronger model
_USER_TEMPLATE = """Datum heute: {today}
Gesuchtes Modell: {search_name}
Titel: {title}
Preis: {price} EUR{vb}{sell_price_line}
Beschreibung: {description}"""

# Payment methods that strip buyer protection — a common scam pattern.
# "PayPal Freunde" (Friends & Family) has no PayPal buyer protection;
# "nur Überweisung" (bank transfer only) has no protection at all.
_PAYPAL_FRIENDS_RE = re.compile(
    r'paypal[^.]{0,25}(?:freunde|f\s*&\s*f\b|f\+f\b)|(?:freunde|f\s*&\s*f\b)[^.]{0,25}paypal',
    re.IGNORECASE,
)
_BANK_TRANSFER_ONLY_RE = re.compile(
    r'nur\s+(?:bar\s+oder\s+)?(?:per\s+)?(?:vorab[- ]?)?überweisung|ausschließlich\s+(?:per\s+)?überweisung',
    re.IGNORECASE,
)


# Deterministic (non-AI) check for scam-typical payment demands — run
# independently of the Groq API so it always applies, even without a key
# configured or if the AI call fails. A hit means the listing should be
# skipped outright, not just flagged (no buyer protection == not worth the risk).
def payment_red_flag(text: str) -> str:
    if _PAYPAL_FRIENDS_RE.search(text):
        return "Nur PayPal Freunde gewünscht — kein Käuferschutz"
    if _BANK_TRANSFER_ONLY_RE.search(text):
        return "Nur Überweisung gewünscht — kein Käuferschutz"
    return ""


# Unix time until which Groq calls are skipped because a daily limit
# (tokens/requests per day) was hit — retrying before then can't succeed.
_groq_paused_until = 0.0


# Parses Groq's "Please try again in 1h2m5.3s" (or "retry in 4.2s") into seconds.
def _groq_retry_seconds(err_str: str) -> Optional[float]:
    m = re.search(r"(?:try again|retry)[_ ]in[_ ](?:(\d+)h)?(?:(\d+)m)?(?:(\d+\.?\d*)s)?", err_str, re.IGNORECASE)
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


# Calls the Groq chat API with retry-on-429, serialized across threads via
# _groq_lock. Returns the raw response text, or None on failure (logged).
def _call_groq(client: Groq, system: str, user_msg: str, max_tokens: int, log_ctx: str) -> Optional[str]:
    global _groq_paused_until
    if time.time() < _groq_paused_until:
        return None
    for attempt in range(3):
        with _groq_lock:
            try:
                chat = client.chat.completions.create(
                    model=_GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0,
                    # gpt-oss models spend part of max_tokens on hidden reasoning
                    # before the JSON answer, so headroom beyond the answer's own
                    # length is needed at low effort.
                    max_tokens=max_tokens,
                    reasoning_effort="low",
                )
                return (chat.choices[0].message.content or "").strip()
            except Exception as e:
                err_str = str(e)
                retry_after = _groq_retry_seconds(err_str)
                # Daily quota used up: pause AI scoring until it resets instead of
                # retrying (each retry blocks every search thread for the wait)
                if "429" in err_str and "per day" in err_str:
                    _groq_paused_until = time.time() + (retry_after or 3600) + 5
                    logger.warning(
                        f"Groq daily limit reached — AI scoring paused until "
                        f"{time.strftime('%H:%M', time.localtime(_groq_paused_until))}, listings are sent without a score"
                    )
                    return None
                if "429" in err_str and attempt < 2:
                    wait = retry_after + 1 if retry_after is not None else 15
                    logger.warning(f"Groq 429 for {log_ctx} — retrying in {wait:.0f}s (attempt {attempt + 1}/3)")
                    time.sleep(wait)
                else:
                    logger.warning(f"Groq call failed for {log_ctx}: {e}")
                    return None
    return None


# Strips markdown code fences a model sometimes wraps its JSON answer in.
def _strip_json_fences(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", raw)


# Calls Groq API and returns (score, warning). Returns (None, "") on any failure.
# search_name tells the model which product was searched for, so it doesn't
# mistake models newer than its training data for fakes.
def score_listing(listing, api_key: str, search_name: str = "") -> tuple:
    if not api_key:
        logger.warning("Groq API key not configured — skipping AI score")
        return None, ""

    # 600 chars gives the stronger model enough context to make accurate condition judgments
    description = (listing.description or "")[:600] or "Keine Beschreibung"
    vb_suffix = " (VB)" if listing.negotiable else ""
    price_str = str(listing.price) if listing.price is not None else "Preis auf Anfrage"

    # Include estimated eBay sell price and profit if available
    if listing.estimated_sell_price is not None:
        sell_price_line = f"\nGeschätzter eBay-Verkaufspreis (Median): {listing.estimated_sell_price:.0f} EUR"
        if listing.estimated_profit is not None:
            sell_price_line += f"\nGeschätzter Gewinn: {listing.estimated_profit:+d} EUR"
    else:
        sell_price_line = ""

    user_msg = _USER_TEMPLATE.format(
        today=date.today().strftime("%d.%m.%Y"),
        search_name=search_name or "unbekannt",
        title=listing.title,
        price=price_str,
        vb=vb_suffix,
        sell_price_line=sell_price_line,
        description=description,
    )

    client = Groq(api_key=api_key)
    raw = _call_groq(client, _SYSTEM, user_msg, max_tokens=300, log_ctx=f"listing {listing.listing_id}")
    if raw is None:
        return None, ""

    try:
        data = json.loads(_strip_json_fences(raw))
        score = int(data.get("score", 0))
        warning = str(data.get("warning", "")).strip()

        # Clamp score to valid range
        score = max(1, min(10, score))
        return score, warning

    except Exception as e:
        logger.warning(f"Groq response parse failed for listing {listing.listing_id}: {e} — raw: {raw[:100]}")
        return None, ""


# ---------------------------------------------------------------------------
# Preference filter — learns patterns from past 👍/👎 ratings (bot/ratings.py)
# and predicts whether the user would want to buy a new listing.
# ---------------------------------------------------------------------------

# Require at least this many 👎-rated examples before the filter acts — with
# fewer, one noisy rating could wrongly block unrelated listings.
MIN_DISLIKES_FOR_FILTER = 3
_MAX_EXAMPLES_PER_SIDE = 12

_PREFERENCE_SYSTEM = """Du erkennst Muster in den bisherigen Kaufentscheidungen eines Wiederverkäufers auf Kleinanzeigen und sagst voraus, ob ihn ein neues Angebot interessieren würde.

Er hat bereits mehrere Angebote mit 👍 (würde kaufen) oder 👎 (würde nicht kaufen) bewertet. Suche nach wiederkehrenden Mustern in den 👎-Beispielen (z.B. bestimmtes Modell, Zustand, Preislage, Formulierungen im Titel), die auch auf das neue Angebot zutreffen könnten.

Antworte ausschließlich mit validem JSON in genau diesem Format (kein Markdown, keine Erklärung):
{"would_buy": true oder false, "reason": "<kurzer Grund auf Deutsch, max. 15 Wörter>"}

Sei zurückhaltend mit false — nur wenn ein klares Muster aus den 👎-Beispielen zutrifft, nicht bei bloßer Unsicherheit oder fehlender Ähnlichkeit."""


# Splits recorded listings into ones the user liked vs. disliked, based on
# their most recent Telegram rating. Caps each list to keep the prompt short.
def _rating_examples() -> tuple[list[dict], list[dict]]:
    liked, disliked = [], []
    for record in all_records().values():
        verdicts = [r.get("rating") for r in record.get("ratings", {}).values()]
        if "down" in verdicts:
            disliked.append(record)
        elif "up" in verdicts:
            liked.append(record)
    return liked[-_MAX_EXAMPLES_PER_SIDE:], disliked[-_MAX_EXAMPLES_PER_SIDE:]


def _format_examples(records: list[dict]) -> str:
    lines = [f"- {r.get('title', '?')} ({r.get('price', '?')} EUR)" for r in records]
    return "\n".join(lines) if lines else "(keine)"


# Predicts whether the user would want to buy this listing, based on patterns
# in their past 👍/👎 ratings. Returns (True, "") — i.e. keep the listing —
# whenever there isn't enough 👎 history yet or the AI call/parse fails; this
# filter only ever removes a listing once it has real signal to act on.
def matches_user_preferences(listing, api_key: str) -> tuple[bool, str]:
    liked, disliked = _rating_examples()
    if len(disliked) < MIN_DISLIKES_FOR_FILTER:
        return True, ""

    user_msg = (
        f"Bisher abgelehnt (👎):\n{_format_examples(disliked)}\n\n"
        f"Bisher gewollt (👍):\n{_format_examples(liked)}\n\n"
        f"Neues Angebot: {listing.title} ({listing.price} EUR)\n"
        f"Beschreibung: {(listing.description or '')[:300] or 'Keine Beschreibung'}"
    )

    client = Groq(api_key=api_key)
    raw = _call_groq(client, _PREFERENCE_SYSTEM, user_msg, max_tokens=200, log_ctx=f"preference filter {listing.listing_id}")
    if raw is None:
        return True, ""

    try:
        data = json.loads(_strip_json_fences(raw))
        return bool(data.get("would_buy", True)), str(data.get("reason", "")).strip()
    except Exception as e:
        logger.warning(f"Preference filter parse failed for listing {listing.listing_id}: {e} — raw: {raw[:100]}")
        return True, ""
