"""Data models for JJS Piano Studio."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Playable:
    kind: str          # "midi" or "key"
    value: int | str   # MIDI number or key label


@dataclass(frozen=True)
class ScoreEvent:
    notes: tuple[Playable, ...]
    beats: float


@dataclass(frozen=True)
class ScheduledAction:
    seconds: float
    action: str         # "down" or "up"
    notes: tuple[Playable, ...]


@dataclass(frozen=True)
class AudioMidiNote:
    start: float
    end: float
    midi: int
    velocity: int = 84


@dataclass(frozen=True)
class OnlineSequenceResult:
    sequence_id: str
    title: str
    author: str = ""
    plays: str = ""
    notes: str = ""
    updated: str = ""
    url: str = ""
    midi_url: str = ""
    source: str = ""


@dataclass(frozen=True)
class OnlineSequenceNote:
    start_beats: float
    length_beats: float
    midi: int
    instrument: int = 0
    volume: float = 1.0


@dataclass(frozen=True)
class OnlineSequenceData:
    bpm: float
    notes: tuple[OnlineSequenceNote, ...]


@dataclass(frozen=True)
class PreparedOnlineMidi:
    path: Path
    result: OnlineSequenceResult
    actions: tuple[ScheduledAction, ...]
    bpm: float | None
    summary: str
    repaired: bool = False
    trimmed_seconds: float = 0.0
    trimmed_notes: int = 0
    cleaned_before_notes: int = 0
    cleaned_after_notes: int = 0


@dataclass
class PlaybackSettings:
    bpm: float
    speed: float
    default_beats: float
    hold_percent: float
    gap_ms: float
    start_delay: float
    start_offset_seconds: float
    end_at_seconds: float
    transpose: int
    low_midi: int
    high_midi: int
    range_mode: str


class SongLibraryEntry(dict):
    """Typed wrapper for library entries with favourites support."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def entry_id(self) -> str:
        return str(self.get("id", ""))

    @property
    def title(self) -> str:
        return str(self.get("title", "Untitled Song"))

    @property
    def artist(self) -> str:
        return str(self.get("artist", ""))

    @property
    def tags(self) -> str:
        return str(self.get("tags", ""))

    @property
    def text(self) -> str:
        return str(self.get("text", ""))

    @property
    def is_favorite(self) -> bool:
        return bool(self.get("favorite", False))

    @property
    def play_count(self) -> int:
        return int(self.get("play_count", 0))

    @property
    def last_played(self) -> str:
        return str(self.get("last_played", ""))
