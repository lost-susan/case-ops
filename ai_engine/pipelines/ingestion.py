"""Ingestion provider contract (OCR for scans, ASR for audio, event extraction).

Milestone 1 ships only MockProvider. A real provider implements `IngestionProvider.process` and returns lines
(the unit of proof) plus events that point at those lines by index. The backend rejects any event whose
line_index is out of range, so nothing can enter the timeline without raw-source proof."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

FinalStatus = Literal["awaiting_extraction", "parsed", "transcribed"]


@dataclass(frozen=True)
class ExtractedLine:
    text: str
    page: int = 1
    bbox: tuple[int, int, int, int] | None = None
    start_sec: float | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ExtractedEvent:
    title: str
    occurred_at: datetime  # timezone-aware
    line_index: int  # index into ExtractionResult.lines: the proof for this event
    actors: tuple[str, ...] = ()
    status: Literal["pending", "verified", "conflict"] = "pending"


@dataclass(frozen=True)
class ExtractionResult:
    status: FinalStatus
    lines: tuple[ExtractedLine, ...]
    events: tuple[ExtractedEvent, ...] = field(default_factory=tuple)
    confidence: float | None = None  # mean OCR/ASR confidence, 0-100


class IngestionProvider(Protocol):
    def process(self, *, path: Path, kind: str, filename: str, sha256: str, size_bytes: int,
                received_at: datetime) -> ExtractionResult: ...


class MockProvider:
    """Extracts nothing. Records file metadata as proof lines and one 'pending' event, so the full upload -> proof
    flow works end to end until a real OCR/ASR engine is plugged in."""

    def process(self, *, path: Path, kind: str, filename: str, sha256: str, size_bytes: int,
                received_at: datetime) -> ExtractionResult:
        lines = (
            ExtractedLine(f"FILE: {filename}"),
            ExtractedLine(f"TYPE: {kind.upper()} • SIZE: {size_bytes} bytes"),
            ExtractedLine(f"SHA-256: {sha256}"),
            ExtractedLine("No OCR / transcription engine is connected. No text has been extracted from this file."),
        )
        event = ExtractedEvent(title=f"Ingested: {filename}", occurred_at=received_at, line_index=0)
        return ExtractionResult(status="awaiting_extraction", lines=lines, events=(event,), confidence=None)


def get_provider(name: str) -> IngestionProvider:
    if name == "mock":
        return MockProvider()
    raise ValueError(f"Unknown ingestion provider: {name}")
