"""Concurrency at the seams around `/retry`: on-demand denoise, and `submit()`.

The partial unique index `jobs(recording_id) WHERE status IN ('queued','running')`
is a *database* property, so it protects any caller that takes that slot. Two
seams sit outside the retry route and needed separate decisions.

**`POST /denoise` — hardened.** It enqueued no ingest job, so the index could not
reach it until it was given a slot to claim. Two harms came from that gap, and
they are worth separating because only the second is a custody defect:

1. *The artefact.* `storage.denoised_path()` is one fixed file per recording and
   the denoiser's level-matching step stages to a second fixed name, so
   concurrent runs overwrote each other mid-write; ffmpeg then read a truncated
   input and failed with an EMPTY stderr, surfacing as
   `422 "level matching failed: "` — an error blaming audio levels for a race.
   Fixed by publishing with `os.replace()` from a private scratch file
   (`_denoise_atomically`), so the canonical path is never observable partial.
2. *The custody record.* With the write made atomic the file stopped being
   corrupt, but all four callers still completed and each appended
   `recording.denoised` to the audit trail for a single artefact. `/provenance`
   renders repeats as a count, so the chain of custody said "denoised ×4" for a
   derivative produced once — a reviewer reconstructing how many times it was
   rebuilt, and by whom, got a false answer. Fixed by claiming the recording's
   active slot before any work (`_claim_denoise_slot`), so one caller wins and
   the rest get 409.

**`jobs.submit()` on the upload path — documented, not hardened.** Every path
that creates a recording mints a fresh `recording_id` server-side, so the insert
provably cannot collide; a 409 handler there would be dead code implying a
reachability that does not exist. The function still raises `ActiveJobConflict`
rather than a bare `IntegrityError`, and that contract is pinned here at the
function seam because the HTTP seam cannot reach it.

Failing on the unfixed code:

  * `test_concurrent_denoise_never_publishes_a_partial_derivative` — the
    canonical path is observed half-written (verified by mutation: reverting
    `_denoise_atomically` to write in place reddens it).
  * `test_concurrent_denoise_produces_one_audit_event` — 4 events, not 1.
"""

from __future__ import annotations

import io
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from conftest import ADMIN_HEADERS, upload_ok

CONCURRENT_DENOISE = 4


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _decodable(path: Path) -> tuple[bool, int]:
    """Is this a complete, decodable WAV? Returns (ok, frames).

    Two traps, both of which make a naive version of this helper vacuous:

    1. **`wave` cannot parse FLAC.** Production denoise publishes `.flac`, so a
       helper that blindly `wave.open`s the artefact fails for the wrong reason
       and looks like a code bug. This helper is only used on the stub's output
       below, which is deliberately a real WAV.
    2. **`getnframes()` reports the length *declared in the header*, not the
       bytes actually present.** Measured: on a WAV truncated to a quarter of
       its samples, `wave.open` succeeds and `getnframes()` still returns 16000,
       while `readframes()` yields only 4000 frames' worth of bytes. So
       `return True, handle.getnframes()` returns `(True, 16000)` for a
       half-written file — passing on exactly the truncation this fixture exists
       to catch.

    The read is therefore compared against the declared length: short read means
    the payload was truncated, which is the corruption the concurrent-write
    defect produced. The declared length is `frames * width * channels` —
    omitting `channels` (a bug this helper shipped with) reports every valid
    stereo WAV as corrupt, since `readframes()` returns interleaved samples.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            frame_bytes = handle.getsampwidth() * handle.getnchannels()
            payload = handle.readframes(frames)
    except Exception:
        return False, 0
    # A truncated file yields fewer bytes than the header promises.
    if len(payload) != frames * frame_bytes:
        return False, 0
    return True, frames


def _audit_count(recording_id: str) -> int:
    """How many `recording.denoised` events exist, read straight from the DB.

    Read with SQL rather than through `/api/audit` so the assertion does not
    depend on a second endpoint's projection of the same rows.
    """
    from app import db

    with db.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_events WHERE recording_id = :rid"
                " AND action = 'recording.denoised'"
            ),
            {"rid": recording_id},
        ).one()
    return int(row[0])


def _active_jobs(recording_id: str) -> list[tuple[str, str, str]]:
    from app import db

    with db.engine.connect() as conn:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                text(
                    "SELECT id, status, kind FROM jobs WHERE recording_id = :rid"
                    " AND status IN ('queued', 'running')"
                ),
                {"rid": recording_id},
            ).all()
        ]


def _derivative_path(recording_id: str) -> Path:
    from app.config import get_settings

    return get_settings().denoised_dir / f"{recording_id}.denoised.flac"


class _SlowStubDenoiser:
    """A denoiser that writes a real WAV slowly, so a write window really exists.

    The production denoiser shells out to ffmpeg and takes seconds, which is
    precisely why the window is reachable. A stub that returns instantly would
    hide it.

    The container matters. An earlier version of this stub wrote
    `readframes()` output straight to the artefact path; `readframes()` returns
    **headerless PCM**, so the file began `b'\\x00\\x00\\xbf\\x01...'` — neither
    `RIFF` nor `fLaC` — and `_decodable` rejected it while the pipeline was
    perfectly correct. Those two red tests were this fixture's fault, not the
    code's. Writing a real container makes the bytes a genuine WAV, so the
    assertion tests the publish logic rather than the stub's file format.
    """

    name = "stub-slow"

    def __init__(self, source_frames: bytes, sample_rate: int):
        self._payload = source_frames
        self._sample_rate = sample_rate
        self.calls = 0
        self._lock = threading.Lock()
        # Set by `arm()` in the tests. Holding every caller at the same point
        # before it opens the destination is what makes the overlap deterministic
        # instead of hoping the thread pool schedules threads close together — a
        # race test that only *sometimes* interleaves proves nothing, which is the
        # lesson already learned on the retry seam.
        self._gate: threading.Barrier | None = None
        # Pre-build a complete WAV container in memory and write *those* bytes
        # below. Writing through a live `wave` handle is not usable here:
        # `Wave_write` has no `flush()`, and it patches the RIFF/data sizes on
        # `close()`, so the header would always describe the final writer and the
        # two-half interleaving that provokes the truncation would be lost.
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as container:
            container.setnchannels(1)
            container.setsampwidth(2)
            container.setframerate(sample_rate)
            container.writeframes(self._payload)
        self._container = buffer.getvalue()

    def available(self) -> tuple[bool, str]:
        return True, "slow stub denoiser"

    @property
    def expected_bytes(self) -> int:
        """Length of the complete container, so a reader can spot a short file."""
        return len(self._container)

    def arm(self, callers: int) -> None:
        """Hold the next `callers` invocations at a common point before writing.

        Under the fixed code only the claim winner reaches the denoiser, so the
        callers that should sync is 1; the parameter exists so a test that
        deliberately exercises the unfixed shape can widen the window.
        """
        self._gate = threading.Barrier(callers)

    def denoise(self, src: Path, dst: Path):
        from app.pipeline.denoise import DenoiseResult

        with self._lock:
            self.calls += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self._gate is not None:
            try:
                # Every armed caller reaches the open() below at the same
                # instant, so any interleaving is produced by the code under
                # test rather than by luck.
                self._gate.wait(timeout=10)
            except threading.BrokenBarrierError:  # pragma: no cover - diagnosis aid
                pass
        # Write the real container in two halves with a pause between. If the
        # code under test passes the canonical path as `dst`, that path holds a
        # partial file for the duration of the pause — which is what the watcher
        # below looks for.
        half = len(self._container) // 2
        with dst.open("wb") as handle:
            handle.write(self._container[:half])
            handle.flush()
            threading.Event().wait(0.15)
            handle.write(self._container[half:])
        return DenoiseResult(path=dst, engine="stub-slow", settings={"call": self.calls})


@pytest.fixture
def slow_denoiser(monkeypatch):
    """Install the slow stub and hand back a factory bound to the uploaded audio."""
    from app.pipeline import jobs as job_pipeline

    def install(audio_path: Path) -> _SlowStubDenoiser:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
        stub = _SlowStubDenoiser(frames, rate)
        monkeypatch.setattr(job_pipeline, "get_denoiser", lambda *a, **k: stub)
        return stub

    return install


def _fire(client, recording_id: str) -> int:
    """One denoise request. An exception counts as a server error, i.e. a 500."""
    try:
        return client.post(
            f"/api/recordings/{recording_id}/denoise", headers=ADMIN_HEADERS
        ).status_code
    except Exception:  # noqa: BLE001 - an exception is a 500 to a client
        return 500


# --------------------------------------------------------------------------- #
# denoise: the artefact
# --------------------------------------------------------------------------- #

def test_concurrent_denoise_never_publishes_a_partial_derivative(
    client, witness_sample, slow_denoiser
):
    """The defect: concurrent denoise exposed a half-written derivative.

    On the unfixed code each caller passed the canonical `denoised_path()` to
    the denoiser, so while one writer was paused mid-file the canonical path
    held a *partial* file. A reader at that moment — ffmpeg's level-matching
    stage, a bundle being zipped — saw a truncated derivative, which is where
    `422 "level matching failed: "` came from.

    **Why this assertion and not "the final file decodes".** All callers write
    byte-identical output, so a shared-path race still lands the correct bytes
    once the last writer closes. Checking only the finished artefact therefore
    passes on the broken code — verified by mutation: reverting
    `_denoise_atomically` to write straight to `canonical` left this test green
    until the watcher was added. The property that actually distinguishes atomic
    publish from shared-path writing is that the canonical path is *never
    observable* in a partial state, so that is what is sampled here,
    continuously, while the write is in flight.

    The claim also means only one caller reaches the denoiser, so the stub gate
    is armed for exactly one. The window being sampled is the single writer's.
    """
    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    recording_id = record["id"]
    stub = slow_denoiser(audio_path)  # installed AFTER upload, so ingest is untouched

    derivative = _derivative_path(recording_id)
    expected = stub.expected_bytes

    # Sample the canonical path for as long as the writer is working. A partial
    # observation is any size strictly between empty and complete: `os.replace`
    # makes the transition absent -> complete in one step, so an intermediate
    # size can only come from writing in place.
    partial_sightings: list[int] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                size = derivative.stat().st_size
            except FileNotFoundError:
                size = -1
            if 0 < size < expected:
                partial_sightings.append(size)
            stop.wait(0.002)

    stub.arm(1)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    try:
        with ThreadPoolExecutor(max_workers=CONCURRENT_DENOISE) as pool:
            codes = list(pool.map(lambda i: _fire(client, recording_id), range(CONCURRENT_DENOISE)))
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert 500 not in codes, f"concurrent denoise leaked a server error: {codes}"
    assert partial_sightings == [], (
        f"the canonical derivative was readable in a partial state "
        f"({len(partial_sightings)} sightings of sizes "
        f"{sorted(set(partial_sightings))} vs expected {expected}); "
        f"the publish is not atomic. codes={codes}"
    )

    # And the finished artefact is a complete, decodable WAV.
    assert derivative.exists(), f"no derivative produced; codes={codes}"
    ok, frames = _decodable(derivative)
    assert ok, f"derivative is not a readable WAV; codes={codes}"
    assert frames > 0, "derivative is empty"

    # A stale scratch file beside the derivative would be shipped in a bundle.
    strays = [
        p for p in derivative.parent.iterdir()
        if p.name.startswith(f"{recording_id}.denoised.") and p != derivative
    ]
    assert strays == [], f"scratch files left behind: {[p.name for p in strays]}"


# --------------------------------------------------------------------------- #
# denoise: the custody record
# --------------------------------------------------------------------------- #

def test_concurrent_denoise_produces_one_audit_event(client, witness_sample, slow_denoiser):
    """A derivative produced once must be recorded once.

    This is the custody defect the lead found: the file was correct while the
    audit trail claimed four rebuilds. `/provenance` collapses repeats into a
    count, so the reviewer-facing view read "denoised ×4". The invariant is "one
    artefact, one event", which is only achievable by admitting one caller —
    hence the claim, not a suppression of the extra rows.
    """
    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    recording_id = record["id"]
    stub = slow_denoiser(audio_path)
    stub.arm(1)

    with ThreadPoolExecutor(max_workers=CONCURRENT_DENOISE) as pool:
        codes = list(pool.map(lambda i: _fire(client, recording_id), range(CONCURRENT_DENOISE)))

    assert 500 not in codes, f"concurrent denoise leaked a server error: {codes}"

    accepted = [code for code in codes if 200 <= code < 300]
    assert len(accepted) == 1, f"exactly one caller should win; codes={codes}"
    losers = [code for code in codes if code != accepted[0]]
    assert losers == [409] * (CONCURRENT_DENOISE - 1), (
        f"losers must be told 409, not given another status: codes={codes}"
    )

    count = _audit_count(recording_id)
    assert count == 1, (
        f"one artefact must be one audit event, not {count}; "
        f"the chain of custody is claiming rebuilds that did not happen"
    )

    ok, frames = _decodable(_derivative_path(recording_id))
    assert ok and frames > 0, "the winning call must still leave a usable derivative"


def test_denoise_loser_gets_the_same_message_as_a_queued_job(
    client, witness_sample, slow_denoiser
):
    """A lost denoise race is indistinguishable from "somebody got there first".

    Same string as `/retry` returns, sourced from `errors.ACTIVE_JOB_MESSAGE`, so
    a client cannot end up with two vocabularies for one situation.
    """
    from app.errors import ACTIVE_JOB_MESSAGE

    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    recording_id = record["id"]
    slow_denoiser(audio_path).arm(1)

    def denoise(_index: int):
        try:
            return client.post(
                f"/api/recordings/{recording_id}/denoise", headers=ADMIN_HEADERS
            )
        except Exception as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=CONCURRENT_DENOISE) as pool:
        responses = list(pool.map(denoise, range(CONCURRENT_DENOISE)))

    statuses = [getattr(r, "status_code", None) for r in responses]
    assert 500 not in statuses, f"a server error leaked: {statuses}"

    losers = [r for r in responses if getattr(r, "status_code", None) == 409]
    assert losers, f"expected at least one 409 among {statuses}"
    for response in losers:
        assert response.json()["detail"] == ACTIVE_JOB_MESSAGE


def test_sequential_denoise_still_works(client, witness_sample, slow_denoiser):
    """Baseline the fix must not break: one call, one derivative, one event."""
    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    slow_denoiser(audio_path)  # after upload: the ingest denoise stage must not use it

    response = client.post(
        f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["has_denoised"] is True

    ok, frames = _decodable(_derivative_path(record["id"]))
    assert ok and frames > 0
    assert _audit_count(record["id"]) == 1

    # The claim is released, so the exhibit is not left looking busy.
    assert _active_jobs(record["id"]) == []


def test_denoise_can_be_repeated_after_it_finishes(client, witness_sample, slow_denoiser):
    """"One caller at a time" must not become "one derivative ever".

    Re-denoising after a settings change is a legitimate request — it is what an
    operator does when the first result looks wrong — and each accepted run is a
    truthful, attributable rebuild.
    """
    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    slow_denoiser(audio_path)

    first = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert first.status_code == 200, first.text

    second = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["has_denoised"] is True

    assert _audit_count(record["id"]) == 2


def test_denoise_is_blocked_while_an_ingest_job_is_active(client, witness_sample):
    """Denoise shares the one slot with ingest, so it cannot run underneath one.

    A documented consequence of using the same index: a denoise during a re-index
    is refused rather than racing the pipeline's own denoise stage, which writes
    the same file.
    """
    from app.db import session_scope
    from app.models import Job

    record = upload_ok(client, witness_sample)
    with session_scope() as session:
        session.add(Job(recording_id=record["id"], kind="process", status="running"))

    response = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert response.status_code == 409
    assert _audit_count(record["id"]) == 0


def test_denoise_records_the_canonical_path_not_the_scratch_file(
    client, witness_sample, slow_denoiser
):
    """The persisted path must be the published one, never a temp name.

    `denoise_existing` used to use the denoiser's returned `result.path` directly.
    With the atomic publish the denoiser returns a scratch path, so this pins that
    the recording is re-pointed at the canonical derivative — otherwise
    `/audio?variant=denoised` would 404 on a file that had already been moved.
    """
    audio_path, _ = witness_sample
    record = upload_ok(client, witness_sample)
    slow_denoiser(audio_path)  # after upload: the ingest denoise stage must not use it

    assert client.post(
        f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS
    ).status_code == 200

    audio = client.get(
        f"/api/recordings/{record['id']}/audio?variant=denoised", headers=ADMIN_HEADERS
    )
    assert audio.status_code == 200, "canonical derivative is not being served"
    assert len(audio.content) > 0
    assert _derivative_path(record["id"]).exists()

    # The audit event names the job that produced it, so the artefact can be
    # traced to exactly one call.
    events = client.get(
        f"/api/audit?recording_id={record['id']}", headers=ADMIN_HEADERS
    ).json()
    denoised = [e for e in events if e["action"] == "recording.denoised"]
    assert len(denoised) == 1
    assert denoised[0]["detail"]["job_id"].startswith("job_")


def test_a_failed_denoise_releases_the_slot_and_leaves_no_debris(
    client, witness_sample, monkeypatch
):
    """A denoise that dies mid-write must clean up and stop holding the exhibit.

    A crashed *process* is recoverable through `POST /api/jobs/{id}/reset`, but a
    denoise that fails knows it is finished and says so itself — an operator
    should never have to reset a job that already reported `failed`.
    """
    from app.pipeline import jobs as job_pipeline

    class _DyingDenoiser:
        name = "dying"

        def available(self):
            return True, "dies mid-write"

        def denoise(self, src, dst):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"partial")
            raise RuntimeError("simulated crash")

    record = upload_ok(client, witness_sample)
    monkeypatch.setattr(job_pipeline, "get_denoiser", lambda *a, **k: _DyingDenoiser())

    with pytest.raises(RuntimeError):
        client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)

    # No scratch debris: the `denoised/` directory ships in a bundle.
    from app.config import get_settings

    directory = get_settings().denoised_dir
    strays = (
        [p for p in directory.iterdir() if p.name.startswith(f"{record['id']}.denoised.")]
        if directory.exists()
        else []
    )
    assert strays == [], f"scratch files left behind: {[p.name for p in strays]}"

    # Nothing is holding the exhibit.
    assert _active_jobs(record["id"]) == [], "a failed denoise left an active job"

    jobs = client.get(f"/api/recordings/{record['id']}/jobs", headers=ADMIN_HEADERS).json()
    assert any(job["stage"] == "denoise.failed" for job in jobs)
    assert _audit_count(record["id"]) == 0, "a failed denoise produced no artefact"

    # And the exhibit can be retried immediately.
    retried = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert retried.status_code == 200, retried.text


def test_denoise_with_no_engine_claims_nothing(client, witness_sample):
    """A 503 is a capability problem, not contention, so no slot is taken.

    `DENOISER=none` in the test environment. The exhibit must not be left locked
    by a request that could not have worked.
    """
    record = upload_ok(client, witness_sample)
    response = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()
    assert _active_jobs(record["id"]) == []


# --------------------------------------------------------------------------- #
# the `submit()` contract (upload path: documented, not hardened)
# --------------------------------------------------------------------------- #

def test_submit_raises_active_job_conflict_rather_than_integrity_error(
    client, witness_sample
):
    """`submit()` must surface the domain error, not the raw driver error.

    Called directly, not through HTTP. The upload route intentionally has no
    `except ActiveJobConflict` (a fresh `recording_id` makes the collision
    unreachable there), so this is the only place the contract can be asserted —
    and it is what a *future* caller that can collide will depend on.
    """
    from app.db import session_scope
    from app.errors import ActiveJobConflict, DependencyUnavailable, ProcessingError
    from app.models import Job, Recording
    from app.pipeline import jobs as job_pipeline

    record = upload_ok(client, witness_sample)
    recording_id = record["id"]

    # Park an active job, then ask `submit()` for a second one for the same exhibit.
    with session_scope() as session:
        session.add(Job(recording_id=recording_id, kind="process", status="running"))

    with session_scope() as session:
        recording = session.get(Recording, recording_id)
        with pytest.raises(ActiveJobConflict) as caught:
            job_pipeline.submit(session, recording, "test.admin")

    # The message is the one the HTTP layer returns, so a caller mapping this to
    # 409 cannot drift from the documented body.
    assert caught.value.message == "A processing job is already queued or running."
    assert caught.value.recording_id == recording_id

    # And it must be the *domain* error, not something a caller has to classify.
    assert not isinstance(caught.value, (DependencyUnavailable, ProcessingError))

    # Nothing was left half-written by the failed submit.
    assert [row[1] for row in _active_jobs(recording_id)] == ["running"]


def test_a_normal_upload_is_not_refused_by_the_active_job_index(client, witness_sample):
    """The un-hardened upload path is safe because every upload mints a new id.

    This is the evidence for leaving that route without a 409 handler: two
    uploads of the same file both succeed, each on its own recording, so the
    insert that *could* raise cannot be reached that way.
    """
    first = upload_ok(client, witness_sample)
    second = upload_ok(client, witness_sample)

    assert first["id"] != second["id"], "uploads reused a recording id"
    assert first["status"] == "ready"
    assert second["status"] == "ready"
