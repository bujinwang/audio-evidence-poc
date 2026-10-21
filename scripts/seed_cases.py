#!/usr/bin/env python3
"""Create the demo case files and attach the existing recordings.

The application shipped with recordings but no case, which is why the UI could
not answer "what is due". This script creates the two cases the demo needs,
assigns exhibits by filename, and scans the redaction cues.

The grouping is demo scaffolding, not a claim about a real investigation: the
recordings are public audio, and the case numbers, subjects and deadlines are
invented to exercise the workflow. Re-running is safe — cases are matched by
number, assignments are idempotent, and cue decisions already made are preserved.

Usage:
    python scripts/seed_cases.py            # create/update cases and cues
    python scripts/seed_cases.py --rescan   # also re-run cue detection
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app import cues  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Case, Recording  # noqa: E402

CASES = [
    {
        "number": "26-104772",
        "subject": "Reopened 1998 homicide — audio review",
        "description": (
            "Re-interview audio and the original microcassette statement for the 1998 file, "
            "plus the press conference announcing the bodycam redaction programme. "
            "Three exhibits, all indexed."
        ),
        "days": 6,
        "owner": "R. Cardinal",
        "exhibits": [
            "2022-09-07_911_call_phoenix.wav",
            "eps_presser.flac",
            "AMI_EN2002c_sdm.wav",
        ],
    },
    {
        "number": "26-098431",
        "subject": "Public-order disclosure — event audio",
        "description": (
            "Broadcast and event audio disclosed to counsel: a championship broadcast, "
            "a commencement address, and a NASA news conference. Kept separate from the "
            "homicide file so disclosure clocks do not mix."
        ),
        "days": 2,
        "owner": "M. Okafor",
        "exhibits": [
            "2025-07-08_speech_commencement-northwestern.wav",
            "2025-11-15_sport_football-championship.wav",
            "2026-05-26_speech_nasa-moonbase.wav",
        ],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rescan", action="store_true", help="re-run cue detection")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        for spec in CASES:
            case = session.execute(
                select(Case).where(Case.number == spec["number"])
            ).scalar_one_or_none()
            if case is None:
                case = Case(number=spec["number"], subject=spec["subject"])
                session.add(case)
                print(f"created case {case.number}")
            case.subject = spec["subject"]
            case.description = spec["description"]
            case.owner = spec["owner"]
            case.disclosure_due = now + timedelta(days=spec["days"])
            session.flush()

            for filename in spec["exhibits"]:
                recording = session.execute(
                    select(Recording).where(Recording.filename == filename)
                ).scalar_one_or_none()
                if recording is None:
                    print(f"  missing recording: {filename}")
                    continue
                if recording.case_id != case.id:
                    recording.case_id = case.id
                    print(f"  {filename} → {case.number}")

        session.commit()

        recordings = list(session.execute(select(Recording)).scalars())
        total = 0
        for recording in recordings:
            if recording.case_id is None:
                continue
            if args.rescan:
                total += cues.scan_recording(session, recording)
        session.commit()

        if args.rescan:
            print(f"cue scan created {total} candidates")

        for spec in CASES:
            case = session.execute(
                select(Case).where(Case.number == spec["number"])
            ).scalar_one()
            count = len(
                list(
                    session.execute(
                        select(Recording).where(Recording.case_id == case.id)
                    ).scalars()
                )
            )
            print(f"{case.number}  {count} exhibits  due {case.disclosure_due:%Y-%m-%d}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
