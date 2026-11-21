from app.services.text_utils import extract_lines, mentions_bus, sanitize_operational, scrub_pii, split_line_list


def test_pii_is_scrubbed():
    raw = ("Bitte <b>Hans Muster</b> anrufen: +41 79 123 45 67 oder 079 123 45 67, mail hans.muster@sbb.ch, "
           "erfasst von u238251, Link https://miku2.sbb.ch/#/fahrt/123")
    out = sanitize_operational(raw)
    for leaked in ("+41 79", "079 123", "hans.muster@sbb.ch", "u238251", "miku2"):
        assert leaked not in out
    assert "<b>" not in out


def test_public_links_and_train_numbers_survive():
    out = scrub_pii("Info: https://www.sbb.ch/de/fahrplan.html IC 2528 rebrousse à Romont")
    assert "https://www.sbb.ch/de/fahrplan.html" in out
    assert "2528" in out


def test_bus_detection_multilingual():
    assert mentions_bus("Ersatzbusse verkehren zwischen A und B")
    assert mentions_bus("Des bus de remplacement circulent")
    assert mentions_bus("Passengers use the replacement bus (EV).")
    assert mentions_bus("bus sostitutivi in servizio")
    assert not mentions_bus("Trains are delayed")


def test_line_extraction():
    assert extract_lines("Lines RE33, RL1, S9 and IC 6 are affected; EC too") == ["RE33", "RL1", "S9", "IC6", "EC"]
    assert split_line_list("ICE, IC6, IC8, S 1") == ["ICE", "IC6", "IC8", "S1"]
