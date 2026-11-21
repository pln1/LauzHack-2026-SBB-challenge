"""Rail routing: snapping, routing along track, blocked-edge detours, fallbacks."""
from app.core.rail import RailNetwork, dist_m, order_stations, path_length_m, simplify

# A small ladder: A-B-C on the main line, and a longer parallel line A-D-E-C around B.
A, B, C = (46.00, 7.00), (46.00, 7.10), (46.00, 7.20)
D, E = (46.05, 7.05), (46.05, 7.15)
NET = RailNetwork([[A, B, C], [A, D, E, C]], "test")


def test_route_follows_track_not_straight_line():
    r = NET.route(A, C)
    assert r.ok and abs(r.length_m - dist_m(A, C)) < 300
    assert any(dist_m(p, B) < 5 for p in r.points)  # passes through the middle station B


def test_blocked_edges_force_a_real_detour():
    main = NET.route(A, C)
    detour = NET.route(A, C, blocked=NET.edges_of(main.nodes))
    assert detour.ok and detour.length_m > main.length_m * 1.1
    assert any(p[0] > 46.04 for p in detour.points)  # goes round via the northern line


def test_unreachable_station_is_not_snapped():
    r = NET.route((47.5, 8.5), C)
    assert not r.ok and len(r.points) == 2  # straight-line fallback, flagged


def test_t_junction_is_connected():
    net = RailNetwork([[A, C], [(46.02, 7.10), (46.0, 7.1005)]], "t")
    r = net.route(A, (46.02, 7.10))
    assert r.ok


def test_simplify_and_order():
    pts = [(46.0, 7.0), (46.0001, 7.05), (46.0, 7.1)]
    assert len(simplify(pts, 50)) == 2
    assert order_stations([B, A, C]) in ([A, B, C], [C, B, A])
    assert path_length_m([A, B, C]) > 15000


def test_local_detours_are_capped_and_start_at_disruption_ends():
    from app.etl.pipeline import _local_detours
    main = NET.route(A, C)
    specs = [{"label": "x", "kind": "via", "names": ["A", "D", "C"], "points": [A, D, C]},
             {"label": "far", "kind": "via", "names": ["A", "Far", "C"], "points": [A, (47.9, 9.9), C]},
             {"label": "bus", "kind": "bus", "names": ["A", "C"], "points": [A, C]}]
    out = _local_detours(NET, [A, C], NET.edges_of(main.nodes), specs)
    assert [d["kind"] for d in out] == ["via", "bus"]      # far-away via station dropped, max 2
    assert dist_m(tuple(out[0]["route"][0]), A) < 50 and dist_m(tuple(out[0]["route"][-1]), C) < 50


def test_bus_line_never_hides_under_the_track(monkeypatch):
    from app.core import rail
    from app.etl.pipeline import _local_detours
    monkeypatch.setenv("ROAD_ROUTING", "false")
    track = NET.route(A, C).points
    out = _local_detours(NET, [A, C], set(), [{"label": "b", "kind": "bus", "names": ["A", "C"], "points": [A, C]}], track)
    assert out[0]["kind"] == "bus" and out[0]["source"] == "offset" and len(out[0]["stops"]) == 2
    assert rail.overlap_share([tuple(p) for p in out[0]["route"]], track) < 0.2
