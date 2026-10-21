#!/usr/bin/env python3
"""Remove recordings from the library — rows, derived index rows, and files.

The library ships with no delete endpoint on purpose: in a real deployment an
evidence item is retained, not destroyed. This is a maintenance script for the
demo dataset, so it is deliberately explicit and requires the recording ids.

Every dependent row is removed in FK order, the SQLite FTS index is cleaned
because the application maintains it (there are no triggers), and the on-disk
originals/derivatives for the recording id are deleted last. The audit trail for
the *remaining* recordings is untouched.

Usage:
    python scripts/prune_recordings.py --list
    python scripts/prune_recordings.py --dry-run rec_abc rec_def
    python scripts/prune_recordings.py rec_abc rec_def
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Recording  # noqa: E402


def _rows(session, table: str, ids: list[str], column: str) -> int:
    if not ids:
        return 0
    marks = ",".join(f":i{n}" for n in range(len(ids)))
    params = {f"i{n}": value for n, value in enumerate(ids)}
    result = session.execute(
        text(f"DELETE FROM {table} WHERE {column} IN ({marks})"), params
    )
    return result.rowcount or 0


def prune(session, recording: Recording, dry_run: bool) -> dict[str, int]:
    settings = get_settings()
    rid = recording.id

    segment_ids = [
        row[0]
        for row in session.execute(
            text("SELECT id FROM segments WHERE recording_id = :rid"), {"rid": rid}
        )
    ]
    counts = {
        "revisions": _rows(session, "revisions", segment_ids, "segment_id") if not dry_run else 0,
        "embeddings": _rows(session, "segment_embeddings", segment_ids, "segment_id")
        if not dry_run
        else 0,
        "fts": _rows(session, "segments_fts", segment_ids, "segment_id") if not dry_run else 0,
        "segments": len(segment_ids),
    }
    counts["audit"] = (
        _rows(session, "audit_events", [rid], "recording_id") if not dry_run else 0
    )
    counts["transcripts"] = (
        _rows(session, "transcripts", [rid], "recording_id") if not dry_run else 0
    )
    counts["jobs"] = _rows(session, "jobs", [rid], "recording_id") if not dry_run else 0

    if not dry_run:
        session.delete(recording)
        session.flush()

        original_dir = settings.originals_dir / rid
        if original_dir.exists():
            shutil.rmtree(original_dir)
        for path in (
            settings.working_dir / f"{rid}.flac",
            settings.denoised_dir / f"{rid}.denoised.flac",
        ):
            path.unlink(missing_ok=True)

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="recording ids to remove")
    parser.add_argument("--list", action="store_true", help="list recordings and exit")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    try:
        if args.list or not args.ids:
            for row in session.query(Recording).order_by(Recording.created_at):
                print(
                    f"{row.id}  {row.duration_s or 0:8.1f}s  "
                    f"{row.source_type:10s}  {row.filename}"
                )
            return 0

        for rid in args.ids:
            recording = session.get(Recording, rid)
            if recording is None:
                print(f"skip {rid}: not found")
                continue
            counts = prune(session, recording, args.dry_run)
            detail = " ".join(f"{k}={v}" for k, v in counts.items())
            print(f"{'would remove' if args.dry_run else 'removed'} {rid} {recording.filename} [{detail}]")

        if args.dry_run:
            session.rollback()
        else:
            session.commit()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
