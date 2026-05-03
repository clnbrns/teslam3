"""One-shot: load monitoring_log.json + .roi_state.json into SQLite.

Run after upgrading from the JSONL-backed version. Idempotent — safe to
re-run; existing (vin, ts, type) rows are skipped.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from tesla_fleet import db

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", type=Path, default=Path("monitoring_log.json"))
    parser.add_argument("--roi-state", type=Path, default=Path(".roi_state.json"))
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    db.init(args.db)
    inserted = skipped = 0
    with db.connect(args.db) as conn:
        if args.log_file.exists():
            conn.execute("BEGIN")
            for line in args.log_file.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "charging_session":
                    if db.record_charging(conn, ev):
                        inserted += 1
                    else:
                        skipped += 1
                elif db.record_event(conn, ev):
                    inserted += 1
                else:
                    skipped += 1
            conn.execute("COMMIT")

        if args.roi_state.exists():
            roi = json.loads(args.roi_state.read_text())
            db.set_roi_totals(conn, vin="", total_miles=roi.get("total_miles", 0),
                              total_kwh=roi.get("total_kwh", 0))
            logger.info("ROI totals: %s mi · %s kWh",
                        roi.get("total_miles"), roi.get("total_kwh"))

    print(f"Inserted {inserted:,} rows · skipped {skipped:,} duplicates")
    print(f"DB → {args.db}")


if __name__ == "__main__":
    main()
