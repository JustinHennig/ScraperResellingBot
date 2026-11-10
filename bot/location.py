# Postcode distances for the "pickup is fine near home" rule.
# plz_de.csv holds German postcode centroids (GeoNames, CC BY 4.0), so distances
# are between the centres of two postcode areas, not exact addresses.
import csv
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_PLZ_FILE = Path(__file__).parent / "plz_de.csv"


@lru_cache(maxsize=1)
def _coordinates() -> dict[str, tuple[float, float]]:
    with _PLZ_FILE.open(encoding="utf-8") as f:
        return {row[0]: (float(row[1]), float(row[2])) for row in csv.reader(f) if row and not row[0].startswith("#")}


# Returns the 5-digit postcode in a Kleinanzeigen location like "60438 Frankfurt am Main".
def postcode_of(location: str) -> Optional[str]:
    m = re.search(r'\b(\d{5})\b', location or "")
    return m.group(1) if m else None


# Great-circle distance in km between two postcode centres, or None if one is unknown.
def distance_km(plz_a: str, plz_b: str) -> Optional[float]:
    coords = _coordinates()
    if plz_a not in coords or plz_b not in coords:
        return None
    (lat1, lon1), (lat2, lon2) = coords[plz_a], coords[plz_b]
    rad = math.pi / 180
    h = (math.sin((lat2 - lat1) * rad / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin((lon2 - lon1) * rad / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


# True if the listing's location lies within the configured pickup area,
# e.g. {"postcode": "60438", "radius_km": 5}.
def within_pickup_area(location: str, area: Optional[dict]) -> bool:
    if not area:
        return False
    plz = postcode_of(location)
    dist = distance_km(plz, str(area.get("postcode", ""))) if plz else None
    return dist is not None and dist <= area.get("radius_km", 0)
