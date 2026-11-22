from datetime import datetime, timezone

from app.core.geo import canton_for, get_station_index
from app.core.i18n import forecast_label, is_uncertain


def test_station_lookup_names_aliases_and_uic():
    idx = get_station_index()
    assert idx.lookup("Genève").code == "8501008"
    assert idx.lookup("Bern").name == "Bern Hbf"
    assert idx.lookup("Fribourg/Freiburg").name == "Fribourg"
    assert idx.lookup("Gümligen, Bahnhof (Bus)").name == "Gümligen"
    assert idx.lookup("Effrektion").name == "Effretikon"  # typo in KULT export
    assert idx.by_uic(8507002).name == "Ostermundigen"
    assert idx.lookup("Nowhere-on-Sea") is None  # safe fallback, no crash


def test_canton_estimation():
    assert canton_for(46.2089, 6.1453) == "GE"
    assert canton_for(46.9491, 7.4385) == "BE"
    assert canton_for(46.1995, 6.2365, "FR") == "FR"


def test_forecast_labels():
    to = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    assert forecast_label("UNTIL_APPROX_DATE_TIME", None, to, None)["de"] == "Bis ca. 08:00 (Prognose kann ändern)"
    assert "14:30" in forecast_label("UNTIL_AT_LEAST", None, None, "14:30:00")["en"]
    assert forecast_label("UNTIL", None, None, None)["en"].startswith("End not yet known")  # missing time -> UNKNOWN
    assert is_uncertain("UNKNOWN") and is_uncertain("UNTIL_AT_LEAST") and not is_uncertain("UNTIL")


def test_dataset_forecast_labels_carry_their_date_and_live_ones_do_not():
    from datetime import datetime
    from app.core.i18n import TZ, forecast_label
    to = datetime(2026, 9, 1, 9, 0, tzinfo=TZ)
    assert "09:00" in forecast_label("UNTIL_APPROX_DATE_TIME", None, to, None)["de"]
    assert "01.09. 09:00" not in forecast_label("UNTIL_APPROX_DATE_TIME", None, to, None)["de"]
    assert "01.09. 09:00" in forecast_label("UNTIL_APPROX_DATE_TIME", None, to, None, with_date=True)["de"]
    assert "01.09. 18:30" in forecast_label("UNTIL_AT_LEAST", None, None, "18:30", with_date=True, ref=to)["en"]


def test_timetable_link_matches_the_sbb_format():
    from app.core.i18n import TZ, sbb_link, timetable_moment
    now = datetime(2026, 11, 22, 6, 41, tzinfo=TZ)
    when = datetime(2026, 9, 1, 9, 0, tzinfo=TZ)
    url = sbb_link("timetable", "en", "Bern Wankdorf", "Gümligen", when=when, origin_id="8516161", destination_id="8507003")["url"]
    # sbb.ch plans today or later: the past dataset day keeps its time of day on today's date, `day` keeps the incident's own date
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    assert url == (f"https://www.sbb.ch/en?stops=Bern+Wankdorf_I8516161~G%C3%BCmligen_I8507003"
                   f"&date=%22{today}%22&time=09_00&moment=dep&day=2026-09-01")
    assert timetable_moment(when, now) == ("2026-11-22", "09:00", "2026-09-01")
    assert timetable_moment(datetime(2026, 12, 3, 8, 5, tzinfo=TZ), now) == ("2026-12-03", "08:05", "2026-12-03")
    # unknown id: name only; no moment: no date/time at all
    bare = sbb_link("timetable", "de", "Genève", "Thun")["url"]
    assert bare.endswith("stops=Gen%C3%A8ve~Thun") and "date" not in bare


def test_timetable_link_is_percent_encoded_exactly_once():
    from urllib.parse import unquote
    from app.core.i18n import TZ, sbb_link
    url = sbb_link("timetable", "fr", "Genève", "Bern Wankdorf", when=datetime(2026, 12, 3, 8, 5, tzinfo=TZ), origin_id="8501008")["url"]
    assert "%25" not in url and " " not in url and '"' not in url
    assert unquote(url.split("stops=")[1].split("&")[0]).replace("+", " ") == "Genève_I8501008~Bern Wankdorf"


def test_old_format_links_are_upgraded_to_the_current_format():
    from urllib.parse import quote
    from app.core.legacy_links import upgrade_timetable_url
    today = datetime.now(__import__("app.core.i18n", fromlist=["TZ"]).TZ).strftime("%Y-%m-%d")
    new = f"https://www.sbb.ch/en?stops=Bern+Wankdorf_I8516161~G%C3%BCmligen_I8507003&date=%22{today}%22&time=06_40&moment=dep&day=2026-09-01"
    stops = '[{"value":"8516161","type":"ID","label":"Bern Wankdorf"},{"value":"8507003","type":"ID","label":"Gümligen"}]'
    old_json = "https://www.sbb.ch/en?stops=" + quote(stops) + "&date=%222026-09-01%22&time=%2206%3A40%22&moment=%22DEPARTURE%22"
    old_twice = old_json.replace("%", "%25")
    assert upgrade_timetable_url(old_json) == new
    assert upgrade_timetable_url(old_twice) == new
    legacy = upgrade_timetable_url("https://www.sbb.ch/en?von=Bern+Wankdorf&nach=G%C3%BCmligen&datum=01.09.2026&zeit=06%3A40")
    assert legacy == new.replace("Bern+Wankdorf_I8516161~G%C3%BCmligen_I8507003", "Bern+Wankdorf~G%C3%BCmligen")
    assert upgrade_timetable_url(new) == new                                   # current format is left alone
    assert upgrade_timetable_url("https://www.sbb.ch/en") == "https://www.sbb.ch/en"


def test_no_old_format_timetable_builder_remains():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    banned = ['"type":"ID"', "'type': 'ID'", "DEPARTURE", "zeit=", "'zeit'", "'datum'", "von=", "'von'", "searchParams.set"]
    hits = []
    for base in ("app", "static"):
        for f in (root / base).rglob("*"):
            if f.suffix in {".py", ".js", ".html", ".json"} and "__pycache__" not in f.parts and f.name != "legacy_links.py" and "rail_ch" not in f.name:
                text = f.read_text(encoding="utf-8", errors="ignore")
                hits += [f"{f.relative_to(root)}: {b}" for b in banned if b in text]
    assert not hits, hits
