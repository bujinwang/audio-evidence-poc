"""Search filter object shared by the keyword, semantic, and hybrid paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SearchFilters:
    recording_ids: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    review_statuses: list[str] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    variant: str = "original"

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.recording_ids,
                self.source_types,
                self.speakers,
                self.review_statuses,
                self.date_from,
                self.date_to,
            ]
        )

    def as_dict(self) -> dict:
        return {
            "recording_ids": self.recording_ids,
            "source_types": self.source_types,
            "speakers": self.speakers,
            "review_statuses": self.review_statuses,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "variant": self.variant,
        }
