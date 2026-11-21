#!/usr/bin/env python
"""Idempotent ETL CLI: load the three SBB datasets from Daten/ into PostgreSQL.

    python scripts/ingest_data.py                 # upsert (safe to re-run)
    python scripts/ingest_data.py --reset         # truncate incident + chat tables first
    python scripts/ingest_data.py --data-dir /path/to/Daten
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.geo import get_station_index  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_schema  # noqa: E402
from app.etl.pipeline import run_ingest  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, help="Path to the Daten/ folder (default: $DATA_DIR or ./Daten)")
    parser.add_argument("--reset", action="store_true", help="Truncate incident and chat tables before loading")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    if args.data_dir:
        settings.data_dir = args.data_dir
    await init_schema()
    try:
        async with get_sessionmaker()() as session:
            stats = await run_ingest(session, settings, get_station_index(), reset=args.reset)
    finally:
        await dispose_engine()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
