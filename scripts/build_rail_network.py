"""Build data/rail_ch.json: a compact railway line network for Switzerland, used to draw disruptions and detours
along real track geometry instead of straight lines.

Sources (pick one):
  --source naturalearth   Natural Earth 10m railroads (public domain), downloaded from GitHub. Default. Works behind
                          proxies that only allow GitHub. Generalised geometry: typically within 30-350 m of a station,
                          up to ~2 km inside big station complexes.
  --source overpass       OpenStreetMap railway=rail/light_rail via the Overpass API (ODbL). Much more detailed, but
                          needs open internet access. Run it once on a machine with internet, commit data/rail_ch.json.
  --input FILE            A GeoJSON file with (Multi)LineString features you already have (e.g. an SBB track export).

The result is clipped to Switzerland plus a 15 km border strip and stored as {"lines": [[[lat, lon], ...], ...]}.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BBOX = (5.55, 45.60, 10.85, 48.05)  # west, south, east, north: Switzerland + border strip
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_railroads.geojson"


def _fetch(url: str, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "rail-network-builder"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _lines_from_geojson(gj: dict) -> list[list[list[float]]]:
    out = []
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") == "LineString":
            parts = [g["coordinates"]]
        elif g.get("type") == "MultiLineString":
            parts = g["coordinates"]
        else:
            continue
        out.extend(parts)
    return out


def _clip(lines: list[list[list[float]]]) -> list[list[list[float]]]:
    w, s, e, n = BBOX
    res = []
    for line in lines:
        cur: list[list[float]] = []
        for x, y in ((p[0], p[1]) for p in line):
            if w <= x <= e and s <= y <= n:
                cur.append([round(y, 5), round(x, 5)])  # store as [lat, lon]
            elif len(cur) >= 2:
                res.append(cur)
                cur = []
            else:
                cur = []
        if len(cur) >= 2:
            res.append(cur)
    return res


def from_overpass() -> list[list[list[float]]]:
    w, s, e, n = BBOX
    q = f'[out:json][timeout:180];way["railway"~"^(rail|light_rail)$"]["service"!~"."]({s},{w},{n},{e});out geom;'
    data = json.loads(_fetch("https://overpass-api.de/api/interpreter", ("data=" + q).encode()))
    return [[[p["lon"], p["lat"]] for p in el["geometry"]] for el in data["elements"] if el.get("geometry")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["naturalearth", "overpass"], default="naturalearth")
    ap.add_argument("--input", help="GeoJSON file with LineString features (overrides --source)")
    ap.add_argument("--out", default=str(ROOT / "data" / "rail_ch.json"))
    args = ap.parse_args()
    if args.input:
        lines = _lines_from_geojson(json.loads(Path(args.input).read_text()))
        source = f"file:{Path(args.input).name}"
    elif args.source == "overpass":
        lines, source = from_overpass(), "OpenStreetMap contributors (ODbL), railway=rail via Overpass"
    else:
        lines, source = _lines_from_geojson(json.loads(_fetch(NE_URL))), "Natural Earth 10m railroads (public domain)"
    clipped = _clip(lines)
    out = {"source": source, "bbox": BBOX, "lines": clipped}
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")))
    pts = sum(len(x) for x in clipped)
    print(f"wrote {args.out}: {len(clipped)} lines, {pts} points, {Path(args.out).stat().st_size / 1024:.0f} KB ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
