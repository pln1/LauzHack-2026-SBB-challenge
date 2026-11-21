"""Robust readers for the SBB CSV / XLSX exports (encoding detection, empty fields)."""
from __future__ import annotations

import logging
from pathlib import Path

import chardet
import pandas as pd

from app.core.i18n import TZ

log = logging.getLogger("etl.loaders")


def detect_encoding(path: Path, sample_bytes: int = 256_000) -> str:
    raw = path.read_bytes()[:sample_bytes]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    guess = chardet.detect(raw).get("encoding") or "cp1252"
    # chardet tends to say ISO-8859-1 for Windows exports with umlauts; cp1252 is a superset
    return "cp1252" if guess.lower() in ("iso-8859-1", "latin-1", "windows-1252", "ascii") else guess


def read_csv(path: Path) -> pd.DataFrame:
    enc = detect_encoding(path)
    df = pd.read_csv(path, encoding=enc, dtype=str, keep_default_na=True, na_values=["", "NULL", "null"])
    df.columns = [c.strip().lstrip("﻿") for c in df.columns]
    df = df.astype(object).where(pd.notna(df), None)
    log.info("read_csv", extra={"file": path.name, "rows": len(df), "encoding": enc})
    return df


def find_one(folder: Path, pattern: str) -> Path | None:
    hits = sorted(folder.glob(pattern))
    return hits[-1] if hits else None


def parse_ts(value, naive_tz=TZ) -> pd.Timestamp | None:
    """Parse '2024-03-04 09:00:00.000 +0100' (aware) or '2026-09-01 04:09:10.625' (naive, Swiss local)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        try:
            ts = pd.to_datetime(value, format="mixed")
        except (ValueError, TypeError):
            return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(naive_tz, ambiguous="NaT", nonexistent="shift_forward")
        if pd.isna(ts):
            return None
    return ts.tz_convert("UTC")


def to_py(ts: pd.Timestamp | None):
    return ts.to_pydatetime() if ts is not None else None


def to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
