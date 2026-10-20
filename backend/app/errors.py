"""Shared error types for the processing pipeline."""

from __future__ import annotations


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
