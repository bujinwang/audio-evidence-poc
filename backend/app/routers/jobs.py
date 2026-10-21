"""Job inspection and recovery.

A job row is audit surface: it records that processing was requested, what the
worker did, and how it ended. It is normally written by the pipeline alone, with
two deliberate exceptions reachable from here:

* `POST /api/jobs/{id}/reset` retires a job that is stuck in `queued`/`running`
  because the process running it died. Without it, a stranded job blocks every
  later `POST /retry` for that exhibit with 409 and the exhibit can never be
  reprocessed again — a custody problem, not an inconvenience. Note
  "reprocessable", not "repaired": the retry it unblocks re-transcribes from
  scratch, so recovery means the exhibit is workable again, not that the earlier
  transcript is preserved. `reset_job`'s docstring spells out the consequence.

Neither endpoint deletes a row, and both append an `AuditEvent` naming the actor
so that abandoning a run in flight is always attributable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..errors import ActiveJobConflict
from ..models import Job, Recording
from ..pipeline import jobs as job_pipeline
from ..schemas import JobOut
from ..security import Actor, require
from ..serializers import job_out

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> JobOut:
    return job_out(_get_or_404(session, job_id))


@router.post("/{job_id}/reset", response_model=JobOut)
def reset_job(
    job_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("retry")),
) -> JobOut:
    """Mark a stranded queued/running job `failed` so the exhibit can be retried.

    This is an explicit operator action rather than an automatic staleness rule.
    `Job` has `started_at` but neither a heartbeat nor a per-stage timestamp, and
    the long stages (`transcribe()`) block for tens of minutes without touching
    the database — so the age of `started_at` cannot tell "the process died"
    from "a two-hour cassette is being transcribed". An automatic threshold
    would have to guess, and guessing wrong throws away a live run's
    derivatives: strictly worse than the bug being fixed. A human deciding, with
    the stage and progress in front of them, is the accurate signal; the audit
    event is what makes that decision reviewable later.

    Permission is `retry` — the same right that would have started the run.

    **What this does not do: it does not cancel anything and it does not start
    anything.** Measured, not assumed:

    * No job is created and no work is queued. A reset adds zero `jobs` rows,
      leaves `denoised_path` and `processing_meta` untouched, and emits only
      `job.reset`. Resetting a denoise claim does **not** re-trigger a denoise;
      the exhibit simply becomes available again. (That was worth measuring: it
      is the natural thing to fear from reusing one slot for two kinds of work.)
      The active-job row is a **mutual-exclusion token, not a scheduler entry** —
      releasing a lock is not the same as enqueuing work — so it is wrong to
      infer "the claim was released, therefore the work restarts".
    * A process that is genuinely alive keeps running. There is no cancellation
      mechanism, so after a reset it can still finish and write its own audit
      events; the retirement marks that an operator gave up on the run, not that
      the run stopped. The `error` text therefore says "marked stale by an
      operator", and the audited `stage`/`progress` record what it looked like
      when the decision was taken.
    * If the recording was stuck at `processing`, it is moved to `failed`. Its
      existing transcripts and segments are left in place **by the reset**, so
      the data is still there when the reset returns. Be precise about what
      follows, though: the retry that a reset unblocks runs `_process()`, which
      calls `_clear_previous_attempt()` before the first stage. So a retry is a
      full re-transcribe, not an index rebuild over existing segments, and if it
      fails or ASR is unavailable the exhibit ends with **no** transcripts.
      Recovery is therefore "the exhibit is retryable again", not "the
      transcripts are safe". Reset while the recording is `processing` only when
      you are prepared to redo the transcription.
    """
    job = _get_or_404(session, job_id)
    try:
        job_pipeline.reset_stuck_job(session, job, actor)
    except ActiveJobConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.message
        ) from exc

    session.expire_all()
    return job_out(session.get(Job, job_id))


@router.post("/reset-stranded/{recording_id}", response_model=list[JobOut])
def reset_stranded_for_recording(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("retry")),
) -> list[JobOut]:
    """Retire every stranded active job on one exhibit.

    The operator-facing shape of the same action: someone looking at an exhibit
    that refuses to re-index knows the exhibit, not which of four job rows is
    the one holding the lock. Retiring all of them is safe because at most one
    can be genuinely live (the partial unique index guarantees a single active
    job), and the audit event records every row that was retired.
    """
    if session.get(Recording, recording_id) is None:
        raise HTTPException(status_code=404, detail="recording not found")

    stranded = list(
        session.execute(
            select(Job)
            .where(Job.recording_id == recording_id)
            .where(Job.status.in_(["queued", "running"]))
            .order_by(Job.created_at)
        ).scalars()
    )
    if not stranded:
        return []

    reset: list[JobOut] = []
    for job in stranded:
        try:
            job_pipeline.reset_stuck_job(session, job, actor)
        except ActiveJobConflict:
            # Raced with the worker finishing it normally: the exhibit is no
            # longer stranded, so there is nothing to report.
            continue
        session.expire_all()
        refreshed = session.get(Job, job.id)
        if refreshed is not None:
            reset.append(job_out(refreshed))
    return reset


def _get_or_404(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
