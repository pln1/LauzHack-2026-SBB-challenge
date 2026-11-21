"""Station lookup: names / UIC numbers -> coordinates and canton.

Coordinates come from ``data/stations.csv``, a compact extract (Switzerland and border
regions) of the open trainline-eu station list (ODbL). VIA reports stations as UIC
numbers ("NUMBER" in the SBB DiDok list), which this table resolves.

Cantons are NOT in that source. They are estimated with a nearest-reference-town
lookup (``CANTON_REFS``) and should be treated as approximate near canton borders.
"""
from __future__ import annotations

import csv
import difflib
import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

SWISS_CENTER = (46.8182, 8.2275)


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    country: str
    lat: float
    lng: float

    @property
    def canton(self) -> str:
        return canton_for(self.lat, self.lng, self.country)

    @property
    def coords(self) -> list[float]:
        return [round(self.lat, 5), round(self.lng, 5)]


# Reference places (lat, lng, canton). Used for nearest-neighbour canton estimation.
CANTON_REFS: list[tuple[float, float, str]] = [
    # ZH
    (47.378, 8.540, "ZH"), (47.500, 8.724, "ZH"), (47.348, 8.718, "ZH"), (47.522, 8.540, "ZH"),
    (47.229, 8.674, "ZH"), (47.405, 8.400, "ZH"), (47.426, 8.687, "ZH"), (47.296, 8.564, "ZH"),
    (47.270, 8.644, "ZH"), (47.397, 8.596, "ZH"), (47.604, 8.543, "ZH"), (47.330, 8.800, "ZH"),
    # BE
    (46.949, 7.439, "BE"), (46.755, 7.630, "BE"), (47.132, 7.243, "BE"), (47.061, 7.622, "BE"),
    (46.690, 7.869, "BE"), (47.217, 7.786, "BE"), (46.686, 7.680, "BE"), (47.153, 6.998, "BE"),
    (47.280, 7.371, "BE"), (47.074, 7.306, "BE"), (46.891, 7.498, "BE"), (46.933, 7.507, "BE"),
    (46.588, 7.648, "BE"), (46.554, 7.374, "BE"), (46.957, 7.481, "BE"), (46.730, 8.180, "BE"),
    (46.475, 7.285, "BE"), (47.060, 7.100, "BE"),
    # LU
    (47.050, 8.310, "LU"), (47.171, 8.098, "LU"), (47.168, 8.292, "LU"), (47.059, 8.074, "LU"),
    (47.118, 8.193, "LU"), (46.990, 7.950, "LU"),
    # UR / SZ / OW / NW / ZG / GL
    (46.880, 8.640, "UR"), (46.819, 8.651, "UR"), (46.667, 8.588, "UR"),
    (47.021, 8.653, "SZ"), (47.049, 8.548, "SZ"), (47.202, 8.778, "SZ"), (47.128, 8.744, "SZ"), (47.083, 8.440, "SZ"),
    (46.896, 8.246, "OW"), (46.958, 8.366, "NW"),
    (47.173, 8.515, "ZG"), (47.101, 8.517, "ZG"), (47.196, 8.528, "ZG"),
    (47.040, 9.068, "GL"), (47.136, 9.060, "GL"), (46.920, 9.000, "GL"),
    # FR
    (46.803, 7.151, "FR"), (46.618, 7.057, "FR"), (46.697, 6.919, "FR"), (46.929, 7.117, "FR"),
    (46.849, 6.846, "FR"), (46.849, 7.190, "FR"), (46.760, 7.000, "FR"),
    # SO / BS / BL
    (47.204, 7.542, "SO"), (47.352, 7.908, "SO"), (47.190, 7.390, "SO"), (47.315, 7.690, "SO"),
    (47.547, 7.590, "BS"),
    (47.484, 7.731, "BL"), (47.521, 7.693, "BL"), (47.462, 7.812, "BL"), (47.465, 7.853, "BL"),
    (47.452, 7.886, "BL"), (47.420, 7.500, "BL"),
    # SH / AR / AI / SG
    (47.698, 8.633, "SH"), (47.684, 8.613, "SH"), (47.659, 8.857, "SH"),
    (47.386, 9.279, "AR"), (47.330, 9.409, "AI"),
    (47.423, 9.370, "SG"), (47.225, 8.816, "SG"), (47.462, 9.044, "SG"), (47.168, 9.478, "SG"),
    (47.045, 9.446, "SG"), (47.452, 9.637, "SG"), (47.226, 8.982, "SG"), (47.477, 9.490, "SG"),
    (47.300, 9.090, "SG"),
    # GR
    (46.853, 9.529, "GR"), (46.967, 9.555, "GR"), (46.800, 9.840, "GR"), (46.498, 9.845, "GR"),
    (46.700, 8.850, "GR"), (46.320, 10.060, "GR"), (46.700, 9.440, "GR"), (46.640, 10.200, "GR"),
    # AG
    (47.391, 8.051, "AG"), (47.476, 8.307, "AG"), (47.481, 8.209, "AG"), (47.391, 8.177, "AG"),
    (47.459, 8.316, "AG"), (47.261, 8.205, "AG"), (47.288, 7.946, "AG"), (47.350, 8.277, "AG"),
    (47.554, 7.794, "AG"), (47.588, 8.290, "AG"), (47.570, 8.326, "AG"), (47.270, 8.340, "AG"),
    (47.603, 8.236, "AG"),
    # TG
    (47.558, 8.896, "TG"), (47.567, 9.104, "TG"), (47.650, 9.175, "TG"), (47.565, 9.380, "TG"),
    (47.494, 8.904, "TG"), (47.463, 8.964, "TG"), (47.515, 9.430, "TG"),
    # TI
    (46.195, 9.029, "TI"), (46.005, 8.947, "TI"), (46.172, 8.801, "TI"), (45.832, 9.031, "TI"),
    (45.869, 8.979, "TI"), (46.359, 8.970, "TI"), (46.528, 8.610, "TI"), (45.847, 9.008, "TI"),
    (46.172, 9.000, "TI"),
    # VD
    (46.517, 6.629, "VD"), (46.782, 6.641, "VD"), (46.507, 6.495, "VD"), (46.383, 6.239, "VD"),
    (46.463, 6.843, "VD"), (46.435, 6.910, "VD"), (46.317, 6.964, "VD"), (46.537, 6.579, "VD"),
    (46.317, 6.192, "VD"), (46.822, 6.938, "VD"), (46.542, 6.837, "VD"), (46.495, 6.766, "VD"),
    (46.707, 6.570, "VD"), (46.613, 6.508, "VD"), (46.712, 6.374, "VD"), (46.709, 6.840, "VD"),
    (46.650, 6.530, "VD"), (46.746, 6.608, "VD"), (46.551, 6.556, "VD"), (46.580, 6.210, "VD"),
    (46.252, 7.010, "VD"),
    # VS
    (46.227, 7.359, "VS"), (46.319, 7.988, "VS"), (46.294, 7.881, "VS"), (46.292, 7.532, "VS"),
    (46.106, 7.077, "VS"), (46.217, 7.003, "VS"), (46.255, 6.954, "VS"), (46.024, 7.748, "VS"),
    (46.210, 7.270, "VS"), (46.310, 7.630, "VS"), (46.430, 8.200, "VS"),
    # NE / GE / JU
    (46.997, 6.936, "NE"), (47.100, 6.828, "NE"), (47.057, 6.749, "NE"), (47.057, 7.070, "NE"),
    (46.940, 6.670, "NE"),
    (46.210, 6.142, "GE"), (46.232, 6.109, "GE"), (46.187, 6.130, "GE"), (46.196, 6.196, "GE"),
    (47.363, 7.350, "JU"), (47.415, 7.076, "JU"), (47.256, 6.996, "JU"),
]

# Names used in SBB texts that differ from the station list spelling.
NAME_ALIASES: dict[str, str] = {
    "bern": "Bern Hbf",
    "berne": "Bern Hbf",
    "fribourg/freiburg": "Fribourg",
    "freiburg": "Fribourg",
    "st. gallen": "St-Gallen",
    "st.gallen": "St-Gallen",
    "st gallen": "St-Gallen",
    "geneve": "Genève",
    "geneva": "Genève",
    "genf": "Genève",
    "ginevra": "Genève",
    "geneve-aeroport": "Genève Aéroport",
    "geneve aeroport": "Genève Aéroport",
    "geneva airport": "Genève Aéroport",
    "zurich": "Zürich HB",
    "zuerich": "Zürich HB",
    "zurigo": "Zürich HB",
    "zurich airport": "Zürich Flughafen",
    "zurich flughafen": "Zürich Flughafen",
    "basel": "Basel SBB",
    "bale": "Basel SBB",
    "basilea": "Basel SBB",
    "lucerne": "Luzern",
    "lucerna": "Luzern",
    "sierre/siders": "Sierre/Siders",
    "sierre": "Sierre/Siders",
    "siders": "Sierre/Siders",
    "biel": "Biel/Bienne",
    "bienne": "Biel/Bienne",
    "la chaux-de-fonds": "La-Chaux-de-Fonds",
    "como s. giovanni": "Como San Giovanni",
    "como s giovanni": "Como San Giovanni",
    "effrektion": "Effretikon",  # typo seen in the KULT export
    "milan": "Milano Centrale",
    "milano": "Milano Centrale",
    "neuchatel": "Neuchâtel",
    "bellinzone": "Bellinzona",
    "lugano": "Lugano",
    "sion": "Sion",
    "sitten": "Sion",
    "brig": "Brig",
    "brigue": "Brig",
    "visp": "Visp",
    "viege": "Visp",
    "annemasse": "Annemasse",
    "interlaken": "Interlaken Ost",
    "roche": "Roche VD",
    "castione": "Castione-Arbedo",
    "konstanz db": "Konstanz",
    "bercher vd": "Bercher",
    "thun": "Thun",
    "chur": "Chur",
}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def distance_km(a, b) -> float:
    return _haversine_km((a[0], a[1]), (b[0], b[1]))


def canton_for(lat: float, lng: float, country: str = "CH") -> str:
    if country and country != "CH":
        return country  # foreign border station: return ISO country (FR, IT, DE, AT)
    best = min(CANTON_REFS, key=lambda r: (r[0] - lat) ** 2 + ((r[1] - lng) * 0.68) ** 2)
    return best[2]


class StationIndex:
    def __init__(self, path: Path):
        self.by_code: dict[str, Station] = {}
        self.by_name: dict[str, Station] = {}
        self._ch_names: list[str] = []
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        st = Station(row["code"], row["name"], row["country"], float(row["lat"]), float(row["lng"]))
                    except (KeyError, ValueError):
                        continue
                    self.by_code.setdefault(st.code, st)
                    for alt in (row.get("alt_codes") or "").split("|"):
                        if alt:
                            self.by_code.setdefault(alt, st)
                    key = normalize(st.name)
                    # Prefer CH and lower (main-line) codes for duplicate names
                    cur = self.by_name.get(key)
                    if cur is None or (cur.country != "CH" and st.country == "CH") or (
                        cur.country == st.country and int(st.code) < int(cur.code)
                    ):
                        self.by_name[key] = st
        self._names = list(self.by_name.keys())

    def by_uic(self, code) -> Station | None:
        if code is None:
            return None
        try:
            code = str(int(float(code)))
        except (TypeError, ValueError):
            return None
        return self.by_code.get(code)

    @lru_cache(maxsize=4096)
    def lookup(self, name: str) -> Station | None:
        if not name:
            return None
        raw = name.strip().strip(",.;:")
        key = normalize(raw)
        if not key:
            return None
        if key in NAME_ALIASES:
            st = self.by_name.get(normalize(NAME_ALIASES[key]))
            if st:
                return st
        if key in self.by_name:
            return self.by_name[key]
        # "Gümligen, Bahnhof (Bus)" -> "Gümligen"; "Genève (Cornavin)" -> "Genève"
        for cut in (",", "(", " gare", " bahnhof", " stazione"):
            if cut in key:
                short = key.split(cut)[0].strip()
                if short and short != key:
                    st = self.lookup(short)
                    if st:
                        return st
        # trailing canton / operator suffix: "Bercher VD", "Konstanz DB"
        m = re.match(r"^(.*\S)\s+(?:[a-z]{2}|db|sbb|cff|ffs)$", key)
        if m and m.group(1) in self.by_name:
            return self.by_name[m.group(1)]
        if "/" in key:
            for part in key.split("/"):
                st = self.by_name.get(part.strip())
                if st:
                    return st
        if key in NAME_ALIASES:
            return self.by_name.get(normalize(NAME_ALIASES[key]))
        match = difflib.get_close_matches(key, self._names, n=1, cutoff=0.88)
        return self.by_name[match[0]] if match else None

    def find_in_text(self, text: str, max_results: int = 4) -> list[Station]:
        """Find station names mentioned in free text (used for trip-context extraction)."""
        if not text:
            return []
        norm = " " + normalize(text) + " "
        found: list[tuple[int, Station]] = []
        candidates = list(NAME_ALIASES.keys()) + [n for n in self._names if len(n) >= 4]
        for cand in candidates:
            pattern = r"(?<![a-z0-9])" + re.escape(cand) + r"(?![a-z0-9])"
            m = re.search(pattern, norm)
            if m:
                st = self.lookup(cand)
                if st and all(st.code != f.code for _, f in found):
                    found.append((m.start(), st))
        # Prefer longer (more specific) names; keep text order
        found.sort(key=lambda x: x[0])
        dedup: list[Station] = []
        for _, st in found:
            if st not in dedup:
                dedup.append(st)
        return dedup[:max_results]


@lru_cache
def get_station_index() -> StationIndex:
    return StationIndex(get_settings().stations_file)
