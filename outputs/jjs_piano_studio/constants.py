"""Constants, paths, colours, key maps, and note utilities."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# ── Application ──────────────────────────────────────────────────
APP_TITLE = "JJS Piano Studio"
CONFIG_PATH = Path(__file__).parent.parent / "piano_macro_settings.json"
LIBRARY_PATH = Path(__file__).parent.parent / "piano_studio_library.json"
JOB_DIR = Path(__file__).parent.parent / "audio_conversion_jobs"
SONG_EXPORT_SUFFIX = ".jjspiano.json"
ONLINE_MIDI_DIR = Path(__file__).parent.parent / "online_midis"

# ── Hotkeys ──────────────────────────────────────────────────────
DEFAULT_HOTKEY_PLAY = "F6"
DEFAULT_HOTKEY_STOP = "F8"
DEFAULT_HOTKEY_PAUSE = "F7"

# ── Online Sequencer ─────────────────────────────────────────────
ONLINE_SEQUENCER_BASE_URL = "https://onlinesequencer.net"
ONLINE_SEARCH_TIMEOUT_SECONDS = 16
ONLINE_DOWNLOAD_TIMEOUT_SECONDS = 30
ONLINE_DOWNLOAD_MAX_BYTES = 24 * 1024 * 1024
ONLINE_SEQUENCE_TICKS_PER_BEAT = 384
ONLINE_SEQUENCE_TICKS_PER_UNIT = 96
ONLINE_SEARCH_MAX_CANDIDATES = 72
ONLINE_SEARCH_WORKERS = 6
ONLINE_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 JJS-Piano-Studio"
)
ONLINE_SEARCH_SORT_OPTIONS = (
    "Best playable",
    "Best match",
    "Most plays",
    "Most notes",
    "Fewest notes",
    "Newest",
    "Title A-Z",
    "Author A-Z",
)

# ── Note-art detection ──────────────────────────────────────────
NOTE_ART_SCAN_SECONDS = 60.0
NOTE_ART_BIN_SECONDS = 0.5
NOTE_ART_INITIAL_WINDOW_SECONDS = 3.0
NOTE_ART_MAX_SPAM_GAP_SECONDS = 3.0
NOTE_ART_MIN_REMOVED_NOTES = 80
NOTE_ART_STARTS_PER_SECOND = 55.0
NOTE_ART_UNIQUE_PITCHES_PER_BIN = 30
NOTE_ART_ACTIVE_NOTES = 28

# ── Timing / scheduling ─────────────────────────────────────────
ACTION_GROUP_EPSILON_SECONDS = 0.0015
HIGH_RES_TIMER_MS = 1
COARSE_WAIT_SECONDS = 0.006
FINE_WAIT_SECONDS = 0.001

# ── Dark UI colour palette ──────────────────────────────────────
UI_BG = "#0d1117"
UI_SURFACE = "#151b23"
UI_SURFACE_HOVER = "#1f2937"
UI_FIELD = "#0a0f16"
UI_BORDER = "#2f3a46"
UI_TEXT = "#e6edf3"
UI_MUTED = "#95a3b3"
UI_ACCENT = "#43c7f4"
UI_ACCENT_DARK = "#1686ad"
UI_ACCENT_TEXT = "#06121a"
UI_DANGER = "#ef6b73"
UI_DANGER_DARK = "#8f3138"
UI_SELECTION = "#263d54"
UI_KEY_WHITE = "#eef3f7"
UI_KEY_WHITE_ACTIVE = "#87dcff"
UI_KEY_BLACK = "#070a0f"
UI_KEY_BLACK_ACTIVE = "#28bdfd"
UI_KEY_OUTLINE = "#2f3945"
UI_FONT = "{Segoe UI} 10"
UI_TITLE_FONT = "{Segoe UI} 20 bold"
UI_SECTION_FONT = "{Segoe UI} 11 bold"

# ── Note / key maps ─────────────────────────────────────────────
NOTE_BASE = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_RE = re.compile(r"^([A-Ga-g])([#bB]?)(-?\d+)$")
NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


# ── Note name utilities ─────────────────────────────────────────
def note_name_to_midi(note: str) -> int:
    match = NOTE_RE.match(note.strip())
    if not match:
        raise ValueError(f"Invalid note name: {note!r}")
    letter, accidental, octave_text = match.groups()
    semitone = NOTE_BASE[letter.upper()]
    accidental = accidental.replace("B", "b")
    if accidental == "#":
        semitone += 1
    elif accidental == "b":
        semitone -= 1
    octave = int(octave_text)
    return (octave + 1) * 12 + semitone


def midi_to_note_name(midi_note: int) -> str:
    octave = midi_note // 12 - 1
    return f"{NOTE_NAMES[midi_note % 12]}{octave}"


# ── Key binding ─────────────────────────────────────────────────
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyBinding:
    label: str
    base_key: str
    shifted: bool


def key_label_to_binding(label: str) -> KeyBinding:
    shifted_symbols = {
        "!": "1",
        "@": "2",
        "#": "3",
        "$": "4",
        "%": "5",
        "^": "6",
        "&": "7",
        "*": "8",
        "(": "9",
        ")": "0",
    }
    if len(label) != 1:
        raise ValueError(f"Key label must be one character: {label!r}")
    if label in shifted_symbols:
        return KeyBinding(label=label, base_key=shifted_symbols[label], shifted=True)
    if label.isalpha() and label.isupper():
        return KeyBinding(label=label, base_key=label.lower(), shifted=True)
    return KeyBinding(label=label, base_key=label.lower(), shifted=False)


def build_visual_pianos_map() -> dict[int, KeyBinding]:
    """Visual Pianos layout, left to right, C2 through C7."""
    white_labels = list("1234567890qwertyuiopasdfghjklzxcvbnm")
    black_labels = [
        "!", "@", "$", "%", "^", "*", "(",
        "Q", "W", "E", "T", "Y", "I", "O", "P",
        "S", "D", "G", "H", "J", "L", "Z", "C", "V", "B",
    ]
    result: dict[int, KeyBinding] = {}
    white_i = 0
    black_i = 0
    for midi_note in range(note_name_to_midi("C2"), note_name_to_midi("C7") + 1):
        if "#" in midi_to_note_name(midi_note):
            label = black_labels[black_i]
            black_i += 1
        else:
            label = white_labels[white_i]
            white_i += 1
        result[midi_note] = key_label_to_binding(label)
    return result


KEY_MAP = build_visual_pianos_map()
RAW_KEY_LABELS = {binding.label for binding in KEY_MAP.values()}
NOTE_OPTIONS = [midi_to_note_name(midi_note) for midi_note in KEY_MAP]
WHITE_MIDI_NOTES = [midi_note for midi_note in KEY_MAP if "#" not in midi_to_note_name(midi_note)]
BLACK_MIDI_NOTES = [midi_note for midi_note in KEY_MAP if "#" in midi_to_note_name(midi_note)]

# ── Search history ──────────────────────────────────────────────
SEARCH_HISTORY_PATH = Path(__file__).parent.parent / "search_history.json"
SEARCH_HISTORY_MAX = 50


def load_search_history() -> list[str]:
    try:
        if SEARCH_HISTORY_PATH.exists():
            data = json.loads(SEARCH_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(item) for item in data if isinstance(item, str) and item.strip()]
    except Exception:
        pass
    return []


def save_search_history(history: list[str]) -> None:
    try:
        unique = list(dict.fromkeys(item.strip() for item in history if item.strip()))
        SEARCH_HISTORY_PATH.write_text(json.dumps(unique[:SEARCH_HISTORY_MAX], indent=2), encoding="utf-8")
    except Exception:
        pass


def add_to_search_history(query: str, history: list[str]) -> list[str]:
    clean = query.strip()
    if not clean:
        return history
    updated = [clean] + [item for item in history if item.lower() != clean.lower()]
    return updated[:SEARCH_HISTORY_MAX]


# ── Song library helpers ────────────────────────────────────────
def new_song_id() -> str:
    return f"song-{int(time.time() * 1000)}"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── Search stop words ───────────────────────────────────────────
STOP_WORDS = {
    "a", "an", "and", "by", "cover", "for", "from", "jjs",
    "midi", "music", "of", "official", "online", "piano",
    "roblox", "sequence", "sequencer", "sheet", "the", "tutorial",
}
