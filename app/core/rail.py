"""Railway graph for Switzerland: snap stations to the track and route along it.

The network comes from ``data/rail_ch.json`` (built by ``scripts/build_rail_network.py``; Natural Earth by default,
OpenStreetMap if you ran the Overpass option). Used at ETL time to turn "station A to station B" into the real track
path, and to build detours as a path along *other* lines while the disrupted track is blocked. Everything falls back
to straight lines when the network is missing, a station is too far from any track, or no path exists.

Coordinates are ``(lat, lon)`` everywhere in this module.
"""
from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.core.config import PROJECT_ROOT

Point = tuple[float, float]
CELL = 0.02  # spatial grid cell in degrees (about 2 km)


def _k(lat: float) -> tuple[float, float]:
    return 111_320.0 * math.cos(math.radians(lat)), 110_540.0


def dist_m(a: Point, b: Point) -> float:
    kx, ky = _k((a[0] + b[0]) / 2)
    return math.hypot((a[1] - b[1]) * kx, (a[0] - b[0]) * ky)


def _project(p: Point, a: Point, b: Point) -> tuple[float, Point, float]:
    """Closest point of segment a-b to p: (t in 0..1, point, distance in metres)."""
    kx, ky = _k(p[0])
    ax, ay = (a[1] - p[1]) * kx, (a[0] - p[0]) * ky
    bx, by = (b[1] - p[1]) * kx, (b[0] - p[0]) * ky
    dx, dy = bx - ax, by - ay
    ln = dx * dx + dy * dy
    t = 0.0 if ln == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / ln))
    q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return t, q, math.hypot(ax + t * dx, ay + t * dy)


def simplify(points: list[Point], tol_m: float) -> list[Point]:
    """Douglas-Peucker in metres."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, idx = 0.0, -1
        for i in range(lo + 1, hi):
            d = _project(points[i], points[lo], points[hi])[2]
            if d > worst:
                worst, idx = d, i
        if idx != -1 and worst > tol_m:
            keep[idx] = True
            stack += [(lo, idx), (idx, hi)]
    return [p for p, k in zip(points, keep) if k]


def path_length_m(points: list[Point]) -> float:
    return sum(dist_m(a, b) for a, b in zip(points, points[1:]))


@dataclass
class Snap:
    u: int
    v: int
    t: float
    point: Point
    dist_m: float


@dataclass
class Route:
    points: list[Point]
    nodes: list[int] = field(default_factory=list)
    length_m: float = 0.0
    ok: bool = True
    snap_m: float = 0.0  # worst station-to-track distance on this route
    detail: str = ""


class RailNetwork:
    def __init__(self, lines: list[list[list[float]]], source: str = "") -> None:
        self.source = source
        self.coords: list[Point] = []
        self.adj: list[dict[int, float]] = []
        self._keys: dict[tuple[int, int], int] = {}
        for line in lines:
            prev = None
            for lat, lon in line:
                n = self._node((lat, lon))
                if prev is not None and prev != n:
                    self._edge(prev, n)
                prev = n
        self._grid: dict[tuple[int, int], set[tuple[int, int]]] = {}
        self._reindex()
        self._fix_junctions()
        self._reindex()

    # ---- construction
    def _node(self, p: Point) -> int:
        key = (round(p[0] * 10_000), round(p[1] * 10_000))  # merge nodes closer than ~10 m
        i = self._keys.get(key)
        if i is None:
            i = len(self.coords)
            self._keys[key] = i
            self.coords.append(p)
            self.adj.append({})
        return i

    def _edge(self, a: int, b: int) -> None:
        w = dist_m(self.coords[a], self.coords[b])
        self.adj[a][b] = w
        self.adj[b][a] = w

    def _reindex(self) -> None:
        self._grid = {}
        for u, nb in enumerate(self.adj):
            for v in nb:
                if u < v:
                    a, b = self.coords[u], self.coords[v]
                    for iy in range(int(min(a[0], b[0]) / CELL), int(max(a[0], b[0]) / CELL) + 1):
                        for ix in range(int(min(a[1], b[1]) / CELL), int(max(a[1], b[1]) / CELL) + 1):
                            self._grid.setdefault((iy, ix), set()).add((u, v))

    def _fix_junctions(self, tol_m: float = 120.0) -> None:
        """Line ends that touch another line mid-segment (T junctions) are joined by splitting that segment."""
        ends = [n for n, nb in enumerate(self.adj) if len(nb) == 1]
        for n in ends:
            best = None
            for u, v in self._candidates(self.coords[n], tol_m):
                if n in (u, v) or v not in self.adj[u]:
                    continue
                t, q, d = _project(self.coords[n], self.coords[u], self.coords[v])
                if d <= tol_m and (best is None or d < best[0]):
                    best = (d, u, v, t, q)
            if best is None:
                continue
            _, u, v, t, q = best
            if t < 0.02:
                self._edge(n, u)
            elif t > 0.98:
                self._edge(n, v)
            else:
                m = len(self.coords)
                self.coords.append(q)
                self.adj.append({})
                del self.adj[u][v], self.adj[v][u]
                self._edge(u, m)
                self._edge(m, v)
                self._edge(n, m)

    def _candidates(self, p: Point, radius_m: float) -> set[tuple[int, int]]:
        r = int(radius_m / 100_000 / CELL) + 1 if radius_m > 2000 else 1
        iy, ix = int(p[0] / CELL), int(p[1] / CELL)
        out: set[tuple[int, int]] = set()
        for dy in range(-r - 1, r + 2):
            for dx in range(-r - 1, r + 2):
                out |= self._grid.get((iy + dy, ix + dx), set())
        return out

    # ---- queries
    def snap(self, p: Point, max_m: float = 3000.0) -> Snap | None:
        best: Snap | None = None
        for u, v in self._candidates(p, max_m):
            if v not in self.adj[u]:
                continue
            t, q, d = _project(p, self.coords[u], self.coords[v])
            if d <= max_m and (best is None or d < best.dist_m):
                best = Snap(u, v, t, q, d)
        return best

    def route(self, a: Point, b: Point, blocked: set[frozenset[int]] | None = None, max_snap_m: float = 3000.0) -> Route:
        sa, sb = self.snap(a, max_snap_m), self.snap(b, max_snap_m)
        if sa is None or sb is None:
            return Route([a, b], ok=False, detail="station too far from any track")
        snap_m = max(sa.dist_m, sb.dist_m)
        blocked = blocked or set()

        def ends(s: Snap) -> dict[int, float]:
            return {s.u: dist_m(s.point, self.coords[s.u]), s.v: dist_m(s.point, self.coords[s.v])}

        src, dst = ends(sa), ends(sb)
        best_len, best_nodes = math.inf, None
        if {sa.u, sa.v} == {sb.u, sb.v} and frozenset((sa.u, sa.v)) not in blocked:  # same track segment
            best_len, best_nodes = dist_m(sa.point, sb.point), []
        dist: dict[int, float] = {n: d for n, d in src.items()}
        prev: dict[int, int] = {}
        heap = [(d, n) for n, d in src.items()]
        heapq.heapify(heap)
        done: set[int] = set()
        while heap:
            d, n = heapq.heappop(heap)
            if n in done:
                continue
            if d >= best_len:
                break
            done.add(n)
            if n in dst and d + dst[n] < best_len:
                best_len = d + dst[n]
                chain = [n]
                while chain[-1] in prev:
                    chain.append(prev[chain[-1]])
                best_nodes = chain[::-1]
            for m, w in self.adj[n].items():
                if m in done or frozenset((n, m)) in blocked:
                    continue
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd, m))
        if best_nodes is None:
            return Route([a, b], ok=False, snap_m=snap_m, detail="no path in the rail network")
        pts = [a, sa.point] + [self.coords[n] for n in best_nodes] + [sb.point, b]
        # drop a spur at the start/end (snap point lies between the two nodes but the path runs the other way)
        return Route(_dedupe(pts), nodes=best_nodes, length_m=best_len + sa.dist_m + sb.dist_m, snap_m=snap_m)

    def route_through(self, points: list[Point], blocked: set[frozenset[int]] | None = None,
                      max_snap_m: float = 3000.0) -> Route:
        """Route a-b-c-... leg by leg. A leg that cannot be routed is drawn straight and ``ok`` becomes False."""
        out: list[Point] = []
        nodes: list[int] = []
        total, snap, ok, detail = 0.0, 0.0, True, ""
        for a, b in zip(points, points[1:]):
            r = self.route(a, b, blocked, max_snap_m)
            ok = ok and r.ok
            detail = detail or r.detail
            total += r.length_m if r.ok else dist_m(a, b)
            snap = max(snap, r.snap_m)
            out += r.points if not out else r.points[1:]
            nodes += r.nodes
        return Route(_dedupe(out), nodes=nodes, length_m=total, ok=ok, snap_m=snap, detail=detail)

    @staticmethod
    def edges_of(nodes: list[int]) -> set[frozenset[int]]:
        return {frozenset((a, b)) for a, b in zip(nodes, nodes[1:])}

    def stats(self) -> dict:
        comp, seen = 0, set()
        sizes = []
        for s in range(len(self.coords)):
            if s in seen:
                continue
            stack, size = [s], 0
            seen.add(s)
            while stack:
                n = stack.pop()
                size += 1
                for m in self.adj[n]:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            sizes.append(size)
            comp += 1
        return {"nodes": len(self.coords), "components": comp, "largest": max(sizes) if sizes else 0}


def _dedupe(points: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if not out or dist_m(out[-1], p) > 0.5:
            out.append(p)
    return out


def order_stations(points: list[Point]) -> list[Point]:
    """Order stations along a corridor: start at the point farthest from the others, then nearest neighbour."""
    if len(points) <= 2:
        return list(points)
    start = max(range(len(points)), key=lambda i: sum(dist_m(points[i], q) for q in points))
    left = set(range(len(points))) - {start}
    order = [start]
    while left:
        nxt = min(left, key=lambda j: dist_m(points[order[-1]], points[j]))
        order.append(nxt)
        left.remove(nxt)
    return [points[i] for i in order]


@lru_cache(maxsize=1)
def get_network() -> RailNetwork | None:
    path = Path(PROJECT_ROOT / "data" / "rail_ch.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return RailNetwork([[tuple(p) for p in line] for line in data["lines"]], data.get("source", ""))


# ---- bus routes on roads and helpers to keep a detour visibly apart from the disrupted track
ROAD_CACHE = Path(PROJECT_ROOT / "data" / "road_cache.json")


def overlap_share(route: list[Point], track: list[Point], tol_m: float = 60.0) -> float:
    """Share (0..1) of the route's length that lies within tol_m of the track."""
    pts = list(route)
    if len(pts) < 2 or len(track) < 2:
        return 0.0
    near = total = 0.0
    for a, b in zip(pts, pts[1:]):
        w = dist_m(a, b)
        m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        total += w
        if any(_project(m, t0, t1)[2] <= tol_m for t0, t1 in zip(track, track[1:])):
            near += w
    return near / total if total else 0.0


def offset_line(points: list[Point], metres: float) -> list[Point]:
    """The polyline shifted sideways (to the left of its direction of travel) by ``metres``."""
    pts = _dedupe(list(points))
    if len(pts) < 2:
        return pts
    out: list[Point] = []
    for i, p in enumerate(pts):
        a, b = pts[max(i - 1, 0)], pts[min(i + 1, len(pts) - 1)]
        kx, ky = _k(p[0])
        dx, dy = (b[1] - a[1]) * kx, (b[0] - a[0]) * ky
        n = math.hypot(dx, dy) or 1.0
        out.append((p[0] + (dx / n) * metres / ky, p[1] - (dy / n) * metres / kx))
    return out


def road_route(a: Point, b: Point, timeout: float = 6.0) -> list[Point] | None:
    """Driving route a->b from an OSRM server (road network, for replacement buses). Cached in data/road_cache.json.
    Returns None when routing is switched off (ROAD_ROUTING=false), unreachable or the answer is implausible."""
    import os
    key = f"{a[0]:.4f},{a[1]:.4f};{b[0]:.4f},{b[1]:.4f}"
    cache: dict = {}
    try:
        cache = json.loads(ROAD_CACHE.read_text()) if ROAD_CACHE.exists() else {}
    except Exception:  # noqa: BLE001
        cache = {}
    if key in cache:
        return [tuple(p) for p in cache[key]] if cache[key] else None
    if os.environ.get("ROAD_ROUTING", "true").lower() in ("0", "false", "no"):
        return None
    base = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
    try:
        import httpx
        r = httpx.get(f"{base}/route/v1/driving/{a[1]:.5f},{a[0]:.5f};{b[1]:.5f},{b[0]:.5f}",
                      params={"overview": "full", "geometries": "geojson"}, timeout=timeout)
        r.raise_for_status()
        line = [(c[1], c[0]) for c in r.json()["routes"][0]["geometry"]["coordinates"]]
    except Exception:  # noqa: BLE001 - offline or blocked: the caller draws an offset line instead; not cached
        return None
    ok = len(line) >= 2 and path_length_m(line) <= max(2.5 * dist_m(a, b), 3000)
    cache[key] = [list(p) for p in line] if ok else []
    try:
        ROAD_CACHE.write_text(json.dumps(cache))
    except Exception:  # noqa: BLE001
        pass
    return line if ok else None
