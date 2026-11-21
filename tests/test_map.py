"""Map GeoJSON: pure helpers (no DB) and the API endpoints (need DATABASE_URL with ingested data)."""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

from app.schemas.dtos import MapGeometry
from app.services import map_service as ms


def _inc(waypoints, detour=None, status="ACTIVE"):
    return SimpleNamespace(id=7, title="A – B", status=status, status_label="Interrupted", affected_lines=["IC5"], current_version=2,
                           has_replacement_bus=True, canton="VD", forecast_label="until 18:00", updated_ago="1 min ago",
                           detour_summary="Take the bus", map_geometry=MapGeometry.model_construct(waypoints=waypoints, detour_route=detour, stations=[], approximate=False))


def test_geojson_uses_lon_lat_order_and_status_colour():
    feats = ms.incident_features(_inc([[46.6, 6.5], [46.7, 6.6]], detour=[[46.2, 6.1], [47.1, 7.2]]), with_detour=True)
    kinds = [f["properties"]["kind"] for f in feats]
    assert kinds == ["segment", "incident", "detour"]
    seg = feats[0]
    assert seg["geometry"]["coordinates"][0] == [6.5, 46.6]          # [lon, lat]
    assert seg["properties"]["color"] == "#EB0000" and seg["properties"]["dashed"] is False
    assert seg["properties"]["bounds"][0][0] < 46.6 < seg["properties"]["bounds"][1][0]


def test_restricted_is_dashed_amber_and_detours_are_optional():
    feats = ms.incident_features(_inc([[46.6, 6.5], [46.7, 6.6]], detour=[[46.2, 6.1], [47.1, 7.2]], status="RESTRICTED"), with_detour=False)
    assert feats[0]["properties"]["dashed"] is True and feats[0]["properties"]["color"] == "#F59E0B"
    assert all(f["properties"]["kind"] != "detour" for f in feats)


def test_incident_without_coordinates_yields_no_features_and_bad_points_are_dropped():
    assert ms.incident_features(_inc([]), True) == []
    assert ms.incident_features(_inc([[None, 1], ["x", 2], [999, 999]]), True) == []
    one = ms.incident_features(_inc([[46.6, 6.5]]), True)            # a single point: marker only, no line
    assert [f["properties"]["kind"] for f in one] == ["incident"]
    empty = ms.collection([], unmappedCount=1)
    assert empty["bbox"] == [5.9, 45.8, 10.55, 47.85] and empty["features"] == []   # falls back to Switzerland


needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL with ingested data")


async def _api():
    from app.db.session import dispose_engine
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/map", params={"mode": "live", "lang": "de"})
        assert r.status_code == 200
        fc = r.json()
        assert fc["type"] == "FeatureCollection" and fc["features"]
        markers = [f for f in fc["features"] if f["properties"]["kind"] == "incident"]
        assert markers and {"id", "title", "status", "color", "affectedLines", "version", "bounds"} <= set(markers[0]["properties"])
        lon, lat = markers[0]["geometry"]["coordinates"]
        assert 5 < lon < 11 and 45 < lat < 48                        # GeoJSON order: lon first
        assert fc["meta"]["mappedCount"] + fc["meta"]["unmappedCount"] == fc["meta"]["incidentCount"]

        one = (await c.get(f"/api/incidents/{markers[0]['properties']['id']}/map", params={"lang": "en"})).json()
        kinds = {f["properties"]["kind"] for f in one["features"]}
        assert {"segment", "incident", "station"} <= kinds and one["meta"]["hasCoordinates"] is True

        ds = (await c.get("/api/map", params={"mode": "dataset"})).json()                # contains incidents without coordinates
        assert ds["meta"]["unmappedCount"] == len(ds["meta"]["unmappedIds"])
        if ds["meta"]["unmappedIds"]:
            r = await c.get(f"/api/incidents/{ds['meta']['unmappedIds'][0]}/map")
            assert r.status_code == 200 and r.json()["meta"]["hasCoordinates"] is False and r.json()["features"] == []
        assert (await c.get("/api/incidents/1/map")).status_code == 404
    await dispose_engine()


@needs_db
def test_map_endpoints():
    asyncio.run(_api())
