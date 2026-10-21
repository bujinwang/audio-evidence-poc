"""Shared error types for the processing pipeline."""

from __future__ import annotations

# Single definition of the 409 body for "this exhibit is already being worked
# on". The sequential path and the concurrent path must be indistinguishable to
# a client — one caller wins, the other is told to wait — so both read the text
# from here rather than keeping two strings in step by hand.
ACTIVE_JOB_MESSAGE = "A processing job is already queued or running."


class DependencyUnavailable(RuntimeError):
    """A processing component is not installed/configured.

    This is not a pipeline failure: the job records the reason and continues
    with whatever is available (PRD FR-02 / acceptance criterion 3).
    """

    def __init__(self, component: str, reason: str):
        self.component = component
        self.reason = reason
        super().__init__(f"{component} unavailable: {reason}")


class ProcessingError(RuntimeError):
    """The component exists but failed on this input."""


class ActiveJobConflict(RuntimeError):
    """The recording already has a queued or running job.

    Raised when the database itself refuses a second active job for one
    recording (`uq_job_active_recording`). This is a *state* conflict, not a
    failure: the caller asked for something that is already true, and it maps
    to HTTP 409 exactly as if it had arrived a moment later.
    """

    def __init__(self, recording_id: str, message: str = ACTIVE_JOB_MESSAGE):
        self.recording_id = recording_id
        self.message = message
        super().__init__(message)
