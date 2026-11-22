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


def test_timetable_link_carries_the_incident_time():
    from datetime import datetime
    from app.core.i18n import TZ, sbb_link
    when = datetime(2026, 9, 1, 6, 40, tzinfo=TZ)
    url = sbb_link("timetable", "de", "Bern", "Thun", when=when)["url"]
    assert "datum=01.09.2026" in url and "zeit=06%3A40" in url and "von=Bern" in url
    assert "datum" not in sbb_link("timetable", "de", "Bern", "Thun")["url"]
