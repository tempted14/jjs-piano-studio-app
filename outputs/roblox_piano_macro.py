"""
Roblox / Visual Pianos style macro player.

Windows GUI for playing Visual Pianos-style keyboard layouts with hotkeys,
MIDI import, online search, and audio-to-MIDI conversion.
"""

from __future__ import annotations

import base64
import html
import math
import bisect
import ctypes
import importlib
import json
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import struct
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen
from ctypes import wintypes

from jjs_piano_studio.constants import (
    ACTION_GROUP_EPSILON_SECONDS,
    APP_TITLE,
    BLACK_MIDI_NOTES,
    COARSE_WAIT_SECONDS,
    CONFIG_PATH,
    DEFAULT_HOTKEY_PAUSE,
    DEFAULT_HOTKEY_PLAY,
    DEFAULT_HOTKEY_STOP,
    FINE_WAIT_SECONDS,
    HIGH_RES_TIMER_MS,
    JOB_DIR,
    KEY_MAP,
    KeyBinding,
    key_label_to_binding,
    LIBRARY_PATH,
    midi_to_note_name,
    new_song_id,
    NOTE_ART_ACTIVE_NOTES,
    NOTE_ART_BIN_SECONDS,
    NOTE_ART_INITIAL_WINDOW_SECONDS,
    NOTE_ART_MAX_SPAM_GAP_SECONDS,
    NOTE_ART_MIN_REMOVED_NOTES,
    NOTE_ART_SCAN_SECONDS,
    NOTE_ART_STARTS_PER_SECOND,
    NOTE_ART_UNIQUE_PITCHES_PER_BIN,
    NOTE_BASE,
    NOTE_NAMES,
    NOTE_OPTIONS,
    NOTE_RE,
    note_name_to_midi,
    now_stamp,
    NUMBER_RE,
    ONLINE_DOWNLOAD_MAX_BYTES,
    ONLINE_DOWNLOAD_TIMEOUT_SECONDS,
    ONLINE_MIDI_DIR,
    ONLINE_SEARCH_MAX_CANDIDATES,
    ONLINE_SEARCH_SORT_OPTIONS,
    ONLINE_SEARCH_TIMEOUT_SECONDS,
    ONLINE_SEARCH_USER_AGENT,
    ONLINE_SEARCH_WORKERS,
    ONLINE_SEQUENCER_BASE_URL,
    ONLINE_SEQUENCE_TICKS_PER_BEAT,
    ONLINE_SEQUENCE_TICKS_PER_UNIT,
    RAW_KEY_LABELS,
    SONG_EXPORT_SUFFIX,
    STOP_WORDS,
    load_search_history,
    save_search_history,
    add_to_search_history,
    SEARCH_HISTORY_MAX,
    UI_ACCENT,
    UI_ACCENT_DARK,
    UI_ACCENT_TEXT,
    UI_BG,
    UI_BORDER,
    UI_DANGER,
    UI_DANGER_DARK,
    UI_FIELD,
    UI_FONT,
    UI_KEY_BLACK,
    UI_KEY_BLACK_ACTIVE,
    UI_KEY_OUTLINE,
    UI_KEY_WHITE,
    UI_KEY_WHITE_ACTIVE,
    UI_MUTED,
    UI_SECTION_FONT,
    UI_SELECTION,
    UI_SURFACE,
    UI_SURFACE_HOVER,
    UI_TEXT,
    UI_TITLE_FONT,
    WHITE_MIDI_NOTES,
)
from jjs_piano_studio.models import (
    AudioMidiNote,
    OnlineSequenceData,
    OnlineSequenceNote,
    OnlineSequenceResult,
    Playable,
    PlaybackSettings,
    PreparedOnlineMidi,
    ScheduledAction,
    ScoreEvent,
)
from jjs_piano_studio.sender import (
    begin_high_resolution_timer,
    end_high_resolution_timer,
    KeySender,
    MidiSynth,
    MIDI_INSTRUMENTS,
    wait_until_precise,
)

try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:
    pynput_keyboard = None

try:
    import mido
except Exception:
    mido = None

np = None
librosa = None


# ── All constants, types, and key mappings are now imported from jjs_piano_studio.* ──


def strip_line_comment(line: str) -> str:
    # Keep sharps in note names intact; only // starts a comment.
    return line.split("//", 1)[0]


def tokenize_score(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    text = "\n".join(strip_line_comment(line) for line in text.splitlines())
    while i < len(text):
        char = text[i]
        if char.isspace() or char in "|,":
            i += 1
            continue
        if char in "[(":
            close = "]" if char == "[" else ")"
            end = text.find(close, i + 1)
            if end == -1:
                raise ValueError(f"Unclosed chord starting at: {text[i:i + 24]!r}")
            j = end + 1
            if j < len(text) and text[j] in ":/":
                j += 1
                while j < len(text) and (text[j].isdigit() or text[j] == "."):
                    j += 1
            tokens.append(text[i:j])
            i = j
            continue
        j = i
        while j < len(text) and not text[j].isspace() and text[j] not in "|,":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def split_duration(token: str) -> tuple[str, float | None]:
    for separator in (":", "/"):
        if separator in token:
            left, right = token.rsplit(separator, 1)
            if NUMBER_RE.match(right):
                return left, float(right)
    return token, None


def parse_playable(token: str) -> Playable | None:
    clean = token.strip()
    if not clean:
        return None
    if clean.lower() in {"r", "rest", "-", "_"}:
        return None
    if clean.lower().startswith("key:"):
        key_label = clean[4:]
        if len(key_label) == 1 and key_label in RAW_KEY_LABELS:
            return Playable("key", key_label)
        raise ValueError(f"Unknown raw key label: {clean!r}")
    if NOTE_RE.match(clean):
        return Playable("midi", note_name_to_midi(clean))
    if len(clean) == 1 and clean in RAW_KEY_LABELS:
        return Playable("key", clean)
    raise ValueError(
        f"Could not parse {clean!r}. Use notes like C4, F#4, Bb3, rests like R, "
        "raw keys like q or key:Q, and chords like [C4 E4 G4]."
    )


def parse_text_score(text: str, default_beats: float) -> list[ScoreEvent]:
    events: list[ScoreEvent] = []
    for token in tokenize_score(text):
        if token[0] in "[(":
            close = "]" if token[0] == "[" else ")"
            close_at = token.rfind(close)
            if close_at == -1:
                raise ValueError(f"Invalid chord token: {token!r}")
            inner = token[1:close_at]
            tail = token[close_at + 1 :]
            beats = default_beats
            if tail:
                if tail[0] not in ":/" or not NUMBER_RE.match(tail[1:]):
                    raise ValueError(f"Invalid chord duration: {token!r}")
                beats = float(tail[1:])
            pieces = [piece for piece in re.split(r"[\s,]+", inner.strip()) if piece]
            notes = tuple(note for piece in pieces if (note := parse_playable(piece)) is not None)
            events.append(ScoreEvent(notes=notes, beats=beats))
            continue

        body, duration = split_duration(token)
        playable = parse_playable(body)
        beats = duration if duration is not None else default_beats
        events.append(ScoreEvent(notes=tuple() if playable is None else (playable,), beats=beats))
    return events


def text_events_to_actions(events: list[ScoreEvent], settings: PlaybackSettings) -> list[ScheduledAction]:
    actions: list[ScheduledAction] = []
    cursor = 0.0
    beat_seconds = 60.0 / settings.bpm
    for event in events:
        duration = max(0.01, event.beats * beat_seconds)
        hold_seconds = duration * settings.hold_percent
        if settings.gap_ms > 0:
            hold_seconds = min(hold_seconds, max(0.01, duration - settings.gap_ms / 1000.0))
        if event.notes and hold_seconds > 0:
            actions.append(ScheduledAction(cursor, "down", event.notes))
            actions.append(ScheduledAction(cursor + hold_seconds, "up", event.notes))
        cursor += duration
    return coalesce_scheduled_actions(actions)


def coalesce_scheduled_actions(
    actions: list[ScheduledAction],
    tolerance_seconds: float = ACTION_GROUP_EPSILON_SECONDS,
) -> list[ScheduledAction]:
    if not actions:
        return []

    ordered = sorted(actions, key=lambda item: (item.seconds, 0 if item.action == "up" else 1))
    merged: list[ScheduledAction] = []
    current_seconds = ordered[0].seconds
    current_action = ordered[0].action
    current_notes: list[Playable] = list(ordered[0].notes)

    def flush() -> None:
        seen: set[tuple[str, int | str]] = set()
        unique_notes: list[Playable] = []
        for playable in current_notes:
            identity = (playable.kind, playable.value)
            if identity in seen:
                continue
            seen.add(identity)
            unique_notes.append(playable)
        merged.append(ScheduledAction(max(0.0, current_seconds), current_action, tuple(unique_notes)))

    for action in ordered[1:]:
        if action.action == current_action and abs(action.seconds - current_seconds) <= tolerance_seconds:
            current_notes.extend(action.notes)
            current_seconds = min(current_seconds, action.seconds)
            continue
        flush()
        current_seconds = action.seconds
        current_action = action.action
        current_notes = list(action.notes)

    flush()
    return merged


def load_midi_actions(path: str, track_indices: set[int] | None = None) -> list[ScheduledAction]:
    if mido is None:
        raise RuntimeError("MIDI support needs mido. Install it with: python -m pip install mido")
    midi_path = Path(path)
    mid = mido.MidiFile(midi_path)
    actions: list[ScheduledAction] = []
    current = 0.0
    track_num = -1
    for track in mid.tracks:
        track_num += 1
        if track_indices is not None and track_num not in track_indices:
            continue
        current = 0.0
        for msg in track:
            current += float(msg.time)
            if not hasattr(msg, "channel"):
                channel = None
            else:
                channel = msg.channel
            if channel == 9:
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                actions.append(ScheduledAction(current, "down", (Playable("midi", int(msg.note)),)))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                actions.append(ScheduledAction(current, "up", (Playable("midi", int(msg.note)),)))

    if not actions:
        raise ValueError("No playable note events were found in that MIDI file.")
    first = min(action.seconds for action in actions)
    normalized = [
        ScheduledAction(max(0.0, action.seconds - first), action.action, action.notes)
        for action in sorted(actions, key=lambda item: (item.seconds, 0 if item.action == "up" else 1))
    ]
    return coalesce_scheduled_actions(normalized)


def get_midi_track_info(path: str) -> list[dict[str, object]]:
    if mido is None:
        return []
    mid = mido.MidiFile(path)
    tracks: list[dict[str, object]] = []
    for idx, track in enumerate(mid.tracks):
        name = track.name or f"Track {idx + 1}"
        note_count = sum(1 for m in track if m.type in ("note_on", "note_off"))
        instruments: set[int] = set()
        for m in track:
            if hasattr(m, "channel") and m.channel is not None:
                instruments.add(m.channel)
        tracks.append({
            "index": idx,
            "name": name,
            "notes": note_count,
            "channels": sorted(instruments),
        })
    return tracks


def online_sequence_url(sequence_id: str) -> str:
    return f"{ONLINE_SEQUENCER_BASE_URL}/{sequence_id}"


def online_sequence_midi_url(sequence_id: str) -> str:
    return online_sequence_url(sequence_id)


def sanitize_filename(name: str, fallback: str = "online_sequence") -> str:
    clean = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name).strip(" ._")
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        clean = fallback
    return clean[:96]


def http_get_bytes(url: str, timeout: float, max_bytes: int | None = None) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": ONLINE_SEARCH_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/octet-stream,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            limit = max_bytes + 1 if max_bytes is not None else -1
            data = response.read(limit)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while opening {url}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"Could not reach the internet: {reason}") from exc
    if max_bytes is not None and len(data) > max_bytes:
        raise RuntimeError("Download is too large to safely import.")
    return data


def http_get_text(url: str, timeout: float = ONLINE_SEARCH_TIMEOUT_SECONDS) -> str:
    data = http_get_bytes(url, timeout=timeout, max_bytes=2_000_000)
    return data.decode("utf-8", errors="replace")


def strip_html_to_text(markup: str) -> str:
    clean = re.sub(r"<script\b.*?</script>", " ", markup, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def extract_online_sequence_id(text: str) -> str | None:
    clean = text.strip()
    clean_path = urlparse(clean).path.strip("/")
    if clean.isdigit() and 3 <= len(clean) <= 10:
        return clean
    if clean_path.isdigit() and 3 <= len(clean_path) <= 10:
        return clean_path
    patterns = [
        r"onlinesequencer\.net/(?:sequence/)?(\d{3,10})",
        r"onlinesequencer\.net/app/midi\.php\?id=(\d{3,10})",
        r"(?:sequence|seq|id)\s*#?\s*[:=]?\s*(\d{3,10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_online_sequence_ids_from_html(markup: str) -> list[str]:
    ids: list[str] = []

    def add_id(value: str | None) -> None:
        if value and value not in ids:
            ids.append(value)

    for href in re.findall(r"""href=["']([^"']+)["']""", markup, flags=re.IGNORECASE):
        decoded = html.unescape(href)
        parsed = urlparse(decoded)
        if "uddg" in parse_qs(parsed.query):
            decoded = unquote(parse_qs(parsed.query)["uddg"][0])
        add_id(extract_online_sequence_id(decoded))

    for match in re.finditer(r"onlinesequencer\.net/(?:sequence/)?(\d{3,10})", markup, flags=re.IGNORECASE):
        add_id(match.group(1))
    for match in re.finditer(r"onlinesequencer\.net/app/midi\.php\?id=(\d{3,10})", markup, flags=re.IGNORECASE):
        add_id(match.group(1))
    return ids


def parse_online_count(value: str) -> int:
    match = re.search(r"[\d,]+", value or "")
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def parse_online_date_key(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def normalize_search_words(text: str) -> list[str]:
    text = html.unescape(text).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return [word for word in text.split() if len(word) > 1 and word not in STOP_WORDS]


def compact_music_query(query: str) -> str:
    words = normalize_search_words(query)
    return " ".join(words)


def online_query_variants(query: str) -> list[str]:
    variants: list[str] = []

    def add_variant(value: str) -> None:
        clean = re.sub(r"\s+", " ", value).strip(" -_\"'")
        if clean and clean.lower() not in {item.lower() for item in variants}:
            variants.append(clean)

    add_variant(query)
    if " - " in query:
        parts = [part.strip() for part in query.split(" - ") if part.strip()]
        if len(parts) >= 2:
            add_variant(f"{parts[1]} {parts[0]}")
            add_variant(parts[1])
            add_variant(parts[0])
    add_variant(compact_music_query(query))
    add_variant(re.sub(r"\([^)]*\)|\[[^]]*\]", " ", query))
    add_variant(re.sub(r"\b(?:midi|piano|sheet music|sheet|ost|theme|cover|tutorial|roblox|jjs)\b", " ", query, flags=re.IGNORECASE))
    if ":" in query:
        for part in query.split(":"):
            add_variant(part)
    return variants[:7]


def _fuzzy_match_score(word: str, candidate: str) -> float:
    """Score how well `word` matches `candidate` using prefix, substring, and edit-distance."""
    w, c = word.lower(), candidate.lower()
    if w == c:
        return 1.0
    if c.startswith(w):
        return 0.92
    if w.startswith(c):
        return 0.88
    if w in c:
        return 0.72
    if c in w:
        return 0.68
    common = sum(1 for a, b in zip(w, c) if a == b)
    if common >= min(len(w), len(c)) * 0.7:
        return 0.55
    if common >= 2 and len(w) >= 3 and len(c) >= 3:
        return 0.35
    return 0.0


def online_sequence_relevance_score(result: OnlineSequenceResult, query: str) -> float:
    words = normalize_search_words(query)
    if not words:
        return 0.0
    haystack_title = " ".join(normalize_search_words(result.title))
    haystack_author = " ".join(normalize_search_words(result.author))
    haystack = f"{haystack_title} {haystack_author}".strip()
    if not haystack:
        return 0.0

    score = 0.0
    title_words = set(haystack_title.split())
    author_words = set(haystack_author.split())
    haystack_words = set(haystack.split())
    all_candidates = list(title_words) + list(author_words)

    query_phrase = " ".join(words)
    if query_phrase and query_phrase in haystack:
        score += 65.0

    for index, word in enumerate(words):
        base_weight = 12.0 if index == 0 else 8.0
        if word in title_words:
            score += base_weight * 1.0
        elif word in author_words:
            score += base_weight * 0.8
        else:
            best_fuzzy = max((_fuzzy_match_score(word, cand) for cand in all_candidates), default=0.0)
            score += base_weight * best_fuzzy * 0.55

    title_raw = result.title.lower()
    query_raw = query.strip().lower()
    if query_raw in title_raw:
        score += 35.0
    elif all(w in title_raw for w in words):
        score += 22.0

    plays_val = parse_online_count(result.plays)
    notes_val = parse_online_count(result.notes)
    score += min(14.0, math.log10(max(1, plays_val)) * 3.5)
    score += min(9.0, math.log10(max(1, notes_val)) * 2.2)

    if plays_val > 10000:
        score += 4.0
    if plays_val > 100000:
        score += 3.5

    title_lower = result.title.lower()
    quality_keywords = ["piano", "cover", "midi", "solo", "instrumental", "arrangement"]
    for kw in quality_keywords:
        if kw in title_lower:
            score += 1.5

    if result.source == "Direct match":
        score += 120.0
    elif result.source.startswith("Online Sequencer"):
        score += 7.0

    return score


def online_sequence_playable_score(result: OnlineSequenceResult, query: str) -> float:
    notes = parse_online_count(result.notes)
    plays = parse_online_count(result.plays)
    score = online_sequence_relevance_score(result, query)
    score += min(18.0, math.log10(max(1, plays)) * 4.5)

    title_words = set(normalize_search_words(result.title))
    title_lower = result.title.lower()
    if "piano" in title_words:
        score += 10.0
    if "cover" in title_words:
        score += 4.0
    if "solo" in title_words:
        score += 3.0
    if "midi" in title_lower:
        score += 2.0

    if notes <= 0:
        score -= 10.0
    else:
        if 200 <= notes <= 4500:
            score += 12.0
        elif 120 <= notes < 200:
            score += 4.0
        elif 4500 < notes <= 9000:
            score += 4.0
        if notes < 80:
            score -= (80 - notes) / 10.0
        if notes > 12000:
            score -= math.log2(notes / 12000.0) * 18.0

    return score


def sort_online_sequence_results(
    results: list[OnlineSequenceResult],
    query: str,
    sort_by: str = "Best playable",
) -> list[OnlineSequenceResult]:
    clean_sort = sort_by if sort_by in ONLINE_SEARCH_SORT_OPTIONS else "Best playable"
    indexed = list(enumerate(results))

    def stable_title(result: OnlineSequenceResult) -> str:
        return result.title.casefold()

    if clean_sort == "Most plays":
        indexed.sort(
            key=lambda item: (
                -parse_online_count(item[1].plays),
                -online_sequence_relevance_score(item[1], query),
                -parse_online_count(item[1].notes),
                stable_title(item[1]),
                item[0],
            )
        )
    elif clean_sort == "Most notes":
        indexed.sort(
            key=lambda item: (
                -parse_online_count(item[1].notes),
                -online_sequence_relevance_score(item[1], query),
                -parse_online_count(item[1].plays),
                stable_title(item[1]),
                item[0],
            )
        )
    elif clean_sort == "Fewest notes":
        indexed.sort(
            key=lambda item: (
                parse_online_count(item[1].notes) if parse_online_count(item[1].notes) else 10**12,
                -online_sequence_relevance_score(item[1], query),
                -parse_online_count(item[1].plays),
                stable_title(item[1]),
                item[0],
            )
        )
    elif clean_sort == "Newest":
        indexed.sort(
            key=lambda item: (
                parse_online_date_key(item[1].updated),
                parse_online_count(item[1].plays),
                online_sequence_relevance_score(item[1], query),
                -item[0],
            ),
            reverse=True,
        )
    elif clean_sort == "Title A-Z":
        indexed.sort(key=lambda item: (stable_title(item[1]), item[1].author.casefold(), item[0]))
    elif clean_sort == "Author A-Z":
        indexed.sort(key=lambda item: (item[1].author.casefold(), stable_title(item[1]), item[0]))
    elif clean_sort == "Best match":
        indexed.sort(
            key=lambda item: (
                -online_sequence_relevance_score(item[1], query),
                -parse_online_count(item[1].plays),
                -parse_online_count(item[1].notes),
                parse_online_date_key(item[1].updated),
                item[0],
            )
        )
    else:
        indexed.sort(
            key=lambda item: (
                -online_sequence_playable_score(item[1], query),
                -online_sequence_relevance_score(item[1], query),
                -parse_online_count(item[1].plays),
                abs(parse_online_count(item[1].notes) - 2600),
                parse_online_date_key(item[1].updated),
                item[0],
            )
        )
    return [result for _index, result in indexed]


def parse_online_sequence_metadata(markup: str, sequence_id: str, source: str = "") -> OnlineSequenceResult:
    text = strip_html_to_text(markup)
    title = ""
    title_match = re.search(r"<title>(.*?)</title>", markup, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(title_match.group(1))
        title = re.sub(r"\s*-\s*Online Sequencer\s*$", "", title, flags=re.IGNORECASE).strip()
    if not title:
        title_match = re.search(r"Link to this sequence:\s*" + re.escape(sequence_id) + r"\s*(.*?)\s*(?:BPM|Download MIDI|OnlineSequencer\.net)", text)
        if title_match:
            title = title_match.group(1).strip()
    if not title:
        title_match = re.search(r"^(.*?)\s+(?:[\d,]+\s+plays|Download MIDI|Link to this sequence)", text)
        if title_match:
            title = title_match.group(1).strip()
    title = title or f"Online Sequencer {sequence_id}"
    title = re.sub(r"\s+", " ", title).strip()

    author = ""
    author_match = re.search(r"(?:created|updated)\s+\d{4}-\d{2}-\d{2}\s+by\s+([^·]+?)(?:\s+Download MIDI|\s+0 Comments|\s+\d+\s+Comments|$)", text)
    if author_match:
        author = author_match.group(1).strip()
    if not author:
        author_match = re.search(re.escape(title) + r"\s+by\s+(.+?)(?:\s+BPM|\s+OnlineSequencer\.net|$)", text)
        if author_match:
            author = author_match.group(1).strip()
    author = re.sub(r"\s+", " ", author).strip()

    plays = ""
    plays_match = re.search(r"([\d,]+)\s+plays", text)
    if plays_match:
        plays = plays_match.group(1)

    notes = ""
    notes_match = re.search(r"([\d,]+)\s+notes", text)
    if notes_match:
        notes = notes_match.group(1)
    if not notes:
        try:
            notes = f"{len(extract_online_sequence_data(markup).notes):,}"
        except Exception:
            notes = ""

    updated = ""
    updated_match = re.search(r"(?:created|updated)\s+(\d{4}-\d{2}-\d{2})", text)
    if updated_match:
        updated = updated_match.group(1)

    return OnlineSequenceResult(
        sequence_id=sequence_id,
        title=title,
        author=author,
        plays=plays,
        notes=notes,
        updated=updated,
        url=online_sequence_url(sequence_id),
        midi_url=online_sequence_midi_url(sequence_id),
        source=source,
    )


def fetch_online_sequence_metadata(sequence_id: str, source: str = "Online Sequencer") -> OnlineSequenceResult:
    markup = http_get_text(online_sequence_url(sequence_id))
    return parse_online_sequence_metadata(markup, sequence_id, source=source)


def online_search_urls(query: str) -> list[str]:
    urls: list[str] = []

    def add_url(value: str) -> None:
        if value not in urls:
            urls.append(value)

    for variant in online_query_variants(query):
        native = quote_plus(variant)
        add_url(f"{ONLINE_SEQUENCER_BASE_URL}/sequences?search={native}&type=3")
        add_url(f"{ONLINE_SEQUENCER_BASE_URL}/sequences?search={native}")
        for web_search_text in (
            f'site:onlinesequencer.net/ "{variant}"',
            f'site:onlinesequencer.net/ {variant} "Online Sequencer"',
            f'site:onlinesequencer.net/ {variant} "Download MIDI"',
        ):
            encoded = quote_plus(web_search_text)
            add_url(f"https://duckduckgo.com/html/?q={encoded}")
            add_url(f"https://www.bing.com/search?q={encoded}")
    return urls


def search_online_sequences(query: str, limit: int = 12, sort_by: str = "Best playable") -> list[OnlineSequenceResult]:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Type a song name, Online Sequencer URL, or sequence ID first.")

    sequence_ids: list[tuple[str, str]] = []
    candidate_limit = min(ONLINE_SEARCH_MAX_CANDIDATES, max(limit * 4, limit))

    def add_sequence_id(sequence_id: str | None, source: str) -> None:
        if not sequence_id:
            return
        if sequence_id not in {existing for existing, _source in sequence_ids}:
            sequence_ids.append((sequence_id, source))

    add_sequence_id(extract_online_sequence_id(clean_query), "Direct match")
    for search_url in online_search_urls(clean_query):
        if len(sequence_ids) >= candidate_limit:
            break
        source = "Online Sequencer" if search_url.startswith(ONLINE_SEQUENCER_BASE_URL) else "Web search"
        try:
            markup = http_get_text(search_url)
        except Exception:
            continue
        for sequence_id in extract_online_sequence_ids_from_html(markup):
            add_sequence_id(sequence_id, source)
            if len(sequence_ids) >= candidate_limit:
                break

    if not sequence_ids:
        raise RuntimeError("No Online Sequencer results found. Try a more specific title, artist, URL, or sequence ID.")

    results: list[OnlineSequenceResult] = []
    result_ids: set[str] = set()

    def fetch_target(target: tuple[str, str]) -> OnlineSequenceResult:
        sequence_id, source = target
        try:
            return fetch_online_sequence_metadata(sequence_id, source=source)
        except Exception:
            return OnlineSequenceResult(
                sequence_id=sequence_id,
                title=f"Online Sequencer {sequence_id}",
                url=online_sequence_url(sequence_id),
                midi_url=online_sequence_midi_url(sequence_id),
                source=source,
            )

    targets = sequence_ids[:candidate_limit]
    max_workers = min(ONLINE_SEARCH_WORKERS, max(1, len(targets)))
    if max_workers <= 1:
        fetched = [fetch_target(target) for target in targets]
    else:
        fetched = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_by_index = {executor.submit(fetch_target, target): index for index, target in enumerate(targets)}
            for future in as_completed(future_by_index):
                fetched.append((future_by_index[future], future.result()))
        fetched = [result for _index, result in sorted(fetched, key=lambda item: item[0])]

    for result in fetched:
        if result.sequence_id not in result_ids:
            result_ids.add(result.sequence_id)
            results.append(result)

    return sort_online_sequence_results(results, clean_query, sort_by=sort_by)[:limit]


def read_proto_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 70:
            raise ValueError("Invalid protobuf varint.")
    raise ValueError("Unexpected end of protobuf varint.")


def read_proto_float32(data: bytes, offset: int) -> tuple[float, int]:
    if offset + 4 > len(data):
        raise ValueError("Unexpected end of protobuf float.")
    return struct.unpack("<f", data[offset : offset + 4])[0], offset + 4


def skip_proto_field(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _value, offset = read_proto_varint(data, offset)
        return offset
    if wire_type == 1:
        return min(len(data), offset + 8)
    if wire_type == 2:
        length, offset = read_proto_varint(data, offset)
        return min(len(data), offset + length)
    if wire_type == 5:
        return min(len(data), offset + 4)
    raise ValueError(f"Unsupported protobuf wire type: {wire_type}")


def iter_proto_fields(data: bytes) -> list[tuple[int, int, bytes]]:
    fields: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset < len(data):
        tag, offset = read_proto_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        value_start = offset
        if wire_type == 0:
            _value, offset = read_proto_varint(data, offset)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            length, offset = read_proto_varint(data, offset)
            value_start = offset
            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            offset = skip_proto_field(data, offset, wire_type)
        if offset > len(data):
            raise ValueError("Protobuf field extends past the end of the data.")
        fields.append((field_number, wire_type, data[value_start:offset]))
    return fields


def parse_online_sequence_settings_proto(data: bytes) -> float:
    bpm = 110.0
    offset = 0
    while offset < len(data):
        tag, offset = read_proto_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 1 and wire_type == 0:
            bpm_value, offset = read_proto_varint(data, offset)
            if bpm_value > 0:
                bpm = float(bpm_value)
        else:
            offset = skip_proto_field(data, offset, wire_type)
    return bpm


def parse_online_sequence_note_proto(data: bytes) -> OnlineSequenceNote | None:
    note_type = 0
    start_beats = 0.0
    length_beats = 0.0
    instrument = 0
    volume = 1.0
    offset = 0
    while offset < len(data):
        tag, offset = read_proto_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 1 and wire_type == 0:
            note_type, offset = read_proto_varint(data, offset)
        elif field_number == 2 and wire_type == 5:
            start_beats, offset = read_proto_float32(data, offset)
        elif field_number == 3 and wire_type == 5:
            length_beats, offset = read_proto_float32(data, offset)
        elif field_number == 4 and wire_type == 0:
            instrument, offset = read_proto_varint(data, offset)
        elif field_number == 5 and wire_type == 5:
            volume, offset = read_proto_float32(data, offset)
        else:
            offset = skip_proto_field(data, offset, wire_type)

    midi_note = int(note_type) + 12
    if midi_note < 0 or midi_note > 127 or length_beats <= 0:
        return None
    return OnlineSequenceNote(
        start_beats=max(0.0, float(start_beats)),
        length_beats=max(0.01, float(length_beats)),
        midi=midi_note,
        instrument=int(instrument),
        volume=max(0.0, min(1.0, float(volume))),
    )


def parse_online_sequence_proto(data: bytes) -> OnlineSequenceData:
    bpm = 110.0
    notes: list[OnlineSequenceNote] = []
    offset = 0
    while offset < len(data):
        tag, offset = read_proto_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type != 2:
            offset = skip_proto_field(data, offset, wire_type)
            continue
        length, offset = read_proto_varint(data, offset)
        payload = data[offset : offset + length]
        offset += length
        if offset > len(data):
            raise ValueError("Online Sequencer protobuf ended unexpectedly.")
        if field_number == 1:
            bpm = parse_online_sequence_settings_proto(payload)
        elif field_number == 2:
            note = parse_online_sequence_note_proto(payload)
            if note is not None:
                notes.append(note)

    if not notes:
        raise ValueError("No notes were found in the Online Sequencer data.")
    notes.sort(key=lambda item: (item.start_beats, item.midi, item.length_beats))
    return OnlineSequenceData(bpm=bpm, notes=tuple(notes))


def extract_online_sequence_data(markup: str) -> OnlineSequenceData:
    match = re.search(r"var\s+data\s*=\s*'([^']+)'", markup)
    if not match:
        match = re.search(r'var\s+data\s*=\s*"([^"]+)"', markup)
    if not match:
        raise ValueError("Could not find embedded Online Sequencer data on the page.")
    encoded = html.unescape(match.group(1)).strip()
    encoded += "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded)
    except Exception as exc:
        raise ValueError("Could not decode Online Sequencer base64 data.") from exc
    return parse_online_sequence_proto(raw)


def write_online_sequence_data_to_midi(sequence_data: OnlineSequenceData, path: Path) -> None:
    if mido is None:
        raise RuntimeError("MIDI support needs mido. Install it with: python -m pip install mido")
    ticks_per_beat = ONLINE_SEQUENCE_TICKS_PER_BEAT
    midi_file = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    midi_file.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(max(1.0, sequence_data.bpm)), time=0))

    events: list[tuple[int, int, str, int, int]] = []
    for note in sequence_data.notes:
        start_tick = max(0, int(round(note.start_beats * ONLINE_SEQUENCE_TICKS_PER_UNIT)))
        end_tick = max(
            start_tick + 1,
            int(round((note.start_beats + note.length_beats) * ONLINE_SEQUENCE_TICKS_PER_UNIT)),
        )
        velocity = max(1, min(127, int(round(max(note.volume, 0.15) * 112))))
        events.append((start_tick, 1, "note_on", note.midi, velocity))
        events.append((end_tick, 0, "note_off", note.midi, 0))

    events.sort(key=lambda item: (item[0], item[1], item[3]))
    last_tick = 0
    for tick, _order, message_type, midi_note, velocity in events:
        delta = max(0, tick - last_tick)
        last_tick = tick
        track.append(mido.Message(message_type, note=midi_note, velocity=velocity, time=delta))
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi_file.save(path)


def read_midi_primary_bpm(path: Path) -> float | None:
    if mido is None:
        return None
    try:
        midi_file = mido.MidiFile(path)
        for track in midi_file.tracks:
            for message in track:
                if message.type == "set_tempo":
                    return float(mido.tempo2bpm(message.tempo))
    except Exception:
        return None
    return None


def download_online_sequence_midi(
    result: OnlineSequenceResult,
    destination_dir: Path = ONLINE_MIDI_DIR,
) -> Path:
    markup = http_get_text(result.url or online_sequence_url(result.sequence_id), timeout=ONLINE_DOWNLOAD_TIMEOUT_SECONDS)
    sequence_data = extract_online_sequence_data(markup)
    destination_dir.mkdir(exist_ok=True)
    filename = f"{sanitize_filename(result.title)}_{result.sequence_id}.mid"
    path = destination_dir / filename
    write_online_sequence_data_to_midi(sequence_data, path)
    return path


def download_direct_midi_url(url: str, destination_dir: Path = ONLINE_MIDI_DIR) -> Path:
    clean_url = url.strip()
    if not clean_url.lower().startswith(("http://", "https://")):
        raise ValueError("Enter a full http:// or https:// MIDI URL.")
    data = http_get_bytes(clean_url, timeout=ONLINE_DOWNLOAD_TIMEOUT_SECONDS, max_bytes=ONLINE_DOWNLOAD_MAX_BYTES)
    if not data.startswith(b"MThd"):
        raise ValueError("That URL did not return a valid MIDI file.")
    destination_dir.mkdir(exist_ok=True)
    parsed = urlparse(clean_url)
    filename = sanitize_filename(Path(parsed.path).name or "direct_midi.mid")
    if not filename.lower().endswith((".mid", ".midi")):
        filename += ".mid"
    path = destination_dir / filename
    if path.exists():
        path = destination_dir / f"{Path(filename).stem}_{int(time.time())}{Path(filename).suffix}"
    path.write_bytes(data)
    return path


def scheduled_actions_to_audio_notes(actions: list[ScheduledAction]) -> list[AudioMidiNote]:
    active: dict[int, list[float]] = {}
    notes: list[AudioMidiNote] = []
    for action in sorted(actions, key=lambda item: (item.seconds, 0 if item.action == "down" else 1)):
        for playable in action.notes:
            if playable.kind != "midi":
                continue
            midi_note = int(playable.value)
            if action.action == "down":
                active.setdefault(midi_note, []).append(action.seconds)
            else:
                starts = active.get(midi_note)
                if not starts:
                    continue
                start = starts.pop(0)
                if action.seconds > start:
                    notes.append(AudioMidiNote(start=start, end=action.seconds, midi=midi_note, velocity=84))
                if not starts:
                    active.pop(midi_note, None)
    return sorted(notes, key=lambda item: (item.start, item.midi, item.end))


def repair_timing_notes(
    notes: list[AudioMidiNote],
    bpm: float,
    quantize_beats: float,
    quantize_strength: float,
    start_offset_seconds: float,
    min_note_beats: float,
    max_note_beats: float,
    gap_ms: float,
    quantize_offset_seconds: float = 0.0,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    beat_seconds = 60.0 / max(1.0, bpm)
    grid_seconds = beat_seconds * max(0.0, quantize_beats)
    quantize_strength = max(0.0, min(1.0, quantize_strength))
    if grid_seconds > 0:
        quantize_offset_seconds = quantize_offset_seconds % grid_seconds
    else:
        quantize_offset_seconds = 0.0
    min_seconds = beat_seconds * max(0.02, min_note_beats)
    max_seconds = beat_seconds * max(min_note_beats, max_note_beats)
    gap_seconds = max(0.0, gap_ms / 1000.0)

    repaired: list[AudioMidiNote] = []
    for note in notes:
        original_start = max(0.0, note.start + start_offset_seconds)
        original_end = max(original_start + 0.001, note.end + start_offset_seconds)
        duration = original_end - original_start
        if grid_seconds > 0:
            snapped_start = round((original_start - quantize_offset_seconds) / grid_seconds) * grid_seconds
            snapped_start += quantize_offset_seconds
            snapped_end = round((original_end - quantize_offset_seconds) / grid_seconds) * grid_seconds
            snapped_end += quantize_offset_seconds
            start = original_start + (snapped_start - original_start) * quantize_strength
            end = original_end + (snapped_end - original_end) * quantize_strength
        else:
            start = original_start
            end = original_end
        start = max(0.0, start)
        duration = max(min_seconds, min(max_seconds, end - start if end > start else duration))
        repaired.append(AudioMidiNote(start=start, end=start + duration, midi=note.midi, velocity=note.velocity))

    by_note: dict[int, list[AudioMidiNote]] = {}
    for note in repaired:
        by_note.setdefault(note.midi, []).append(note)

    trimmed: list[AudioMidiNote] = []
    for midi_note, midi_notes in by_note.items():
        midi_notes.sort(key=lambda item: item.start)
        for index, note in enumerate(midi_notes):
            end = note.end
            if index + 1 < len(midi_notes):
                end = min(end, max(note.start + min_seconds, midi_notes[index + 1].start - gap_seconds))
            if end > note.start:
                trimmed.append(AudioMidiNote(start=note.start, end=end, midi=midi_note, velocity=note.velocity))

    first_start = min(note.start for note in trimmed) if trimmed else 0.0
    return sorted(
        [
            AudioMidiNote(start=max(0.0, note.start - first_start), end=max(0.001, note.end - first_start), midi=note.midi, velocity=note.velocity)
            for note in trimmed
        ],
        key=lambda item: (item.start, item.midi, item.end),
    )


def summarize_midi_actions_for_status(actions: list[ScheduledAction]) -> str:
    notes = scheduled_actions_to_audio_notes(actions)
    if not notes:
        return "Loaded MIDI."
    duration = max((action.seconds for action in actions), default=0.0)
    low_note = midi_to_note_name(min(note.midi for note in notes))
    high_note = midi_to_note_name(max(note.midi for note in notes))
    return (
        f"Loaded {len(notes)} notes | {duration:.1f}s | Range: {low_note}-{high_note} | "
        f"Max held: {max_audio_polyphony(notes)} | Key conflicts: {max_visual_piano_key_conflicts(notes)}"
    )


def estimate_note_art_intro_seconds(notes: list[AudioMidiNote]) -> float:
    if len(notes) < NOTE_ART_MIN_REMOVED_NOTES:
        return 0.0
    duration = max((note.end for note in notes), default=0.0)
    if duration <= 0:
        return 0.0
    scan_end = min(NOTE_ART_SCAN_SECONDS, duration * 0.35, duration)
    if scan_end < NOTE_ART_BIN_SECONDS:
        return 0.0

    bin_count = max(1, int(math.ceil(scan_end / NOTE_ART_BIN_SECONDS)))
    starts_by_bin = [0] * bin_count
    pitches_by_bin: list[set[int]] = [set() for _ in range(bin_count)]
    events: list[tuple[float, int]] = []

    for note in notes:
        if note.start < scan_end:
            index = min(bin_count - 1, max(0, int(note.start / NOTE_ART_BIN_SECONDS)))
            starts_by_bin[index] += 1
            pitches_by_bin[index].add(note.midi)
        if note.start < scan_end and note.end > 0:
            events.append((max(0.0, note.start), 1))
            events.append((min(scan_end, note.end), -1))

    active_by_bin = [0] * bin_count
    events.sort(key=lambda item: (item[0], -item[1]))
    event_index = 0
    active = 0
    for index in range(bin_count):
        center = min(scan_end, index * NOTE_ART_BIN_SECONDS + NOTE_ART_BIN_SECONDS * 0.5)
        while event_index < len(events) and events[event_index][0] <= center:
            active += events[event_index][1]
            event_index += 1
        active_by_bin[index] = active

    spam_bins: list[int] = []
    for index in range(bin_count):
        starts_per_second = starts_by_bin[index] / NOTE_ART_BIN_SECONDS
        if (
            starts_per_second >= NOTE_ART_STARTS_PER_SECOND
            or len(pitches_by_bin[index]) >= NOTE_ART_UNIQUE_PITCHES_PER_BIN
            or active_by_bin[index] >= NOTE_ART_ACTIVE_NOTES
        ):
            spam_bins.append(index)

    if not spam_bins:
        return 0.0
    first_spam_start = spam_bins[0] * NOTE_ART_BIN_SECONDS
    if first_spam_start > NOTE_ART_INITIAL_WINDOW_SECONDS:
        return 0.0

    last_spam = spam_bins[0]
    for index in spam_bins[1:]:
        gap_seconds = (index - last_spam - 1) * NOTE_ART_BIN_SECONDS
        if gap_seconds > NOTE_ART_MAX_SPAM_GAP_SECONDS:
            break
        last_spam = index

    trim_seconds = min(scan_end, (last_spam + 1) * NOTE_ART_BIN_SECONDS)
    starts = sorted(note.start for note in notes if note.start >= trim_seconds)
    if starts and starts[0] - trim_seconds <= 8.0:
        trim_seconds = starts[0]

    removed_notes = sum(1 for note in notes if note.start < trim_seconds)
    if removed_notes < NOTE_ART_MIN_REMOVED_NOTES:
        return 0.0
    remaining_notes = len(notes) - removed_notes
    if remaining_notes < 20:
        return 0.0
    return max(0.0, trim_seconds)


def trim_note_art_intro_actions(actions: list[ScheduledAction]) -> tuple[list[ScheduledAction], float, int]:
    notes = scheduled_actions_to_audio_notes(actions)
    trim_seconds = estimate_note_art_intro_seconds(notes)
    if trim_seconds <= 0:
        return actions, 0.0, 0

    kept: list[AudioMidiNote] = []
    removed_notes = 0
    for note in notes:
        if note.start < trim_seconds:
            removed_notes += 1
            continue
        kept.append(
            AudioMidiNote(
                start=max(0.0, note.start - trim_seconds),
                end=max(0.001, note.end - trim_seconds),
                midi=note.midi,
                velocity=note.velocity,
            )
        )
    if not kept:
        return actions, 0.0, 0
    return audio_notes_to_actions(kept), trim_seconds, removed_notes


def window_scheduled_actions(
    actions: list[ScheduledAction],
    start_seconds: float = 0.0,
    end_seconds: float = 0.0,
) -> list[ScheduledAction]:
    start_seconds = max(0.0, float(start_seconds))
    end_seconds = max(0.0, float(end_seconds))
    if end_seconds > 0 and end_seconds <= start_seconds:
        end_seconds = 0.0
    if start_seconds <= 0 and end_seconds <= 0:
        return coalesce_scheduled_actions(actions)

    has_raw_keys = any(playable.kind == "key" for action in actions for playable in action.notes)
    if has_raw_keys:
        filtered = [
            ScheduledAction(
                seconds=max(0.0, action.seconds - start_seconds),
                action=action.action,
                notes=action.notes,
            )
            for action in actions
            if action.seconds >= start_seconds and (end_seconds <= 0 or action.seconds <= end_seconds)
        ]
        return coalesce_scheduled_actions(filtered)

    notes = scheduled_actions_to_audio_notes(actions)
    clipped: list[AudioMidiNote] = []
    for note in notes:
        if note.end <= start_seconds:
            continue
        if end_seconds > 0 and note.start >= end_seconds:
            continue
        start = max(note.start, start_seconds)
        end = note.end if end_seconds <= 0 else min(note.end, end_seconds)
        if end <= start:
            continue
        clipped.append(
            AudioMidiNote(
                start=max(0.0, start - start_seconds),
                end=max(0.001, end - start_seconds),
                midi=note.midi,
                velocity=note.velocity,
            )
        )
    return audio_notes_to_actions(clipped)


def cleanup_midi_notes_for_playability(
    notes: list[AudioMidiNote],
    bpm: float,
    low_midi: int,
    high_midi: int,
    max_polyphony: int = 4,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    beat_seconds = 60.0 / max(1.0, bpm)
    grid_seconds = max(0.035, beat_seconds * 0.125)
    min_seconds = max(0.035, beat_seconds * 0.08)
    max_seconds = max(min_seconds, beat_seconds * 8.0)
    prepared: list[AudioMidiNote] = []
    for note in notes:
        if note.end <= note.start:
            continue
        midi_note = fit_midi_to_range(note.midi, low_midi, high_midi)
        duration = max(min_seconds, min(max_seconds, note.end - note.start))
        prepared.append(AudioMidiNote(note.start, note.start + duration, midi_note, note.velocity))
    prepared = merge_audio_notes_by_pitch(
        prepared,
        merge_gap_seconds=max(0.015, beat_seconds * 0.02),
        retrigger_gap_seconds=max(0.045, beat_seconds * 0.16),
    )
    prepared = enforce_audio_polyphony(
        prepared,
        max_polyphony=max(1, min(8, int(max_polyphony))),
        beat_seconds=beat_seconds,
        grid_seconds=grid_seconds,
        low_midi=low_midi,
        high_midi=high_midi,
        melody_boost=1.0,
        keep_bass=True,
    )
    prepared = enforce_visual_piano_keyboard_playability(
        prepared,
        beat_seconds=beat_seconds,
        grid_seconds=grid_seconds,
        low_midi=low_midi,
        high_midi=high_midi,
        keep_bass=True,
    )
    if not prepared:
        return []
    first_start = min(note.start for note in prepared)
    return sorted(
        [
            AudioMidiNote(
                start=max(0.0, note.start - first_start),
                end=max(0.001, note.end - first_start),
                midi=note.midi,
                velocity=note.velocity,
            )
            for note in prepared
        ],
        key=lambda item: (item.start, item.midi, item.end),
    )


def cleanup_midi_actions_for_playability(
    actions: list[ScheduledAction],
    bpm: float,
    low_midi: int,
    high_midi: int,
    max_polyphony: int = 4,
) -> tuple[list[ScheduledAction], int, int]:
    notes = scheduled_actions_to_audio_notes(actions)
    cleaned = cleanup_midi_notes_for_playability(notes, bpm, low_midi, high_midi, max_polyphony=max_polyphony)
    if notes and not cleaned:
        return actions, len(notes), len(notes)
    return audio_notes_to_actions(cleaned), len(notes), len(cleaned)


def prepare_online_midi_load(
    result: OnlineSequenceResult,
    run_timing_repair: bool,
    repair_settings: dict[str, float | bool],
    trim_note_art_intro: bool = True,
    cleanup_playability: bool = False,
) -> PreparedOnlineMidi:
    path = download_online_sequence_midi(result)
    actions = load_midi_actions(str(path))
    detected_bpm = read_midi_primary_bpm(path)
    repaired = False
    trimmed_seconds = 0.0
    trimmed_notes = 0
    cleaned_before = 0
    cleaned_after = 0

    if trim_note_art_intro:
        actions, trimmed_seconds, trimmed_notes = trim_note_art_intro_actions(actions)

    if cleanup_playability:
        cleanup_bpm = max(1.0, float(detected_bpm if detected_bpm is not None else repair_settings.get("bpm", 120.0)))
        actions, cleaned_before, cleaned_after = cleanup_midi_actions_for_playability(
            actions,
            bpm=cleanup_bpm,
            low_midi=int(repair_settings.get("low_midi", note_name_to_midi("C2"))),
            high_midi=int(repair_settings.get("high_midi", note_name_to_midi("C7"))),
            max_polyphony=4,
        )

    if run_timing_repair:
        notes = scheduled_actions_to_audio_notes(actions)
        if notes:
            bpm = max(1.0, float(detected_bpm if detected_bpm is not None else repair_settings.get("bpm", 120.0)))
            quantize_beats = max(0.0, float(repair_settings.get("quantize_beats", 0.25)))
            quantize_offset_seconds = 0.0
            if bool(repair_settings.get("auto_offset", False)) and quantize_beats > 0:
                quantize_offset_seconds, _offset_confidence = estimate_note_grid_offset(notes, bpm, quantize_beats)
            repaired_notes = repair_timing_notes(
                notes=notes,
                bpm=bpm,
                quantize_beats=quantize_beats,
                quantize_strength=max(0.0, min(1.0, float(repair_settings.get("quantize_strength", 0.55)))),
                start_offset_seconds=float(repair_settings.get("offset_seconds", 0.0)),
                min_note_beats=max(0.02, float(repair_settings.get("min_note_beats", 0.1))),
                max_note_beats=max(0.1, float(repair_settings.get("max_note_beats", 4.0))),
                gap_ms=max(0.0, float(repair_settings.get("gap_ms", 8.0))),
                quantize_offset_seconds=quantize_offset_seconds,
            )
            actions = audio_notes_to_actions(repaired_notes)
            repaired = True

    summary = summarize_midi_actions_for_status(actions)
    if trimmed_seconds > 0:
        summary = f"{summary} | Skipped intro: {trimmed_seconds:.1f}s ({trimmed_notes} notes)"
    if cleaned_before and cleaned_after:
        summary = f"{summary} | Cleanup: {cleaned_before}->{cleaned_after} notes"
    return PreparedOnlineMidi(
        path=path,
        result=result,
        actions=tuple(actions),
        bpm=detected_bpm,
        summary=summary,
        repaired=repaired,
        trimmed_seconds=trimmed_seconds,
        trimmed_notes=trimmed_notes,
        cleaned_before_notes=cleaned_before,
        cleaned_after_notes=cleaned_after,
    )


def prepare_direct_midi_load(
    url: str,
    trim_note_art_intro: bool = True,
    cleanup_playability: bool = False,
    cleanup_settings: dict[str, float | bool] | None = None,
) -> PreparedOnlineMidi:
    path = download_direct_midi_url(url)
    actions = load_midi_actions(str(path))
    detected_bpm = read_midi_primary_bpm(path)
    trimmed_seconds = 0.0
    trimmed_notes = 0
    cleaned_before = 0
    cleaned_after = 0
    if trim_note_art_intro:
        actions, trimmed_seconds, trimmed_notes = trim_note_art_intro_actions(actions)
    if cleanup_playability:
        cleanup_settings = cleanup_settings or {}
        cleanup_bpm = max(1.0, float(detected_bpm if detected_bpm is not None else cleanup_settings.get("bpm", 120.0)))
        actions, cleaned_before, cleaned_after = cleanup_midi_actions_for_playability(
            actions,
            bpm=cleanup_bpm,
            low_midi=int(cleanup_settings.get("low_midi", note_name_to_midi("C2"))),
            high_midi=int(cleanup_settings.get("high_midi", note_name_to_midi("C7"))),
            max_polyphony=4,
        )
    result = OnlineSequenceResult(
        sequence_id="direct-midi",
        title=Path(path).stem,
        url=url,
        midi_url=url,
        source="Direct MIDI URL",
    )
    summary = summarize_midi_actions_for_status(actions)
    if trimmed_seconds > 0:
        summary = f"{summary} | Skipped intro: {trimmed_seconds:.1f}s ({trimmed_notes} notes)"
    if cleaned_before and cleaned_after:
        summary = f"{summary} | Cleanup: {cleaned_before}->{cleaned_after} notes"
    return PreparedOnlineMidi(
        path=path,
        result=result,
        actions=tuple(actions),
        bpm=detected_bpm,
        summary=summary,
        trimmed_seconds=trimmed_seconds,
        trimmed_notes=trimmed_notes,
        cleaned_before_notes=cleaned_before,
        cleaned_after_notes=cleaned_after,
    )


def require_audio_dependencies() -> None:
    global np, librosa
    if np is None:
        try:
            np = importlib.import_module("numpy")
        except Exception:
            np = None
    if librosa is None:
        try:
            librosa = importlib.import_module("librosa")
        except Exception:
            librosa = None

    missing: list[str] = []
    if np is None:
        missing.append("numpy")
    if librosa is None:
        missing.append("librosa")
    if missing:
        raise RuntimeError(
            "Audio to MIDI needs extra packages. Install them with:\n"
            "python -m pip install numpy librosa soundfile\n\n"
            f"Missing: {', '.join(missing)}"
        )


def require_basic_pitch() -> object:
    try:
        inference = importlib.import_module("basic_pitch.inference")
    except Exception as exc:
        raise RuntimeError(
            "The high-quality Basic Pitch engine is not installed.\n\n"
            "Install it with:\n"
            "python -m pip install basic-pitch setuptools\n\n"
            "Then restart JJS Piano Studio."
        ) from exc
    if not hasattr(inference, "predict"):
        raise RuntimeError("basic-pitch is installed, but its predict() API was not found.")
    return inference


def midi_to_hz(midi_note: int) -> float:
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def hz_to_midi_float(hz: float) -> float:
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def clamp_midi_note(midi_note: int, low_midi: int, high_midi: int) -> int:
    return min(high_midi, max(low_midi, midi_note))


def quantize_seconds(value: float, grid_seconds: float) -> float:
    if grid_seconds <= 0:
        return max(0.0, value)
    return max(0.0, round(value / grid_seconds) * grid_seconds)


def soft_quantize_seconds(
    value: float,
    grid_seconds: float,
    strength: float,
    offset_seconds: float = 0.0,
) -> float:
    if grid_seconds <= 0 or strength <= 0:
        return max(0.0, value)
    offset_seconds = offset_seconds % grid_seconds
    snapped = round((value - offset_seconds) / grid_seconds) * grid_seconds + offset_seconds
    strength = max(0.0, min(1.0, strength))
    return max(0.0, value + (snapped - value) * strength)


def nearest_grid_time_seconds(value: float, grid_times: list[float]) -> float:
    if not grid_times:
        return max(0.0, value)
    index = bisect.bisect_left(grid_times, value)
    candidates: list[float] = []
    if index < len(grid_times):
        candidates.append(grid_times[index])
    if index > 0:
        candidates.append(grid_times[index - 1])
    if not candidates:
        return max(0.0, value)
    return max(0.0, min(candidates, key=lambda candidate: abs(candidate - value)))


def soft_quantize_to_grid_times(value: float, grid_times: list[float], strength: float) -> float:
    if not grid_times or strength <= 0:
        return max(0.0, value)
    strength = max(0.0, min(1.0, strength))
    snapped = nearest_grid_time_seconds(value, grid_times)
    return max(0.0, value + (snapped - value) * strength)


def timing_bucket_index(value: float, bucket_seconds: float, grid_times: list[float] | None = None) -> int:
    if grid_times:
        index = bisect.bisect_left(grid_times, value)
        if index <= 0:
            return 0
        if index >= len(grid_times):
            return len(grid_times) - 1
        previous_distance = abs(value - grid_times[index - 1])
        next_distance = abs(grid_times[index] - value)
        return index - 1 if previous_distance <= next_distance else index
    return int(round(value / max(0.001, bucket_seconds)))


def normalize_timing_grid_times(grid_times: list[float] | None) -> list[float]:
    if not grid_times:
        return []
    normalized: list[float] = []
    previous = -1.0
    for item in sorted(float(value) for value in grid_times if math.isfinite(float(value)) and float(value) >= 0.0):
        if item - previous < 0.006:
            continue
        normalized.append(item)
        previous = item
    return normalized


def grid_distance_seconds(value: float, grid_seconds: float) -> float:
    if grid_seconds <= 0:
        return 0.0
    phase = value % grid_seconds
    return min(phase, grid_seconds - phase)


def estimate_note_grid_offset(
    notes: list[AudioMidiNote],
    bpm: float,
    grid_beats: float,
) -> tuple[float, float]:
    if not notes or grid_beats <= 0:
        return 0.0, 0.0
    beat_seconds = 60.0 / max(1.0, bpm)
    grid_seconds = beat_seconds * grid_beats
    if grid_seconds <= 0:
        return 0.0, 0.0

    weighted_phases: list[tuple[float, float]] = []
    for note in notes:
        if note.end <= note.start:
            continue
        duration = max(0.01, note.end - note.start)
        duration_beats = duration / max(0.001, beat_seconds)
        weight = max(0.25, min(1.75, note.velocity / 88.0))
        weight *= max(0.35, min(1.35, math.sqrt(duration_beats)))
        weighted_phases.append((note.start % grid_seconds, weight))
    if len(weighted_phases) < 2:
        return 0.0, 0.0

    best_offset = 0.0
    best_distance = float("inf")
    total_weight = sum(weight for _, weight in weighted_phases)
    for candidate, _ in weighted_phases:
        distance = 0.0
        for phase, weight in weighted_phases:
            distance += min(abs(phase - candidate), grid_seconds - abs(phase - candidate)) * weight
        if distance < best_distance:
            best_distance = distance
            best_offset = candidate

    average_distance = best_distance / max(0.001, total_weight)
    confidence = max(0.0, min(1.0, 1.0 - average_distance / max(0.001, grid_seconds * 0.5)))
    if confidence < 0.18:
        return 0.0, confidence
    return best_offset, confidence


def normalize_audio(y: object) -> object:
    if np is None:
        return y
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0:
        return y / peak
    return y


def median_smooth_optional_notes(values: list[int | None], radius: int = 2) -> list[int | None]:
    if radius <= 0 or not values:
        return values
    smoothed: list[int | None] = []
    for index, value in enumerate(values):
        if value is None:
            smoothed.append(None)
            continue
        window = [
            item
            for item in values[max(0, index - radius) : min(len(values), index + radius + 1)]
            if item is not None
        ]
        if not window:
            smoothed.append(value)
            continue
        window.sort()
        smoothed.append(window[len(window) // 2])
    return smoothed


def remove_short_none_gaps(values: list[int | None], max_gap_frames: int = 2) -> list[int | None]:
    filled = list(values)
    index = 0
    while index < len(filled):
        if filled[index] is not None:
            index += 1
            continue
        start = index
        while index < len(filled) and filled[index] is None:
            index += 1
        end = index
        if start == 0 or end >= len(filled):
            continue
        left = filled[start - 1]
        right = filled[end]
        if left is not None and left == right and end - start <= max_gap_frames:
            for gap_index in range(start, end):
                filled[gap_index] = left
    return filled


def remove_short_note_runs(values: list[int | None], min_frames: int = 2) -> list[int | None]:
    if min_frames <= 1:
        return values
    cleaned = list(values)
    index = 0
    while index < len(cleaned):
        value = cleaned[index]
        start = index
        while index < len(cleaned) and cleaned[index] == value:
            index += 1
        end = index
        if value is not None and end - start < min_frames:
            replacement: int | None = None
            if start > 0 and end < len(cleaned) and cleaned[start - 1] == cleaned[end]:
                replacement = cleaned[start - 1]
            for run_index in range(start, end):
                cleaned[run_index] = replacement
    return cleaned


def audio_notes_to_dicts(notes: list[AudioMidiNote]) -> list[dict[str, float | int]]:
    return [
        {"start": note.start, "end": note.end, "midi": note.midi, "velocity": note.velocity}
        for note in notes
    ]


def audio_notes_from_dicts(items: list[dict[str, object]]) -> list[AudioMidiNote]:
    notes: list[AudioMidiNote] = []
    for item in items:
        notes.append(
            AudioMidiNote(
                start=float(item["start"]),
                end=float(item["end"]),
                midi=int(item["midi"]),
                velocity=int(item.get("velocity", 84)),
            )
        )
    return notes


def load_audio_mono(path: str, sample_rate: int, trim_silence: bool) -> tuple[object, int]:
    require_audio_dependencies()
    y, sr = librosa.load(path, sr=sample_rate, mono=True)
    y = normalize_audio(y)
    if trim_silence and len(y):
        y, _ = librosa.effects.trim(y, top_db=36)
    return y, int(sr)


def estimate_local_beat_grid(
    path: str,
    sample_rate: int,
    trim_silence: bool,
    grid_beats: float,
) -> tuple[list[float], float, float]:
    require_audio_dependencies()
    if grid_beats <= 0:
        return [], 0.0, 0.0
    y, sr = load_audio_mono(path, sample_rate, trim_silence)
    if not len(y):
        return [], 0.0, 0.0

    hop_length = 512
    onset_envelope = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
    tempo, beat_frames = librosa.beat.beat_track(
        y=y,
        sr=sr,
        onset_envelope=onset_envelope,
        hop_length=hop_length,
        trim=False,
    )
    if hasattr(tempo, "__len__"):
        tempo = float(tempo[0])
    tempo = fold_bpm_to_playable_range(float(tempo))
    beat_times = [float(item) for item in librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)]
    beat_times = [item for item in beat_times if math.isfinite(item) and item >= 0.0]
    if len(beat_times) < 4:
        return [], tempo, 0.0

    intervals = [right - left for left, right in zip(beat_times, beat_times[1:]) if 0.12 <= right - left <= 2.5]
    if len(intervals) < 3:
        return [], tempo, 0.0
    median_interval = float(np.median(intervals))
    if median_interval <= 0:
        return [], tempo, 0.0
    interval_spread = float(np.median([abs(item - median_interval) for item in intervals]))
    stability = max(0.0, min(1.0, 1.0 - interval_spread / max(0.001, median_interval * 0.45)))
    duration = len(y) / float(sr)
    beat_density = min(1.0, len(beat_times) / max(1.0, duration * 0.85))
    confidence = max(0.0, min(1.0, stability * 0.75 + beat_density * 0.25))

    subdivisions = max(1, min(16, int(round(1.0 / max(0.03125, grid_beats)))))
    grid_times: list[float] = []
    for left, right in zip(beat_times, beat_times[1:]):
        if right <= left:
            continue
        for step in range(subdivisions):
            grid_times.append(left + (right - left) * step / subdivisions)
    grid_times.append(beat_times[-1])

    first_interval = intervals[0]
    for step in range(1, subdivisions + 1):
        grid_times.append(max(0.0, beat_times[0] - first_interval * step / subdivisions))
    last_interval = intervals[-1]
    for step in range(1, subdivisions + 3):
        grid_times.append(beat_times[-1] + last_interval * step / subdivisions)

    return normalize_timing_grid_times(grid_times), tempo, confidence


def write_temp_audio_file(y: object, sample_rate: int) -> str:
    try:
        soundfile = importlib.import_module("soundfile")
    except Exception as exc:
        raise RuntimeError(
            "Audio preprocessing needs soundfile. Install it with: python -m pip install soundfile"
        ) from exc
    handle = tempfile.NamedTemporaryFile(prefix="jjs_basic_pitch_", suffix=".wav", delete=False)
    temp_path = handle.name
    handle.close()
    soundfile.write(temp_path, y, sample_rate)
    return temp_path


def postprocess_audio_notes(
    notes: list[AudioMidiNote],
    bpm: float,
    grid_beats: float,
    min_note_beats: float,
    low_midi: int,
    high_midi: int,
    merge_gap_seconds: float = 0.06,
    onset_times: list[float] | None = None,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    beat_seconds = 60.0 / max(1.0, bpm)
    grid_seconds = max(0.005, beat_seconds * max(0.0, grid_beats))
    min_note_seconds = beat_seconds * max(0.02, min_note_beats)

    split_notes: list[AudioMidiNote] = []
    onset_times = onset_times or []
    for note in notes:
        interior_onsets = [
            onset
            for onset in onset_times
            if note.start + min_note_seconds <= onset <= note.end - min_note_seconds
        ]
        if not interior_onsets:
            split_notes.append(note)
            continue
        cursor = note.start
        for onset in interior_onsets:
            split_notes.append(AudioMidiNote(cursor, onset, note.midi, note.velocity))
            cursor = onset
        split_notes.append(AudioMidiNote(cursor, note.end, note.midi, note.velocity))

    cleaned: list[AudioMidiNote] = []
    for note in split_notes:
        start = quantize_seconds(note.start, grid_seconds) if grid_beats > 0 else max(0.0, note.start)
        end = quantize_seconds(note.end, grid_seconds) if grid_beats > 0 else max(0.0, note.end)
        if end <= start:
            end = start + max(grid_seconds, min_note_seconds)
        midi_note = clamp_midi_note(note.midi, low_midi, high_midi)
        if end - start >= min_note_seconds:
            cleaned.append(AudioMidiNote(start=start, end=end, midi=midi_note, velocity=note.velocity))

    cleaned.sort(key=lambda item: (item.midi, item.start, item.end))
    merged: list[AudioMidiNote] = []
    for note in cleaned:
        if merged and merged[-1].midi == note.midi and note.start - merged[-1].end <= merge_gap_seconds:
            previous = merged[-1]
            merged[-1] = AudioMidiNote(
                start=previous.start,
                end=max(previous.end, note.end),
                midi=previous.midi,
                velocity=max(previous.velocity, note.velocity),
            )
        else:
            merged.append(note)

    return sorted(merged, key=lambda item: (item.start, item.midi, item.end))


def fit_midi_to_range(midi_note: int, low_midi: int, high_midi: int) -> int:
    if low_midi <= midi_note <= high_midi:
        return midi_note
    octave_candidates = [
        midi_note + 12 * octave_shift
        for octave_shift in range(-8, 9)
        if low_midi <= midi_note + 12 * octave_shift <= high_midi
    ]
    if octave_candidates:
        return min(octave_candidates, key=lambda candidate: abs(candidate - midi_note))
    return clamp_midi_note(midi_note, low_midi, high_midi)


def merge_audio_notes_by_pitch(
    notes: list[AudioMidiNote],
    merge_gap_seconds: float,
    retrigger_gap_seconds: float = 0.0,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    normalized = sorted(notes, key=lambda item: (item.midi, item.start, item.end))
    merged: list[AudioMidiNote] = []
    for note in normalized:
        if note.end <= note.start:
            continue
        if (
            retrigger_gap_seconds > 0
            and merged
            and merged[-1].midi == note.midi
            and note.start - merged[-1].start >= retrigger_gap_seconds
        ):
            previous = merged[-1]
            if previous.end > note.start - merge_gap_seconds:
                trimmed_end = max(previous.start + 0.01, note.start - merge_gap_seconds)
                merged[-1] = AudioMidiNote(previous.start, trimmed_end, previous.midi, previous.velocity)
            merged.append(note)
            continue
        if merged and merged[-1].midi == note.midi and note.start <= merged[-1].end + merge_gap_seconds:
            previous = merged[-1]
            merged[-1] = AudioMidiNote(
                start=min(previous.start, note.start),
                end=max(previous.end, note.end),
                midi=previous.midi,
                velocity=max(previous.velocity, note.velocity),
            )
            continue
        merged.append(note)
    return sorted(merged, key=lambda item: (item.start, item.midi, item.end))


def remove_sparse_audio_blips(notes: list[AudioMidiNote], beat_seconds: float, style: str) -> list[AudioMidiNote]:
    if not notes or "dense" in style.lower() or "raw" in style.lower():
        return notes
    by_midi_weight: dict[int, float] = {}
    for note in notes:
        duration = max(0.0, note.end - note.start)
        by_midi_weight[note.midi] = by_midi_weight.get(note.midi, 0.0) + duration * max(0.25, note.velocity / 100.0)

    minimum_total = beat_seconds * (0.22 if "rich" in style.lower() else 0.36)
    cleaned: list[AudioMidiNote] = []
    for note in notes:
        duration = max(0.0, note.end - note.start)
        total_weight = by_midi_weight.get(note.midi, 0.0)
        if duration < beat_seconds * 0.07 and note.velocity < 96:
            continue
        if total_weight < minimum_total and duration < beat_seconds * 0.20 and note.velocity < 88:
            continue
        cleaned.append(note)
    return cleaned


def _audio_note_overlap_ratio(first: AudioMidiNote, second: AudioMidiNote) -> float:
    overlap = min(first.end, second.end) - max(first.start, second.start)
    if overlap <= 0:
        return 0.0
    shorter = min(first.end - first.start, second.end - second.start)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _has_melodic_neighbor_context(
    note: AudioMidiNote,
    notes: list[AudioMidiNote],
    beat_seconds: float,
    same_bucket_seconds: float,
) -> bool:
    previous_context = False
    next_context = False
    for other in notes:
        if other is note:
            continue
        start_gap = other.start - note.start
        if abs(start_gap) <= same_bucket_seconds * 1.2:
            continue
        if abs(start_gap) > beat_seconds * 1.6:
            continue
        if abs(other.midi - note.midi) > 5:
            continue
        if other.velocity < note.velocity - 20:
            continue
        if start_gap < 0:
            previous_context = True
        else:
            next_context = True
    if previous_context and next_context:
        return True
    duration_beats = (note.end - note.start) / max(0.001, beat_seconds)
    if (previous_context or next_context) and duration_beats >= 0.25 and note.velocity >= 85:
        return True
    return (previous_context or next_context) and duration_beats >= 0.55 and note.velocity >= 92


def suppress_harmonic_ghost_notes(
    notes: list[AudioMidiNote],
    beat_seconds: float,
    style: str,
) -> list[AudioMidiNote]:
    clean_style = style.strip().lower()
    if not notes or "dense" in clean_style or "raw" in clean_style:
        return notes

    same_bucket_seconds = max(0.035, beat_seconds * 0.10)
    if "rich" in clean_style:
        velocity_margin = -2
    elif "lead" in clean_style or "melody" in clean_style:
        velocity_margin = 10
    else:
        velocity_margin = 6

    kept = [True] * len(notes)
    ordered = sorted(enumerate(notes), key=lambda item: (item[1].start, item[1].midi))
    harmonic_intervals = {19, 24, 28, 31, 36}

    for high_position, high in ordered:
        if not kept[high_position]:
            continue
        high_duration = max(0.001, high.end - high.start)
        for low_position, low in ordered:
            if high_position == low_position or not kept[low_position]:
                continue
            interval = high.midi - low.midi
            if interval <= 0:
                continue
            if abs(high.start - low.start) > same_bucket_seconds:
                continue
            overlap_ratio = _audio_note_overlap_ratio(high, low)
            if overlap_ratio < 0.36:
                continue
            low_duration = max(0.001, low.end - low.start)

            parallel_octave_shadow = (
                interval in {12, 24, 36}
                and low.midi >= 64
                and high.velocity <= low.velocity + 4
                and high_duration <= low_duration * 1.50
            )
            octave_shadow = interval == 12 and high.velocity <= low.velocity - 16 and high_duration <= low_duration * 1.18
            overtone_shadow = (
                interval in harmonic_intervals
                and high.velocity <= low.velocity + velocity_margin
                and high_duration <= low_duration * 1.65
            )
            if not parallel_octave_shadow and not octave_shadow and not overtone_shadow:
                continue
            if not parallel_octave_shadow and _has_melodic_neighbor_context(high, notes, beat_seconds, same_bucket_seconds):
                continue
            kept[high_position] = False
            break

    return [note for index, note in enumerate(notes) if kept[index]]


MAJOR_KEY_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_KEY_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
MAJOR_SCALE_PCS = {0, 2, 4, 5, 7, 9, 11}
MINOR_SCALE_PCS = {0, 2, 3, 5, 7, 8, 10}


def estimate_audio_key(notes: list[AudioMidiNote]) -> tuple[int, str, float] | None:
    if not notes:
        return None
    histogram = [0.0] * 12
    for note in notes:
        duration = max(0.0, note.end - note.start)
        if duration <= 0:
            continue
        histogram[note.midi % 12] += duration * max(0.25, note.velocity / 100.0)
    total = sum(histogram)
    if total <= 0:
        return None

    scored: list[tuple[float, int, str]] = []
    for root in range(12):
        major_score = 0.0
        minor_score = 0.0
        for pitch_class, weight in enumerate(histogram):
            major_score += weight * MAJOR_KEY_PROFILE[(pitch_class - root) % 12]
            minor_score += weight * MINOR_KEY_PROFILE[(pitch_class - root) % 12]
        scored.append((major_score, root, "major"))
        scored.append((minor_score, root, "minor"))
    scored.sort(reverse=True)
    best_score, root, mode = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    confidence = max(0.0, (best_score - runner_up) / max(1.0, total))
    return root, mode, confidence


def audio_key_label(key_info: tuple[int, str, float] | None) -> str | None:
    if key_info is None:
        return None
    root, mode, confidence = key_info
    return f"{NOTE_NAMES[root]} {mode} ({confidence:.2f})"


def nearest_in_scale_midi(midi_note: int, scale_pitch_classes: set[int], low_midi: int, high_midi: int) -> int:
    if midi_note % 12 in scale_pitch_classes:
        return midi_note
    candidates: list[tuple[int, int]] = []
    for offset in (-1, 1, -2, 2):
        candidate = midi_note + offset
        if low_midi <= candidate <= high_midi and candidate % 12 in scale_pitch_classes:
            candidates.append((abs(offset), candidate))
    if not candidates:
        return midi_note
    candidates.sort(key=lambda item: (item[0], abs(item[1] - midi_note)))
    return candidates[0][1]


def cleanup_notes_to_estimated_key(
    notes: list[AudioMidiNote],
    bpm: float,
    low_midi: int,
    high_midi: int,
    style: str,
    enabled: bool,
) -> tuple[list[AudioMidiNote], str | None]:
    if not enabled or not notes or "raw" in style.lower():
        return notes, None
    key_info = estimate_audio_key(notes)
    if key_info is None:
        return notes, None
    root, mode, confidence = key_info
    if confidence < 0.015:
        return notes, audio_key_label(key_info)

    scale_template = MAJOR_SCALE_PCS if mode == "major" else MINOR_SCALE_PCS
    scale_pitch_classes = {(root + pitch_class) % 12 for pitch_class in scale_template}
    beat_seconds = 60.0 / max(1.0, bpm)
    cleaned: list[AudioMidiNote] = []
    for note in notes:
        if note.midi % 12 in scale_pitch_classes:
            cleaned.append(note)
            continue
        duration = max(0.0, note.end - note.start)
        strong_note = note.velocity >= 102 and duration >= beat_seconds * 0.30
        strong_bass = note.midi <= low_midi + 14 and _is_strong_beat(note.start, beat_seconds)
        if strong_note or strong_bass:
            cleaned.append(note)
            continue
        fixed_midi = nearest_in_scale_midi(note.midi, scale_pitch_classes, low_midi, high_midi)
        cleaned.append(AudioMidiNote(note.start, note.end, fixed_midi, note.velocity))

    return merge_audio_notes_by_pitch(cleaned, merge_gap_seconds=max(0.02, beat_seconds * 0.025)), audio_key_label(key_info)


def blend_audio_note_passes(
    note_passes: list[list[AudioMidiNote]],
    bpm: float,
    low_midi: int,
    high_midi: int,
) -> list[AudioMidiNote]:
    combined: list[tuple[int, AudioMidiNote]] = []
    for pass_index, notes in enumerate(note_passes):
        for note in notes:
            if note.end <= note.start:
                continue
            combined.append(
                (
                    pass_index,
                    AudioMidiNote(
                        start=max(0.0, note.start),
                        end=max(note.start + 0.01, note.end),
                        midi=fit_midi_to_range(note.midi, low_midi, high_midi),
                        velocity=max(24, min(127, int(note.velocity))),
                    ),
                )
            )
    if not combined:
        return []

    beat_seconds = 60.0 / max(1.0, bpm)
    bucket_seconds = max(0.045, beat_seconds * 0.08)
    support: list[int] = [0] * len(combined)
    for index, (pass_index, note) in enumerate(combined):
        for other_index, (other_pass, other) in enumerate(combined):
            if other_index == index or other_pass == pass_index or other.midi != note.midi:
                continue
            overlap = min(note.end, other.end) - max(note.start, other.start)
            near_start = abs(note.start - other.start) <= bucket_seconds * 1.5
            if overlap > min(note.end - note.start, other.end - other.start) * 0.20 or near_start:
                support[index] += 1

    buckets: dict[tuple[int, int], AudioMidiNote] = {}
    for index, (_, note) in enumerate(combined):
        key = (note.midi, int(round(note.start / bucket_seconds)))
        velocity = max(24, min(127, note.velocity + support[index] * 12))
        boosted = AudioMidiNote(note.start, note.end, note.midi, velocity)
        previous = buckets.get(key)
        if previous is None:
            buckets[key] = boosted
            continue
        buckets[key] = AudioMidiNote(
            start=min(previous.start, boosted.start),
            end=max(previous.end, boosted.end),
            midi=boosted.midi,
            velocity=max(previous.velocity, boosted.velocity),
        )

    return merge_audio_notes_by_pitch(list(buckets.values()), merge_gap_seconds=max(0.025, beat_seconds * 0.035))


def _arrangement_style_defaults(style: str) -> dict[str, float | bool]:
    clean = style.strip().lower()
    if "lead" in clean or "melody" in clean:
        return {
            "max_polyphony": 1,
            "velocity_window": 52.0,
            "melody_boost": 1.25,
            "keep_bass": False,
            "cluster_spacing": 3,
            "duration_weight": 4.0,
            "adaptive_texture": True,
        }
    if "rich" in clean:
        return {
            "max_polyphony": 6,
            "velocity_window": 54.0,
            "melody_boost": 0.85,
            "keep_bass": True,
            "cluster_spacing": 1,
            "duration_weight": 8.0,
            "adaptive_texture": True,
        }
    if "dense" in clean or "raw" in clean:
        return {
            "max_polyphony": 8,
            "velocity_window": 70.0,
            "melody_boost": 0.55,
            "keep_bass": True,
            "cluster_spacing": 0,
            "duration_weight": 6.0,
            "adaptive_texture": False,
        }
    return {
        "max_polyphony": 4,
        "velocity_window": 30.0,
        "melody_boost": 0.95,
        "keep_bass": True,
        "cluster_spacing": 2,
        "duration_weight": 7.0,
        "adaptive_texture": True,
    }


def _is_strong_beat(time_seconds: float, beat_seconds: float) -> bool:
    if beat_seconds <= 0:
        return False
    beat_position = time_seconds / beat_seconds
    phase = beat_position - math.floor(beat_position)
    return phase <= 0.11 or phase >= 0.89 or abs(phase - 0.5) <= 0.08


def _audio_note_priority(
    note: AudioMidiNote,
    bucket_high: int,
    bucket_low: int,
    beat_seconds: float,
    low_midi: int,
    high_midi: int,
    melody_boost: float,
    keep_bass: bool,
    duration_weight: float,
) -> float:
    duration_beats = max(0.0, (note.end - note.start) / max(0.001, beat_seconds))
    middle = (low_midi + high_midi) / 2.0
    score = float(note.velocity)
    score += min(24.0, duration_beats * duration_weight)
    score += max(-8.0, min(8.0, (note.midi - middle) * 0.18))
    if note.midi == bucket_high:
        score += 18.0 * melody_boost
    if keep_bass and note.midi == bucket_low and bucket_high - bucket_low >= 12:
        score += 18.0 if _is_strong_beat(note.start, beat_seconds) else 5.0
    if note.midi <= low_midi + 7 and not _is_strong_beat(note.start, beat_seconds):
        score -= 10.0
    return score


def adaptive_polyphony_for_bucket(
    bucket_start: float,
    melody_start_history: list[float],
    max_polyphony: int,
    beat_seconds: float,
    style: str,
    strong_beat: bool,
) -> int:
    if max_polyphony <= 1:
        return 1
    clean_style = style.strip().lower()
    if "dense" in clean_style or "raw" in clean_style:
        return max_polyphony
    recent_starts = [start for start in melody_start_history[-4:] if bucket_start - start <= beat_seconds * 1.05]
    if len(recent_starts) >= 3:
        return 1 if not strong_beat else min(2, max_polyphony)
    if len(recent_starts) >= 2:
        return min(2 if "rich" not in clean_style else 3, max_polyphony)
    if strong_beat:
        return max_polyphony
    if "rich" in clean_style:
        return min(max_polyphony, max(2, max_polyphony - 1))
    return min(max_polyphony, 3)


def enforce_audio_polyphony(
    notes: list[AudioMidiNote],
    max_polyphony: int,
    beat_seconds: float,
    grid_seconds: float,
    low_midi: int,
    high_midi: int,
    melody_boost: float,
    keep_bass: bool,
) -> list[AudioMidiNote]:
    if max_polyphony <= 0 or len(notes) <= max_polyphony:
        return notes
    step_seconds = max(0.045, grid_seconds if grid_seconds > 0 else beat_seconds * 0.125)
    max_time = max(note.end for note in notes)
    kept = [True] * len(notes)

    def priority(index: int) -> float:
        note = notes[index]
        return _audio_note_priority(
            note=note,
            bucket_high=max((candidate.midi for candidate in notes if abs(candidate.start - note.start) <= step_seconds), default=note.midi),
            bucket_low=min((candidate.midi for candidate in notes if abs(candidate.start - note.start) <= step_seconds), default=note.midi),
            beat_seconds=beat_seconds,
            low_midi=low_midi,
            high_midi=high_midi,
            melody_boost=melody_boost,
            keep_bass=keep_bass,
            duration_weight=6.0,
        )

    for _ in range(10):
        to_remove: set[int] = set()
        slot_count = int(max_time / step_seconds) + 2
        for slot in range(slot_count):
            time_point = slot * step_seconds + step_seconds * 0.5
            active = [
                index
                for index, note in enumerate(notes)
                if kept[index] and note.start <= time_point < note.end
            ]
            if len(active) <= max_polyphony:
                continue
            active.sort(key=priority)
            to_remove.update(active[: len(active) - max_polyphony])
        if not to_remove:
            break
        for index in to_remove:
            kept[index] = False

    return [note for index, note in enumerate(notes) if kept[index]]


def _keyboard_binding_for_midi(midi_note: int) -> KeyBinding | None:
    return KEY_MAP.get(midi_note)


def _keyboard_note_priority(
    note: AudioMidiNote,
    beat_seconds: float,
    low_midi: int,
    high_midi: int,
    keep_bass: bool,
) -> float:
    duration_beats = max(0.0, (note.end - note.start) / max(0.001, beat_seconds))
    middle = (low_midi + high_midi) / 2.0
    score = float(note.velocity)
    score += min(18.0, duration_beats * 5.0)
    score += max(-6.0, min(7.0, (note.midi - middle) * 0.14))
    if keep_bass and note.midi <= middle - 10 and _is_strong_beat(note.start, beat_seconds):
        score += 9.0
    binding = _keyboard_binding_for_midi(note.midi)
    if binding and binding.shifted:
        score -= 1.5
    return score


def enforce_visual_piano_keyboard_playability(
    notes: list[AudioMidiNote],
    beat_seconds: float,
    grid_seconds: float,
    low_midi: int,
    high_midi: int,
    keep_bass: bool,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    step_seconds = max(0.035, grid_seconds if grid_seconds > 0 else beat_seconds * 0.10)
    max_time = max(note.end for note in notes)
    kept = [True] * len(notes)

    def priority(index: int) -> float:
        return _keyboard_note_priority(notes[index], beat_seconds, low_midi, high_midi, keep_bass)

    for _ in range(8):
        to_remove: set[int] = set()
        slot_count = int(max_time / step_seconds) + 2
        for slot in range(slot_count):
            time_point = slot * step_seconds + step_seconds * 0.5
            by_base_key: dict[str, list[int]] = {}
            for index, note in enumerate(notes):
                if not kept[index] or not (note.start <= time_point < note.end):
                    continue
                binding = _keyboard_binding_for_midi(note.midi)
                if binding is None:
                    continue
                by_base_key.setdefault(binding.base_key, []).append(index)
            for conflicting_indexes in by_base_key.values():
                if len(conflicting_indexes) <= 1:
                    continue
                conflicting_indexes.sort(key=priority, reverse=True)
                to_remove.update(conflicting_indexes[1:])
        if not to_remove:
            break
        for index in to_remove:
            kept[index] = False

    return [note for index, note in enumerate(notes) if kept[index]]


def make_audio_melody_monophonic(
    notes: list[AudioMidiNote],
    min_seconds: float,
    gap_seconds: float,
) -> list[AudioMidiNote]:
    if not notes:
        return []
    melody: list[AudioMidiNote] = []
    for note in sorted(notes, key=lambda item: (item.start, -item.velocity, -item.midi)):
        if note.end <= note.start:
            continue
        if not melody:
            melody.append(note)
            continue
        previous = melody[-1]
        if note.start >= previous.end + gap_seconds:
            melody.append(note)
            continue
        desired_previous_end = note.start - gap_seconds
        if desired_previous_end - previous.start >= min_seconds * 0.45:
            melody[-1] = AudioMidiNote(
                start=previous.start,
                end=max(previous.start + 0.01, desired_previous_end),
                midi=previous.midi,
                velocity=previous.velocity,
            )
            melody.append(note)
            continue
        previous_score = previous.velocity + previous.midi * 0.08 + (previous.end - previous.start) * 12.0
        note_score = note.velocity + note.midi * 0.08 + (note.end - note.start) * 12.0
        if note_score > previous_score + 3.0:
            melody[-1] = note

    return sorted(melody, key=lambda item: (item.start, item.midi, item.end))


def trim_support_notes_under_fast_melody(
    notes: list[AudioMidiNote],
    melody_notes: list[AudioMidiNote],
    beat_seconds: float,
    min_seconds: float,
    gap_seconds: float,
    style: str,
) -> list[AudioMidiNote]:
    clean_style = style.strip().lower()
    if not notes or not melody_notes or "dense" in clean_style or "raw" in clean_style:
        return notes
    melody_identities = {(round(note.start, 6), note.midi) for note in melody_notes}
    melody_starts = sorted({note.start for note in melody_notes})
    trimmed: list[AudioMidiNote] = []
    for note in notes:
        if (round(note.start, 6), note.midi) in melody_identities:
            trimmed.append(note)
            continue
        interior_starts = [
            start
            for start in melody_starts
            if note.start + min_seconds * 0.5 < start < note.end - gap_seconds
        ]
        busy_overlap = len(interior_starts) >= 2
        quick_next = bool(interior_starts and interior_starts[0] - note.start <= beat_seconds * 0.38)
        if busy_overlap or quick_next:
            end = interior_starts[0] - gap_seconds
            if end - note.start >= min_seconds * 0.45:
                trimmed.append(AudioMidiNote(note.start, end, note.midi, note.velocity))
            continue
        trimmed.append(note)
    return trimmed


def _melody_local_score(
    item: AudioMidiNote,
    bucket_high: int,
    bucket_low: int,
    max_velocity: int,
    beat_seconds: float,
    low_midi: int,
    high_midi: int,
    melody_boost: float,
    keep_bass: bool,
    duration_weight: float,
    style: str,
) -> float:
    score = _audio_note_priority(
        item,
        bucket_high,
        bucket_low,
        beat_seconds,
        low_midi,
        high_midi,
        melody_boost,
        keep_bass,
        duration_weight,
    )
    score += item.midi * 0.08
    if item.midi == bucket_high and item.velocity < max_velocity:
        score -= (max_velocity - item.velocity) * 1.4

    clean_style = style.strip().lower()
    if "lead" in clean_style or "melody" in clean_style or "balanced" in clean_style:
        middle = (low_midi + high_midi) / 2.0
        if "lead" in clean_style or "melody" in clean_style:
            if item.midi < middle - 6:
                score -= (middle - 6 - item.midi) * 5.0
            elif item.midi >= middle:
                score += min(22.0, (item.midi - middle) * 0.75)
        elif item.midi < middle - 14:
            score -= (middle - 14 - item.midi) * 0.85
        if item.midi > high_midi - 3 and item.velocity < max_velocity + 4:
            score -= 5.0
    return score


def _melody_transition_score(previous: AudioMidiNote, current: AudioMidiNote, beat_seconds: float) -> float:
    distance = abs(current.midi - previous.midi)
    gap_beats = max(0.12, (current.start - previous.start) / max(0.001, beat_seconds))
    if distance == 0:
        score = 6.0
    elif distance <= 2:
        score = 10.0
    elif distance <= 5:
        score = 6.5
    elif distance <= 7:
        score = 2.0
    else:
        score = -((distance - 7) ** 1.18) * (1.9 / min(2.3, gap_beats + 0.35))

    if distance > 12:
        score -= min(34.0, (distance - 12) * 2.2)
    if gap_beats <= 0.55 and distance > 7:
        score -= (distance - 7) * 2.8
    if previous.midi % 12 == current.midi % 12 and distance >= 12 and gap_beats < 1.2:
        score -= 9.0
    if current.midi < previous.midi - 14 and gap_beats < 1.4:
        score -= (previous.midi - current.midi - 14) * 3.4
    return score


def select_melody_contour(
    bucket_infos: list[dict[str, object]],
    beat_seconds: float,
    low_midi: int,
    high_midi: int,
    melody_boost: float,
    keep_bass: bool,
    duration_weight: float,
    style: str,
) -> dict[int, AudioMidiNote]:
    if not bucket_infos:
        return {}
    clean_style = style.strip().lower()
    if "dense" in clean_style or "raw" in clean_style:
        return {}

    states: list[list[AudioMidiNote]] = []
    local_scores: list[list[float]] = []
    buckets_for_states: list[int] = []
    for info in bucket_infos:
        candidates = list(info["strong_candidates"])  # type: ignore[arg-type]
        if not candidates:
            continue
        candidates = sorted(
            candidates,
            key=lambda note: _melody_local_score(
                note,
                int(info["bucket_high"]),
                int(info["bucket_low"]),
                int(info["max_velocity"]),
                beat_seconds,
                low_midi,
                high_midi,
                melody_boost,
                keep_bass,
                duration_weight,
                style,
            ),
            reverse=True,
        )[:8]
        states.append(candidates)
        buckets_for_states.append(int(info["bucket"]))
        local_scores.append(
            [
                _melody_local_score(
                    note,
                    int(info["bucket_high"]),
                    int(info["bucket_low"]),
                    int(info["max_velocity"]),
                    beat_seconds,
                    low_midi,
                    high_midi,
                    melody_boost,
                    keep_bass,
                    duration_weight,
                    style,
                )
                for note in candidates
            ]
        )

    if not states:
        return {}
    if len(states) == 1:
        return {buckets_for_states[0]: states[0][0]}

    dp: list[list[float]] = []
    parents: list[list[int]] = []
    dp.append(local_scores[0])
    parents.append([-1] * len(states[0]))

    for index in range(1, len(states)):
        row_scores: list[float] = []
        row_parents: list[int] = []
        for current_index, current in enumerate(states[index]):
            best_score = -1e18
            best_parent = 0
            for previous_index, previous in enumerate(states[index - 1]):
                transition = _melody_transition_score(previous, current, beat_seconds)
                score = dp[index - 1][previous_index] + transition + local_scores[index][current_index]
                if score > best_score:
                    best_score = score
                    best_parent = previous_index
            row_scores.append(best_score)
            row_parents.append(best_parent)
        dp.append(row_scores)
        parents.append(row_parents)

    path_indexes = [0] * len(states)
    path_indexes[-1] = max(range(len(dp[-1])), key=lambda item: dp[-1][item])
    for index in range(len(states) - 1, 0, -1):
        path_indexes[index - 1] = parents[index][path_indexes[index]]

    return {
        buckets_for_states[index]: states[index][path_indexes[index]]
        for index in range(len(states))
    }


def arrange_audio_notes_for_jjs(
    notes: list[AudioMidiNote],
    bpm: float,
    low_midi: int,
    high_midi: int,
    style: str,
    max_polyphony: int,
    grid_beats: float,
    quantize_strength: float,
    min_note_beats: float,
    max_note_beats: float,
    gap_ms: float,
    keep_bass: bool,
    melody_boost: float,
    key_cleanup: bool = False,
    quantize_offset_seconds: float = 0.0,
    timing_nudge_seconds: float = 0.0,
    timing_grid_seconds: list[float] | None = None,
) -> list[AudioMidiNote]:
    if not notes:
        return []

    style_defaults = _arrangement_style_defaults(style)
    default_polyphony = int(style_defaults["max_polyphony"])
    max_polyphony = max(1, min(10, int(max_polyphony or default_polyphony)))
    if max_polyphony > default_polyphony and "dense" not in style.lower() and "rich" not in style.lower():
        max_polyphony = max(default_polyphony, min(max_polyphony, default_polyphony + 1))
    keep_bass = bool(keep_bass and style_defaults["keep_bass"])
    melody_boost = max(0.0, min(2.0, melody_boost or float(style_defaults["melody_boost"])))
    quantize_strength = max(0.0, min(1.0, quantize_strength))

    beat_seconds = 60.0 / max(1.0, bpm)
    grid_seconds = beat_seconds * max(0.0, grid_beats)
    timing_grid = normalize_timing_grid_times(timing_grid_seconds)
    if grid_seconds > 0:
        quantize_offset_seconds = quantize_offset_seconds % grid_seconds
    else:
        quantize_offset_seconds = 0.0
    bucket_seconds = max(0.055, grid_seconds if grid_seconds > 0 else beat_seconds * 0.125)
    min_seconds = beat_seconds * max(0.015, min_note_beats)
    max_seconds = beat_seconds * max(max_note_beats, min_note_beats)
    gap_seconds = max(0.0, gap_ms / 1000.0)

    prepared: list[AudioMidiNote] = []
    for note in notes:
        if note.end <= note.start:
            continue
        nudged_start = max(0.0, note.start + timing_nudge_seconds)
        nudged_end = max(nudged_start + 0.01, note.end + timing_nudge_seconds)
        if timing_grid:
            start = soft_quantize_to_grid_times(nudged_start, timing_grid, quantize_strength)
        else:
            start = soft_quantize_seconds(nudged_start, grid_seconds, quantize_strength, quantize_offset_seconds)
        original_duration = max(0.01, nudged_end - nudged_start)
        if timing_grid:
            snapped_end = soft_quantize_to_grid_times(nudged_end, timing_grid, quantize_strength * 0.55)
        else:
            snapped_end = soft_quantize_seconds(nudged_end, grid_seconds, quantize_strength * 0.55, quantize_offset_seconds)
        duration = snapped_end - start if snapped_end > start else original_duration
        duration = max(min_seconds, min(max_seconds, duration))
        midi_note = fit_midi_to_range(int(note.midi), low_midi, high_midi)
        velocity = max(24, min(127, int(note.velocity)))
        if original_duration < min_seconds * 0.45 and velocity < 82:
            continue
        prepared.append(AudioMidiNote(start=start, end=start + duration, midi=midi_note, velocity=velocity))

    prepared = remove_sparse_audio_blips(prepared, beat_seconds=beat_seconds, style=style)
    prepared = suppress_harmonic_ghost_notes(prepared, beat_seconds=beat_seconds, style=style)
    prepared, _ = cleanup_notes_to_estimated_key(
        prepared,
        bpm=bpm,
        low_midi=low_midi,
        high_midi=high_midi,
        style=style,
        enabled=key_cleanup,
    )
    prepared = suppress_harmonic_ghost_notes(prepared, beat_seconds=beat_seconds, style=style)
    clean_style = style.strip().lower()
    retrigger_gap_seconds = 0.0 if "dense" in clean_style or "raw" in clean_style else max(beat_seconds * 0.28, grid_seconds * 1.4 if grid_seconds > 0 else 0.0)
    prepared = merge_audio_notes_by_pitch(
        prepared,
        merge_gap_seconds=max(gap_seconds, 0.025),
        retrigger_gap_seconds=retrigger_gap_seconds,
    )
    if not prepared:
        return []

    buckets: dict[int, list[AudioMidiNote]] = {}
    for note in prepared:
        bucket = timing_bucket_index(note.start, bucket_seconds, timing_grid)
        buckets.setdefault(bucket, []).append(note)

    velocity_window = float(style_defaults["velocity_window"])
    cluster_spacing = int(style_defaults["cluster_spacing"])
    duration_weight = float(style_defaults["duration_weight"])
    bucket_infos: list[dict[str, object]] = []

    for bucket in sorted(buckets):
        bucket_notes = buckets[bucket]
        unique_by_midi: dict[int, AudioMidiNote] = {}
        for note in sorted(bucket_notes, key=lambda item: (item.velocity, item.end - item.start), reverse=True):
            if note.midi not in unique_by_midi:
                unique_by_midi[note.midi] = note
        candidates = list(unique_by_midi.values())
        if not candidates:
            continue
        max_velocity = max(note.velocity for note in candidates)
        strong_candidates = [note for note in candidates if note.velocity >= max_velocity - velocity_window]
        if not strong_candidates:
            strong_candidates = candidates
        bucket_infos.append(
            {
                "bucket": bucket,
                "strong_candidates": strong_candidates,
                "bucket_high": max(note.midi for note in strong_candidates),
                "bucket_low": min(note.midi for note in strong_candidates),
                "max_velocity": max_velocity,
            }
        )

    melody_by_bucket = select_melody_contour(
        bucket_infos,
        beat_seconds=beat_seconds,
        low_midi=low_midi,
        high_midi=high_midi,
        melody_boost=melody_boost,
        keep_bass=keep_bass,
        duration_weight=duration_weight,
        style=style,
    )

    selected: list[AudioMidiNote] = []
    melody_start_history: list[float] = []
    melody_notes: list[AudioMidiNote] = []

    for info in bucket_infos:
        bucket = int(info["bucket"])
        strong_candidates = list(info["strong_candidates"])  # type: ignore[arg-type]
        bucket_high = int(info["bucket_high"])
        bucket_low = int(info["bucket_low"])
        max_velocity = int(info["max_velocity"])
        melody_note = melody_by_bucket.get(bucket)
        if melody_note is None:
            melody_note = max(
                strong_candidates,
                key=lambda item: _melody_local_score(
                    item,
                    bucket_high,
                    bucket_low,
                    max_velocity,
                    beat_seconds,
                    low_midi,
                    high_midi,
                    melody_boost,
                    keep_bass,
                    duration_weight,
                    style,
                ),
            )
        melody_start_history.append(melody_note.start)
        melody_notes.append(melody_note)
        effective_polyphony = max_polyphony
        if bool(style_defaults["adaptive_texture"]):
            effective_polyphony = adaptive_polyphony_for_bucket(
                bucket_start=melody_note.start,
                melody_start_history=melody_start_history,
                max_polyphony=max_polyphony,
                beat_seconds=beat_seconds,
                style=style,
                strong_beat=_is_strong_beat(melody_note.start, beat_seconds),
            )

        chosen: list[AudioMidiNote] = [melody_note]
        if keep_bass and effective_polyphony > 1 and bucket_low != melody_note.midi:
            bass_candidates = [
                note
                for note in strong_candidates
                if note.midi == bucket_low and bucket_high - note.midi >= 12
            ]
            if bass_candidates and (_is_strong_beat(bass_candidates[0].start, beat_seconds) or "rich" in style.lower() or "dense" in style.lower()):
                chosen.append(bass_candidates[0])

        ranked = sorted(
            strong_candidates,
            key=lambda item: _audio_note_priority(
                item,
                bucket_high,
                bucket_low,
                beat_seconds,
                low_midi,
                high_midi,
                melody_boost,
                keep_bass,
                duration_weight,
            ),
            reverse=True,
        )
        for note in ranked:
            if len(chosen) >= effective_polyphony:
                break
            if any(existing.midi == note.midi for existing in chosen):
                continue
            if cluster_spacing and any(abs(existing.midi - note.midi) <= cluster_spacing for existing in chosen):
                continue
            chosen.append(note)
        bucket_anchor = melody_note.start
        aligned_chosen: list[AudioMidiNote] = []
        for note in chosen[:effective_polyphony]:
            if note.midi == melody_note.midi and math.isclose(note.start, melody_note.start, abs_tol=0.0001):
                aligned_chosen.append(note)
                continue
            duration = max(min_seconds, note.end - note.start)
            aligned_chosen.append(
                AudioMidiNote(
                    start=bucket_anchor,
                    end=bucket_anchor + duration,
                    midi=note.midi,
                    velocity=note.velocity,
                )
            )
        selected.extend(aligned_chosen)

    selected = sorted(selected, key=lambda item: (item.start, item.midi, item.end))
    if max_polyphony > 1 and bool(style_defaults["adaptive_texture"]):
        selected = trim_support_notes_under_fast_melody(
            selected,
            melody_notes=melody_notes,
            beat_seconds=beat_seconds,
            min_seconds=min_seconds,
            gap_seconds=gap_seconds,
            style=style,
        )
    if max_polyphony == 1:
        selected = make_audio_melody_monophonic(selected, min_seconds=min_seconds, gap_seconds=gap_seconds)
    else:
        selected = enforce_audio_polyphony(
            selected,
            max_polyphony=max_polyphony,
            beat_seconds=beat_seconds,
            grid_seconds=grid_seconds,
            low_midi=low_midi,
            high_midi=high_midi,
            melody_boost=melody_boost,
            keep_bass=keep_bass,
        )

    by_note: dict[int, list[AudioMidiNote]] = {}
    for note in selected:
        by_note.setdefault(note.midi, []).append(note)

    trimmed: list[AudioMidiNote] = []
    for midi_note, midi_notes in by_note.items():
        midi_notes.sort(key=lambda item: item.start)
        for index, note in enumerate(midi_notes):
            end = min(note.end, note.start + max_seconds)
            if index + 1 < len(midi_notes):
                end = min(end, midi_notes[index + 1].start - gap_seconds)
            if end - note.start < min_seconds:
                end = note.start + min_seconds
            if end > note.start:
                trimmed.append(AudioMidiNote(note.start, end, midi_note, note.velocity))

    if not trimmed:
        return []
    trimmed = enforce_visual_piano_keyboard_playability(
        trimmed,
        beat_seconds=beat_seconds,
        grid_seconds=grid_seconds,
        low_midi=low_midi,
        high_midi=high_midi,
        keep_bass=keep_bass,
    )
    if not trimmed:
        return []
    first_start = min(note.start for note in trimmed)
    return sorted(
        [
            AudioMidiNote(
                start=max(0.0, note.start - first_start),
                end=max(0.001, note.end - first_start),
                midi=note.midi,
                velocity=note.velocity,
            )
            for note in trimmed
        ],
        key=lambda item: (item.start, item.midi, item.end),
    )


def max_audio_polyphony(notes: list[AudioMidiNote]) -> int:
    events: list[tuple[float, int]] = []
    for note in notes:
        events.append((note.start, 1))
        events.append((note.end, -1))
    active = 0
    maximum = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def average_audio_polyphony(notes: list[AudioMidiNote]) -> float:
    if not notes:
        return 0.0
    events: list[tuple[float, int]] = []
    for note in notes:
        events.append((note.start, 1))
        events.append((note.end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    last_time = events[0][0]
    weighted = 0.0
    total_time = max(note.end for note in notes) - min(note.start for note in notes)
    for time_point, delta in events:
        if time_point > last_time:
            weighted += active * (time_point - last_time)
            last_time = time_point
        active += delta
    if total_time <= 0:
        return float(max_audio_polyphony(notes))
    return weighted / total_time


def max_visual_piano_key_conflicts(notes: list[AudioMidiNote]) -> int:
    events: list[tuple[float, int, AudioMidiNote]] = []
    for note in notes:
        events.append((note.start, 1, note))
        events.append((note.end, -1, note))
    active: dict[tuple[int, float], AudioMidiNote] = {}
    max_conflicts = 0
    for time_point, delta, note in sorted(events, key=lambda item: (item[0], item[1])):
        identity = (note.midi, note.start)
        if delta > 0:
            active[identity] = note
        else:
            active.pop(identity, None)
        by_base_key: dict[str, int] = {}
        for active_note in active.values():
            binding = _keyboard_binding_for_midi(active_note.midi)
            if binding is None:
                continue
            by_base_key[binding.base_key] = by_base_key.get(binding.base_key, 0) + 1
        if by_base_key:
            max_conflicts = max(max_conflicts, max(max(0, count - 1) for count in by_base_key.values()))
    return max_conflicts


def audio_active_ratio(notes: list[AudioMidiNote]) -> float:
    if not notes:
        return 0.0
    events: list[tuple[float, int]] = []
    for note in notes:
        events.append((note.start, 1))
        events.append((note.end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    if not events:
        return 0.0
    active = 0
    last_time = events[0][0]
    active_time = 0.0
    total_time = max(note.end for note in notes) - min(note.start for note in notes)
    for time_point, delta in events:
        if time_point > last_time and active > 0:
            active_time += time_point - last_time
        last_time = time_point
        active += delta
    if total_time <= 0:
        return 1.0
    return max(0.0, min(1.0, active_time / total_time))


def melody_contour_score(notes: list[AudioMidiNote], bpm: float) -> float:
    if len(notes) < 3:
        return 0.0
    beat_seconds = 60.0 / max(1.0, bpm)
    bucket_seconds = max(0.045, beat_seconds * 0.125)
    buckets: dict[int, list[AudioMidiNote]] = {}
    for note in notes:
        buckets.setdefault(int(round(note.start / bucket_seconds)), []).append(note)

    melody: list[AudioMidiNote] = []
    for bucket in sorted(buckets):
        bucket_notes = buckets[bucket]
        melody.append(max(bucket_notes, key=lambda item: (item.velocity + item.midi * 0.16, item.end - item.start)))
    if len(melody) < 3:
        return 0.0

    score = 18.0
    penalties = 0.0
    rewards = 0.0
    for previous, current in zip(melody, melody[1:]):
        distance = abs(current.midi - previous.midi)
        gap_beats = max(0.05, (current.start - previous.start) / max(0.001, beat_seconds))
        if distance <= 2:
            rewards += 1.2
        elif distance <= 5:
            rewards += 0.6
        elif distance > 12 and gap_beats < 1.7:
            penalties += (distance - 12) * 1.3
        elif distance > 7 and gap_beats < 0.75:
            penalties += (distance - 7) * 0.85
    return max(-24.0, min(24.0, score + rewards - penalties / max(1.0, len(melody) - 1)))


def melody_average_midi(notes: list[AudioMidiNote], bpm: float) -> float:
    if not notes:
        return 0.0
    beat_seconds = 60.0 / max(1.0, bpm)
    bucket_seconds = max(0.045, beat_seconds * 0.125)
    buckets: dict[int, list[AudioMidiNote]] = {}
    for note in notes:
        buckets.setdefault(int(round(note.start / bucket_seconds)), []).append(note)
    melody_pitches: list[int] = []
    for bucket_notes in buckets.values():
        melody_note = max(bucket_notes, key=lambda item: (item.velocity + item.midi * 0.16, item.end - item.start))
        melody_pitches.append(melody_note.midi)
    if not melody_pitches:
        return 0.0
    return sum(melody_pitches) / len(melody_pitches)


def melody_low_register_fraction(notes: list[AudioMidiNote], bpm: float, threshold_midi: float) -> float:
    if not notes:
        return 0.0
    beat_seconds = 60.0 / max(1.0, bpm)
    bucket_seconds = max(0.045, beat_seconds * 0.125)
    buckets: dict[int, list[AudioMidiNote]] = {}
    for note in notes:
        buckets.setdefault(int(round(note.start / bucket_seconds)), []).append(note)
    if not buckets:
        return 0.0
    low_count = 0
    for bucket_notes in buckets.values():
        melody_note = max(bucket_notes, key=lambda item: (item.velocity + item.midi * 0.16, item.end - item.start))
        if melody_note.midi < threshold_midi:
            low_count += 1
    return low_count / len(buckets)


def chord_dissonance_penalty(notes: list[AudioMidiNote], bpm: float) -> float:
    if len(notes) < 2:
        return 0.0
    beat_seconds = 60.0 / max(1.0, bpm)
    bucket_seconds = max(0.045, beat_seconds * 0.125)
    buckets: dict[int, set[int]] = {}
    for note in notes:
        buckets.setdefault(int(round(note.start / bucket_seconds)), set()).add(note.midi)
    penalty = 0.0
    for midi_notes in buckets.values():
        ordered = sorted(midi_notes)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                interval = abs(right - left) % 12
                if interval == 1:
                    penalty += 1.8
                elif interval == 2:
                    penalty += 0.8
                elif interval == 6:
                    penalty += 1.0
    return penalty


def score_jjs_arrangement_quality(
    notes: list[AudioMidiNote],
    bpm: float,
    low_midi: int,
    high_midi: int,
    style: str,
) -> float:
    if not notes:
        return -10000.0
    duration = max(0.25, max(note.end for note in notes) - min(note.start for note in notes))
    density = len(notes) / duration
    average_polyphony = average_audio_polyphony(notes)
    maximum_polyphony = max_audio_polyphony(notes)
    active_ratio = audio_active_ratio(notes)
    key_conflicts = max_visual_piano_key_conflicts(notes)
    note_range = max(note.midi for note in notes) - min(note.midi for note in notes)
    clean_style = style.strip().lower()

    if "lead" in clean_style or "melody" in clean_style:
        target_density = 3.2
        target_polyphony = 1.05
    elif "rich" in clean_style:
        target_density = 6.0
        target_polyphony = 2.6
    elif "dense" in clean_style or "raw" in clean_style:
        target_density = 8.0
        target_polyphony = 3.5
    else:
        target_density = 5.4
        target_polyphony = 2.35

    density_score = 22.0 - abs(density - target_density) * 4.2
    if density < target_density * 0.38:
        density_score -= (target_density * 0.38 - density) * 12.0
    if density > target_density * 2.0:
        density_score -= (density - target_density * 2.0) * 5.0

    polyphony_score = 18.0 - abs(average_polyphony - target_polyphony) * 8.0
    if maximum_polyphony > 6 and "dense" not in clean_style:
        polyphony_score -= (maximum_polyphony - 6) * 4.0

    active_score = 16.0 * min(1.0, active_ratio / 0.72)
    if active_ratio < 0.28 and duration > 8:
        active_score -= (0.28 - active_ratio) * 35.0

    contour_score = melody_contour_score(notes, bpm)
    average_melody_midi = melody_average_midi(notes, bpm)
    melody_register_score = 0.0
    middle = (low_midi + high_midi) / 2.0
    if "lead" in clean_style or "melody" in clean_style:
        if average_melody_midi < middle:
            melody_register_score -= (middle - average_melody_midi) * 2.4
        low_fraction = melody_low_register_fraction(notes, bpm, middle - 2)
        melody_register_score -= max(0.0, low_fraction - 0.05) * 110.0
    elif "balanced" in clean_style:
        if average_melody_midi < middle - 8:
            melody_register_score -= (middle - 8 - average_melody_midi) * 1.6
        low_fraction = melody_low_register_fraction(notes, bpm, middle - 8)
        melody_register_score -= max(0.0, low_fraction - 0.15) * 80.0
    conflict_score = -38.0 * key_conflicts
    dissonance_score = -6.5 * chord_dissonance_penalty(notes, bpm)
    range_score = 8.0
    if note_range < 7 and duration > 8:
        range_score -= 8.0
    elif note_range > high_midi - low_midi - 4:
        range_score -= 6.0

    return (
        density_score
        + polyphony_score
        + active_score
        + contour_score
        + melody_register_score
        + conflict_score
        + dissonance_score
        + range_score
    )


def smart_arrangement_candidate_specs(
    style: str,
    max_polyphony: int,
    grid_beats: float,
    quantize_strength: float,
    min_note_beats: float,
    max_note_beats: float,
    gap_ms: float,
    keep_bass: bool,
    melody_boost: float,
    key_cleanup: bool,
) -> list[dict[str, object]]:
    base = {
        "label": style,
        "style": style,
        "max_polyphony": max_polyphony,
        "grid_beats": grid_beats,
        "quantize_strength": quantize_strength,
        "min_note_beats": min_note_beats,
        "max_note_beats": max_note_beats,
        "gap_ms": gap_ms,
        "keep_bass": keep_bass,
        "melody_boost": melody_boost,
        "key_cleanup": key_cleanup,
    }
    candidates: list[dict[str, object]] = []

    def add(label: str, **updates: object) -> None:
        candidate = dict(base)
        candidate.update(updates)
        candidate["label"] = label
        if not any(str(item["label"]) == label for item in candidates):
            candidates.append(candidate)

    clean_style = style.strip().lower()
    add(f"{style} base")
    if "raw" in clean_style:
        return candidates
    if "lead" in clean_style or "melody" in clean_style:
        add("Lead clear", style="Lead melody", max_polyphony=1, quantize_strength=min(0.62, quantize_strength + 0.08), melody_boost=max(melody_boost, 1.45), keep_bass=False)
        add("Lead natural", style="Lead melody", max_polyphony=1, quantize_strength=max(0.18, quantize_strength - 0.16), melody_boost=max(melody_boost, 1.25), keep_bass=False)
        return candidates
    if "rich" in clean_style:
        add("Rich smooth", style="Rich piano", max_polyphony=max(3, min(6, max_polyphony)), quantize_strength=max(0.18, quantize_strength - 0.10), melody_boost=max(0.75, melody_boost))
        add("Rich controlled", style="Balanced JJS", max_polyphony=max(3, min(4, max_polyphony)), quantize_strength=max(0.28, quantize_strength), melody_boost=max(0.95, melody_boost), keep_bass=True)
        add("Lead rescue", style="Lead melody", max_polyphony=1, quantize_strength=max(0.24, quantize_strength - 0.08), melody_boost=1.45, keep_bass=False)
        return candidates
    if "dense" in clean_style:
        add("Dense cleaner", style="Balanced JJS", max_polyphony=max(3, min(5, max_polyphony)), quantize_strength=max(0.22, quantize_strength), melody_boost=max(0.9, melody_boost), keep_bass=keep_bass)
        return candidates

    add("Balanced natural", style="Balanced JJS", max_polyphony=max(2, min(4, max_polyphony)), quantize_strength=max(0.20, quantize_strength - 0.14), melody_boost=max(1.0, melody_boost), keep_bass=keep_bass)
    add("Balanced clear", style="Balanced JJS", max_polyphony=max(2, min(3, max_polyphony)), quantize_strength=min(0.64, quantize_strength + 0.12), melody_boost=max(1.15, melody_boost), keep_bass=keep_bass)
    add("Lead rescue", style="Lead melody", max_polyphony=1, quantize_strength=max(0.22, quantize_strength - 0.06), melody_boost=1.55, keep_bass=False)
    add("Rich light", style="Rich piano", max_polyphony=max(3, min(5, max_polyphony + 1)), quantize_strength=max(0.18, quantize_strength - 0.16), melody_boost=max(0.85, melody_boost), keep_bass=True)
    return candidates


def smart_arrange_audio_notes_for_jjs(
    notes: list[AudioMidiNote],
    bpm: float,
    low_midi: int,
    high_midi: int,
    style: str,
    max_polyphony: int,
    grid_beats: float,
    quantize_strength: float,
    min_note_beats: float,
    max_note_beats: float,
    gap_ms: float,
    keep_bass: bool,
    melody_boost: float,
    key_cleanup: bool = False,
    quantize_offset_seconds: float = 0.0,
    timing_nudge_seconds: float = 0.0,
    timing_grid_seconds: list[float] | None = None,
) -> tuple[list[AudioMidiNote], str, float, int]:
    candidates = smart_arrangement_candidate_specs(
        style,
        max_polyphony,
        grid_beats,
        quantize_strength,
        min_note_beats,
        max_note_beats,
        gap_ms,
        keep_bass,
        melody_boost,
        key_cleanup,
    )
    best_notes: list[AudioMidiNote] = []
    best_label = style
    best_score = -10000.0
    score_style = style
    for candidate in candidates:
        arranged = arrange_audio_notes_for_jjs(
            notes=notes,
            bpm=bpm,
            low_midi=low_midi,
            high_midi=high_midi,
            style=str(candidate["style"]),
            max_polyphony=int(candidate["max_polyphony"]),
            grid_beats=float(candidate["grid_beats"]),
            quantize_strength=float(candidate["quantize_strength"]),
            min_note_beats=float(candidate["min_note_beats"]),
            max_note_beats=float(candidate["max_note_beats"]),
            gap_ms=float(candidate["gap_ms"]),
            keep_bass=bool(candidate["keep_bass"]),
            melody_boost=float(candidate["melody_boost"]),
            key_cleanup=bool(candidate["key_cleanup"]),
            quantize_offset_seconds=quantize_offset_seconds,
            timing_nudge_seconds=timing_nudge_seconds,
            timing_grid_seconds=timing_grid_seconds,
        )
        score = score_jjs_arrangement_quality(arranged, bpm, low_midi, high_midi, score_style)
        if score > best_score:
            best_score = score
            best_label = str(candidate["label"])
            best_notes = arranged
    return best_notes, best_label, best_score, len(candidates)


def convert_audio_melody_to_notes(
    path: str,
    bpm: float,
    low_midi: int,
    high_midi: int,
    confidence_threshold: float,
    min_note_beats: float,
    grid_beats: float,
    sample_rate: int,
    trim_silence: bool,
    harmonic_only: bool,
) -> list[AudioMidiNote]:
    require_audio_dependencies()
    y, sr = load_audio_mono(path, sample_rate, trim_silence)
    if harmonic_only and len(y):
        y = librosa.effects.harmonic(y)

    hop_length = 512
    frame_length = 2048
    fmin = midi_to_hz(low_midi)
    fmax = midi_to_hz(high_midi)
    try:
        f0, voiced_flag, voiced_prob = librosa.pyin(
            y,
            fmin=fmin,
            fmax=fmax,
            sr=sr,
            frame_length=frame_length,
            hop_length=hop_length,
            fill_na=np.nan,
        )
    except Exception:
        f0 = librosa.yin(y, fmin=fmin, fmax=fmax, sr=sr, frame_length=frame_length, hop_length=hop_length)
        rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]
        energy_cutoff = float(np.percentile(rms, 60)) if len(rms) else 0.0
        voiced_flag = rms > energy_cutoff
        voiced_prob = np.where(voiced_flag, 0.75, 0.0)

    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=hop_length)
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, hop_length=hop_length, backtrack=True)
    onset_times = [float(item) for item in librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)]
    frame_midi: list[int | None] = []
    frame_conf: list[float] = []
    for hz, voiced, confidence in zip(f0, voiced_flag, voiced_prob):
        if not voiced or not np.isfinite(hz) or hz <= 0 or float(confidence) < confidence_threshold:
            frame_midi.append(None)
            frame_conf.append(0.0)
            continue
        midi_note = clamp_midi_note(int(round(hz_to_midi_float(float(hz)))), low_midi, high_midi)
        frame_midi.append(midi_note)
        frame_conf.append(float(confidence))

    frame_midi = median_smooth_optional_notes(frame_midi, radius=2)
    frame_midi = remove_short_none_gaps(frame_midi, max_gap_frames=2)
    frame_midi = remove_short_note_runs(frame_midi, min_frames=2)

    raw_notes: list[AudioMidiNote] = []
    current_note: int | None = None
    current_start = 0.0
    current_conf: list[float] = []
    frame_step = float(hop_length) / float(sr)

    for index, midi_note in enumerate(frame_midi):
        frame_time = float(times[index])
        if midi_note == current_note:
            if midi_note is not None:
                current_conf.append(frame_conf[index])
            continue
        if current_note is not None:
            end_time = frame_time
            velocity = int(55 + 45 * min(1.0, max(current_conf) if current_conf else 0.65))
            raw_notes.append(AudioMidiNote(current_start, end_time, current_note, velocity))
        current_note = midi_note
        current_start = frame_time
        current_conf = [frame_conf[index]] if midi_note is not None else []

    if current_note is not None:
        end_time = float(times[-1] + frame_step) if len(times) else current_start + frame_step
        velocity = int(55 + 45 * min(1.0, max(current_conf) if current_conf else 0.65))
        raw_notes.append(AudioMidiNote(current_start, end_time, current_note, velocity))

    return postprocess_audio_notes(
        raw_notes,
        bpm,
        grid_beats,
        min_note_beats,
        low_midi,
        high_midi,
        merge_gap_seconds=0.08,
        onset_times=onset_times,
    )


def convert_audio_spectral_to_notes(
    path: str,
    bpm: float,
    low_midi: int,
    high_midi: int,
    sensitivity: float,
    min_note_beats: float,
    grid_beats: float,
    sample_rate: int,
    trim_silence: bool,
    harmonic_only: bool,
    max_polyphony: int,
) -> list[AudioMidiNote]:
    require_audio_dependencies()
    y, sr = load_audio_mono(path, sample_rate, trim_silence)
    if harmonic_only and len(y):
        y = librosa.effects.harmonic(y)

    hop_length = 512
    n_bins = high_midi - low_midi + 1
    cqt = np.abs(
        librosa.cqt(
            y,
            sr=sr,
            hop_length=hop_length,
            fmin=midi_to_hz(low_midi),
            n_bins=n_bins,
            bins_per_octave=12,
        )
    )
    if cqt.size == 0:
        return []
    power = librosa.amplitude_to_db(cqt, ref=np.max)
    times = librosa.frames_to_time(np.arange(power.shape[1]), sr=sr, hop_length=hop_length)
    active_threshold_db = -24.0 - sensitivity * 42.0
    frame_sets: list[set[int]] = []
    max_polyphony = max(1, min(8, max_polyphony))

    for frame_index in range(power.shape[1]):
        column = power[:, frame_index]
        peak_db = float(np.max(column))
        if peak_db < -72:
            frame_sets.append(set())
            continue
        threshold = max(active_threshold_db, peak_db - (12.0 + sensitivity * 24.0))
        candidates = np.where(column >= threshold)[0]
        peaks: list[tuple[float, int]] = []
        for bin_index in candidates:
            left = column[bin_index - 1] if bin_index > 0 else -120.0
            right = column[bin_index + 1] if bin_index + 1 < len(column) else -120.0
            if column[bin_index] >= left and column[bin_index] >= right:
                peaks.append((float(column[bin_index]), int(bin_index)))
        peaks.sort(reverse=True)
        selected_bins: list[int] = []
        for strength, bin_index in peaks:
            if len(selected_bins) >= max_polyphony:
                break
            if any(abs(bin_index - existing) <= 1 for existing in selected_bins):
                continue
            if peaks and strength < peaks[0][0] - (10.0 + sensitivity * 10.0):
                continue
            selected_bins.append(bin_index)
        notes = {low_midi + bin_index for bin_index in selected_bins}
        frame_sets.append(notes)

    raw_notes: list[AudioMidiNote] = []
    active_starts: dict[int, float] = {}
    active_strength: dict[int, float] = {}
    frame_step = float(hop_length) / float(sr)

    for frame_index, active_notes in enumerate(frame_sets):
        frame_time = float(times[frame_index])
        for midi_note in list(active_starts):
            if midi_note not in active_notes:
                velocity = int(55 + min(45, max(0, active_strength.get(midi_note, -60.0) + 60.0)))
                raw_notes.append(AudioMidiNote(active_starts[midi_note], frame_time, midi_note, velocity))
                del active_starts[midi_note]
                active_strength.pop(midi_note, None)
        for midi_note in active_notes:
            bin_index = midi_note - low_midi
            strength = float(power[bin_index, frame_index])
            if midi_note not in active_starts:
                active_starts[midi_note] = frame_time
                active_strength[midi_note] = strength
            else:
                active_strength[midi_note] = max(active_strength[midi_note], strength)

    end_time = float(times[-1] + frame_step) if len(times) else frame_step
    for midi_note, start in active_starts.items():
        velocity = int(55 + min(45, max(0, active_strength.get(midi_note, -60.0) + 60.0)))
        raw_notes.append(AudioMidiNote(start, end_time, midi_note, velocity))

    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, hop_length=hop_length, backtrack=True)
    onset_times = [float(item) for item in librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)]

    return postprocess_audio_notes(
        raw_notes,
        bpm,
        grid_beats,
        min_note_beats,
        low_midi,
        high_midi,
        merge_gap_seconds=0.05,
        onset_times=onset_times,
    )


def convert_audio_basic_pitch_to_notes(
    path: str,
    bpm: float,
    low_midi: int,
    high_midi: int,
    sensitivity: float,
    min_note_beats: float,
    grid_beats: float,
    melodia_trick: bool,
    sample_rate: int | None = None,
    trim_silence: bool | None = None,
    harmonic_only: bool | None = None,
    postprocess: bool = True,
) -> list[AudioMidiNote]:
    inference = require_basic_pitch()
    predict = inference.predict
    sensitivity = min(0.95, max(0.05, sensitivity))
    onset_threshold = max(0.08, min(0.85, 0.72 - sensitivity * 0.55))
    frame_threshold = max(0.08, min(0.65, 0.52 - sensitivity * 0.34))
    minimum_note_length_ms = max(20.0, min_note_beats * 60000.0 / max(1.0, bpm))

    input_path = path
    cleanup_path: str | None = None
    if bool(trim_silence) or bool(harmonic_only):
        y, sr = load_audio_mono(path, int(sample_rate or 32000), bool(trim_silence))
        if not len(y):
            raise ValueError("The selected audio file is empty after trimming silence.")
        if bool(harmonic_only) and len(y):
            y = librosa.effects.harmonic(y, margin=8.0)
            y = normalize_audio(y)
        cleanup_path = write_temp_audio_file(y, sr)
        input_path = cleanup_path

    try:
        _, _, note_events = predict(
            input_path,
            onset_threshold=onset_threshold,
            frame_threshold=frame_threshold,
            minimum_note_length=minimum_note_length_ms,
            minimum_frequency=midi_to_hz(low_midi),
            maximum_frequency=midi_to_hz(high_midi),
            multiple_pitch_bends=False,
            melodia_trick=melodia_trick,
            midi_tempo=max(1.0, bpm),
        )
    finally:
        if cleanup_path:
            try:
                Path(cleanup_path).unlink(missing_ok=True)
            except Exception:
                pass

    raw_notes: list[AudioMidiNote] = []
    for event in note_events:
        start_time = float(event[0])
        end_time = float(event[1])
        midi_note = int(event[2])
        amplitude = float(event[3]) if len(event) > 3 else 0.7
        if end_time <= start_time:
            continue
        if midi_note < low_midi or midi_note > high_midi:
            continue
        velocity = int(max(32, min(120, 42 + amplitude * 84)))
        raw_notes.append(AudioMidiNote(start=start_time, end=end_time, midi=midi_note, velocity=velocity))

    if not postprocess:
        return sorted(raw_notes, key=lambda item: (item.start, item.midi, item.end))

    return postprocess_audio_notes(
        raw_notes,
        bpm=bpm,
        grid_beats=grid_beats,
        min_note_beats=max(0.03, min_note_beats * 0.65),
        low_midi=low_midi,
        high_midi=high_midi,
        merge_gap_seconds=0.035,
    )


def fold_bpm_to_playable_range(bpm: float, low: float = 70.0, high: float = 220.0) -> float:
    bpm = float(bpm)
    if not math.isfinite(bpm) or bpm <= 0:
        return 120.0
    while bpm < low:
        bpm *= 2.0
    while bpm > high:
        bpm /= 2.0
    return max(40.0, min(260.0, bpm))


def bpm_candidates_near(base_bpm: float) -> list[float]:
    candidates: list[float] = []
    for factor in (0.5, 2.0 / 3.0, 0.75, 1.0, 4.0 / 3.0, 1.5, 2.0):
        folded = fold_bpm_to_playable_range(base_bpm * factor)
        if all(abs(folded - existing) > 0.75 for existing in candidates):
            candidates.append(folded)
    return candidates


def score_bpm_against_times(times: list[float], bpm: float, grid_beats: float = 0.25) -> float:
    if len(times) < 4 or bpm <= 0:
        return 0.0
    beat_seconds = 60.0 / bpm
    grid_seconds = beat_seconds * max(0.03125, grid_beats)
    if grid_seconds <= 0:
        return 0.0

    phases = [time_point % grid_seconds for time_point in times if time_point >= 0]
    if len(phases) < 4:
        return 0.0

    best_distance = float("inf")
    for candidate in phases:
        distance = 0.0
        for phase in phases:
            raw_distance = abs(phase - candidate)
            distance += min(raw_distance, grid_seconds - raw_distance)
        best_distance = min(best_distance, distance / len(phases))
    return max(0.0, min(1.0, 1.0 - best_distance / max(0.001, grid_seconds * 0.5)))


def rhythm_density_score(times: list[float], bpm: float) -> float:
    if len(times) < 4:
        return 0.0
    starts = sorted(set(round(time_point, 4) for time_point in times if time_point >= 0))
    intervals = [
        right - left
        for left, right in zip(starts, starts[1:])
        if 0.035 <= right - left <= 2.0
    ]
    if not intervals:
        return 0.0
    intervals.sort()
    median_interval = intervals[len(intervals) // 2]
    median_beats = median_interval * bpm / 60.0
    targets = (
        (0.50, 1.00),
        (1.00, 0.90),
        (0.75, 0.82),
        (0.25, 0.58),
        (1.50, 0.55),
    )
    best = 0.0
    for target, weight in targets:
        closeness = max(0.0, 1.0 - abs(median_beats - target) / max(0.001, target))
        best = max(best, closeness * weight)
    if median_beats < 0.18:
        best *= 0.65
    elif median_beats > 2.25:
        best *= 0.72
    return best


def choose_best_bpm_candidate(candidates: list[float], times: list[float], fallback_bpm: float) -> float:
    if not candidates:
        return fold_bpm_to_playable_range(fallback_bpm)
    unique_candidates: list[float] = []
    for candidate in candidates:
        folded = fold_bpm_to_playable_range(candidate)
        if all(abs(folded - existing) > 0.75 for existing in unique_candidates):
            unique_candidates.append(folded)
    if len(unique_candidates) == 1 or len(times) < 4:
        return unique_candidates[0]

    def candidate_score(candidate: float) -> float:
        grid_16 = score_bpm_against_times(times, candidate, grid_beats=0.25)
        grid_32 = score_bpm_against_times(times, candidate, grid_beats=0.125)
        density = rhythm_density_score(times, candidate)
        tempo_center_bonus = max(0.0, 1.0 - abs(candidate - 145.0) / 120.0) * 0.08
        return grid_16 * 0.48 + grid_32 * 0.24 + density * 0.28 + tempo_center_bonus

    fallback = fold_bpm_to_playable_range(fallback_bpm)
    best = max(unique_candidates, key=candidate_score)
    fallback_score = candidate_score(fallback)
    best_score = candidate_score(best)
    if best_score < fallback_score + 0.035:
        return fallback
    return best


def refine_bpm_from_note_starts(notes: list[AudioMidiNote], base_bpm: float) -> float:
    starts = sorted(set(round(note.start, 4) for note in notes if note.end > note.start))
    if len(starts) < 8:
        return fold_bpm_to_playable_range(base_bpm)
    intervals = [
        right - left
        for left, right in zip(starts, starts[1:])
        if 0.04 <= right - left <= 2.0
    ]
    candidates = bpm_candidates_near(base_bpm)
    if intervals:
        intervals.sort()
        median_interval = intervals[len(intervals) // 2]
        for beat_fraction in (0.25, 0.5, 0.75, 1.0, 1.5):
            if median_interval > 0:
                candidates.extend(bpm_candidates_near(60.0 * beat_fraction / median_interval))
    return choose_best_bpm_candidate(candidates, starts, base_bpm)


def estimate_audio_bpm(path: str, sample_rate: int, trim_silence: bool) -> float:
    require_audio_dependencies()
    y, sr = load_audio_mono(path, sample_rate, trim_silence)
    if not len(y):
        raise ValueError("The selected audio file is empty.")
    hop_length = 512
    onset_envelope = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr, onset_envelope=onset_envelope, hop_length=hop_length)
    if hasattr(tempo, "__len__"):
        tempo = float(tempo[0])
    base_tempo = fold_bpm_to_playable_range(float(tempo))

    candidates = bpm_candidates_near(base_tempo)
    try:
        local_tempos = librosa.feature.tempo(
            onset_envelope=onset_envelope,
            sr=sr,
            hop_length=hop_length,
            aggregate=None,
        )
        if len(local_tempos):
            for percentile in (25, 50, 75):
                candidates.extend(bpm_candidates_near(float(np.percentile(local_tempos, percentile))))
    except Exception:
        pass

    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_envelope,
        sr=sr,
        hop_length=hop_length,
        backtrack=True,
    )
    onset_times = [float(item) for item in librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)]
    return choose_best_bpm_candidate(candidates, onset_times, base_tempo)


def audio_notes_to_actions(notes: list[AudioMidiNote]) -> list[ScheduledAction]:
    actions: list[ScheduledAction] = []
    for note in notes:
        playable = Playable("midi", note.midi)
        actions.append(ScheduledAction(note.start, "down", (playable,)))
        actions.append(ScheduledAction(note.end, "up", (playable,)))
    return coalesce_scheduled_actions(actions)


def audio_notes_to_text(notes: list[AudioMidiNote], bpm: float) -> str:
    if not notes:
        return ""
    beat_seconds = 60.0 / max(1.0, bpm)
    grouped: dict[tuple[float, float], list[int]] = {}
    for note in notes:
        start_beats = round(note.start / beat_seconds * 4) / 4
        duration_beats = max(0.25, round((note.end - note.start) / beat_seconds * 4) / 4)
        grouped.setdefault((start_beats, duration_beats), []).append(note.midi)

    tokens: list[str] = []
    current_beat = 0.0
    for (start_beats, duration_beats), midi_notes in sorted(grouped.items()):
        if start_beats > current_beat + 0.001:
            rest_beats = start_beats - current_beat
            tokens.append(f"R:{PianoMacroApp._format_beats(rest_beats)}")
        note_names = [midi_to_note_name(note) for note in sorted(set(midi_notes))]
        if len(note_names) == 1:
            token = note_names[0]
        else:
            token = "[" + " ".join(note_names) + "]"
        token += f":{PianoMacroApp._format_beats(duration_beats)}"
        tokens.append(token)
        current_beat = max(current_beat, start_beats + duration_beats)

    return "\n".join(" ".join(tokens[index : index + 8]) for index in range(0, len(tokens), 8))


def write_audio_notes_to_midi(notes: list[AudioMidiNote], bpm: float, path: str) -> None:
    if mido is None:
        raise RuntimeError("MIDI export needs mido. Install it with: python -m pip install mido")
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(max(1.0, bpm)), time=0))

    events: list[tuple[int, int, AudioMidiNote]] = []
    for note in notes:
        start_tick = max(0, int(round(note.start * bpm / 60.0 * mid.ticks_per_beat)))
        end_tick = max(start_tick + 1, int(round(note.end * bpm / 60.0 * mid.ticks_per_beat)))
        events.append((start_tick, 1, note))
        events.append((end_tick, 0, note))
    events.sort(key=lambda item: (item[0], item[1]))

    last_tick = 0
    for tick, kind, note in events:
        delta = max(0, tick - last_tick)
        if kind == 1:
            track.append(mido.Message("note_on", note=note.midi, velocity=note.velocity, time=delta))
        else:
            track.append(mido.Message("note_off", note=note.midi, velocity=0, time=delta))
        last_tick = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(path)


def _vk_for_key(key: str) -> int:
    clean = key.lower()
    if clean == "shift":
        return 0x10
    if len(clean) == 1 and "a" <= clean <= "z":
        return ord(clean.upper())
    if len(clean) == 1 and "0" <= clean <= "9":
        return ord(clean)
    raise ValueError(f"Unsupported key for Windows SendInput: {key!r}")


ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUTUNION(ctypes.Union):
    _fields_ = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("union", INPUTUNION),
    )


class WindowsScanCodeSender:
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    MAPVK_VK_TO_VSC = 0

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.keybd_event.argtypes = (ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ULONG_PTR)
        self.user32.keybd_event.restype = None

    def send_input(self, key: str, down: bool, use_scan_code: bool) -> None:
        vk = _vk_for_key(key)
        scan = self.user32.MapVirtualKeyW(vk, self.MAPVK_VK_TO_VSC)
        if scan == 0:
            raise RuntimeError(f"Could not resolve scan code for {key!r}.")
        flags = self.KEYEVENTF_SCANCODE if use_scan_code else 0
        if not down:
            flags |= self.KEYEVENTF_KEYUP
        event = INPUT(
            type=self.INPUT_KEYBOARD,
            union=INPUTUNION(
                ki=KEYBDINPUT(
                    wVk=0 if use_scan_code else vk,
                    wScan=scan if use_scan_code else 0,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        sent = self.user32.SendInput(1, ctypes.pointer(event), ctypes.sizeof(INPUT))
        if sent != 1:
            error = ctypes.get_last_error()
            raise OSError(error, f"SendInput failed for {key!r}")

    def keybd_event(self, key: str, down: bool) -> None:
        vk = _vk_for_key(key)
        scan = self.user32.MapVirtualKeyW(vk, self.MAPVK_VK_TO_VSC)
        if scan == 0:
            raise RuntimeError(f"Could not resolve scan code for {key!r}.")
        flags = 0 if down else self.KEYEVENTF_KEYUP
        self.user32.keybd_event(vk, scan, flags, 0)


class KeySender:
    def __init__(self) -> None:
        self.active: dict[str, tuple[KeyBinding, int]] = {}
        self.shift_down = False
        self.lock = threading.Lock()
        self.method = "Windows SendInput scan"
        self.windows_sender: WindowsScanCodeSender | None = None

    def set_method(self, method: str) -> None:
        self.method = method

    def _send_key(self, key: str, down: bool) -> None:
        if self.method == "PyAutoGUI":
            if pyautogui is None:
                raise RuntimeError("pyautogui is not installed. Run: python -m pip install pyautogui pynput mido")
            pyautogui.PAUSE = 0
            if down:
                pyautogui.keyDown(key)
            else:
                pyautogui.keyUp(key)
            return

        if self.windows_sender is None:
            self.windows_sender = WindowsScanCodeSender()
        if self.method == "Windows keybd_event":
            self.windows_sender.keybd_event(key, down)
        else:
            self.windows_sender.send_input(key, down, use_scan_code=self.method != "Windows SendInput vk")

    def _set_shift(self, enabled: bool) -> None:
        if enabled == self.shift_down:
            return
        if enabled:
            self._send_key("shift", True)
        else:
            self._send_key("shift", False)
        self.shift_down = enabled

    def key_down(self, binding: KeyBinding) -> None:
        with self.lock:
            existing = self.active.get(binding.label)
            if existing:
                self.active[binding.label] = (existing[0], existing[1] + 1)
                return

            if binding.shifted:
                self._set_shift(True)
                self._send_key(binding.base_key, True)
            else:
                was_shift_down = self.shift_down
                if was_shift_down:
                    self._set_shift(False)
                self._send_key(binding.base_key, True)
                if was_shift_down:
                    self._set_shift(True)

            self.active[binding.label] = (binding, 1)

    def key_up(self, binding: KeyBinding) -> None:
        with self.lock:
            existing = self.active.get(binding.label)
            if not existing:
                return
            old_binding, count = existing
            if count > 1:
                self.active[binding.label] = (old_binding, count - 1)
                return

            if old_binding.shifted:
                self._send_key(old_binding.base_key, False)
                del self.active[binding.label]
                if not any(active_binding.shifted for active_binding, _ in self.active.values()):
                    self._set_shift(False)
            else:
                was_shift_down = self.shift_down
                if was_shift_down:
                    self._set_shift(False)
                self._send_key(old_binding.base_key, False)
                if was_shift_down:
                    self._set_shift(True)
                del self.active[binding.label]

    def release_all(self) -> None:
        with self.lock:
            for binding, _ in list(self.active.values()):
                try:
                    self._send_key(binding.base_key, False)
                except Exception:
                    pass
            self.active.clear()
            try:
                self._send_key("shift", False)
            except Exception:
                pass
            self.shift_down = False


class PianoMacroApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x760")
        self.minsize(840, 620)
        self._configure_theme()

        self.sender = KeySender()
        self.synth: MidiSynth | None = None
        self.synth_enabled = tk.BooleanVar(value=True)
        self.synth_instrument = tk.StringVar(value="Acoustic Grand Piano")
        self.synth_volume = tk.IntVar(value=100)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.play_thread: threading.Thread | None = None
        self.loaded_midi_actions: list[ScheduledAction] | None = None
        self.loaded_midi_name = tk.StringVar(value="No MIDI loaded")
        self.source_mode = tk.StringVar(value="text")
        self.status = tk.StringVar(value=f"Ready. Focus Roblox, then press {DEFAULT_HOTKEY_PLAY} to play.")
        self.progress = tk.DoubleVar(value=0.0)
        self.song_title = tk.StringVar(value="Untitled Song")
        self.song_artist = tk.StringVar(value="")
        self.song_tags = tk.StringVar(value="")
        self.song_notes = tk.StringVar(value="")
        self.analysis_summary = tk.StringVar(value="Analyze a text score or MIDI to see range, length, and transpose tips.")
        self.library_entries: list[dict[str, object]] = []
        self.current_song_id: str | None = None
        self.last_suggested_transpose: int | None = None
        self.audio_conversion_notes: list[AudioMidiNote] = []
        self.audio_conversion_source: str | None = None
        self.audio_conversion_bpm = 120.0
        self.preview_items: dict[int, list[int]] = {}
        self.preview_rects: dict[int, int] = {}
        self.preview_text: dict[int, list[int]] = {}
        self.preview_is_black: dict[int, bool] = {}
        self.preview_active_notes: set[int] = set()
        self.preview_note_velocities: dict[int, int] = {}
        self.metronome_active = False
        self.scale_root = tk.StringVar(value="None")
        self.scale_mode_var = tk.StringVar(value="major")
        self.scale_highlighted: set[int] = set()
        self.listener = None
        self.pressed_hotkey_parts: set[str] = set()
        self.hotkey_capture_target: tuple[str, tk.StringVar] | None = None
        self.hotkey_capture_block_name: str | None = None
        self.hotkey_capture_block_until = 0.0
        self.play_hotkey = tk.StringVar(value=DEFAULT_HOTKEY_PLAY)
        self.pause_hotkey = tk.StringVar(value=DEFAULT_HOTKEY_PAUSE)
        self.stop_hotkey = tk.StringVar(value=DEFAULT_HOTKEY_STOP)
        self.current_play_hotkey = self._normalize_hotkey(DEFAULT_HOTKEY_PLAY)
        self.current_pause_hotkey = self._normalize_hotkey(DEFAULT_HOTKEY_PAUSE)
        self.current_stop_hotkey = self._normalize_hotkey(DEFAULT_HOTKEY_STOP)
        self.hotkeys_unique = True
        for hotkey_var in (self.play_hotkey, self.pause_hotkey, self.stop_hotkey):
            hotkey_var.trace_add("write", self._on_hotkey_changed)

        self.bpm = tk.DoubleVar(value=120.0)
        self.speed = tk.DoubleVar(value=1.0)
        self.default_beats = tk.DoubleVar(value=1.0)
        self.hold_percent = tk.DoubleVar(value=0.9)
        self.gap_ms = tk.DoubleVar(value=20.0)
        self.start_delay = tk.DoubleVar(value=3.0)
        self.playback_start_offset = tk.DoubleVar(value=0.0)
        self.playback_end_at = tk.DoubleVar(value=0.0)
        self.transpose = tk.IntVar(value=0)
        self.transpose.trace_add("write", lambda *_: self._preview_transpose_on_keyboard())
        self.high_note = tk.StringVar(value="C7")
        self.range_mode = tk.StringVar(value="Auto-fit octaves")
        self.input_method = tk.StringVar(value="Windows SendInput scan")
        self.preview_only = tk.BooleanVar(value=False)
        self.timing_quantize_beats = tk.DoubleVar(value=0.25)
        self.timing_quantize_strength = tk.DoubleVar(value=0.55)
        self.timing_offset_ms = tk.DoubleVar(value=0.0)
        self.timing_auto_offset = tk.BooleanVar(value=True)
        self.timing_min_note_beats = tk.DoubleVar(value=0.10)
        self.timing_max_note_beats = tk.DoubleVar(value=4.0)
        self.timing_gap_ms = tk.DoubleVar(value=12.0)
        self.calibration_note = tk.StringVar(value="C7")
        self.calibration_hold_seconds = tk.DoubleVar(value=1.5)
        self.loop_enabled = tk.BooleanVar(value=False)
        self.loop_start_sec = tk.DoubleVar(value=0.0)
        self.loop_end_sec = tk.DoubleVar(value=0.0)
        self.metronome_enabled = tk.BooleanVar(value=False)
        self.practice_mode = tk.BooleanVar(value=False)
        self.practice_start_speed = tk.DoubleVar(value=0.50)
        self.practice_target_speed = tk.DoubleVar(value=1.0)
        self.practice_increment = tk.DoubleVar(value=0.05)
        self.practice_loops_per_step = tk.IntVar(value=2)
        self.practice_current_speed = 1.0
        self.practice_loop_count = 0
        self.midi_track_filter = tk.StringVar(value="All tracks")
        self.playback_queue: list[str] = []  # list of song IDs
        self.recent_midi_files: list[str] = []
        self.recording = tk.BooleanVar(value=False)
        self.recorded_events: list[tuple[float, str, str]] = []  # (timestamp, "down"/"up", label)
        self.record_start_time = 0.0
        self.settings_loaded = False

        self._load_settings()
        self._load_song_library()
        self._build_ui()
        self.bind_all("<KeyPress>", self._on_tk_hotkey_capture, add="+")
        self.after(100, self.draw_keyboard_preview)
        self.after(150, self.load_latest_song_on_startup)
        self.bind_all("<Control-s>", lambda _e: self.save_current_song())
        self.bind_all("<Control-n>", lambda _e: self.new_song())
        self.bind_all("<Control-o>", lambda _e: self.load_midi())
        self.bind_all("<Control-f>", lambda _e: self._focus_library_search())
        self.bind_all("<Control-g>", lambda _e: self._find_in_editor())
        self.bind_all("<Control-Shift-f>", lambda _e: self.toggle_favorite_song())
        self.bind_all("<Control-Shift-v>", lambda _e: self.import_from_clipboard())
        self.bind_all("<Control-Shift-e>", lambda _e: self.export_as_plain_text())
        self.bind_all("<F1>", lambda _e: self._show_shortcuts())
        self._auto_save_timer = self.after(120000, self._auto_save_tick)
        self.after(200, self._init_synth)
        self._is_dirty = False
        self._setup_drag_drop()
        self._update_title()
        self.score_text.bind("<<Modified>>", self._on_text_modified)
        self.song_title.trace_add("write", lambda *_: self._mark_dirty())
        self.song_artist.trace_add("write", lambda *_: self._mark_dirty())
        self.song_tags.trace_add("write", lambda *_: self._mark_dirty())
        self._start_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Destroy>", self._on_destroy_drag_drop)

    def _mark_dirty(self) -> None:
        if not self._is_dirty:
            self._is_dirty = True
            self._update_title()

    def _mark_clean(self) -> None:
        if self._is_dirty:
            self._is_dirty = False
            self._update_title()

    def _on_text_modified(self, _event: object = None) -> None:
        self.score_text.edit_modified(False)
        self._mark_dirty()

    def _update_title(self) -> None:
        title = f"{'*' if self._is_dirty else ''}{APP_TITLE}"
        song = self.song_title.get().strip() or "Untitled Song"
        if song != "Untitled Song" or self._is_dirty:
            title += f" - {song}"
        self.title(title)

    def _setup_drag_drop(self) -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            ctypes.windll.shell32.DragAcceptFiles(hwnd, True)
            self._drag_drop_wndproc_old = ctypes.windll.user32.SetWindowLongPtrW(
                hwnd, -4, ctypes.cast(self._drag_drop_wndproc, ctypes.c_void_p).value
            )
        except Exception:
            pass

    def _on_destroy_drag_drop(self, _event: object = None) -> None:
        old = getattr(self, "_drag_drop_wndproc_old", None)
        if old:
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                ctypes.windll.user32.SetWindowLongPtrW(hwnd, -4, old)
            except Exception:
                pass

    def _drag_drop_wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == 0x0233:  # WM_DROPFILES
            try:
                count = ctypes.windll.shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                for i in range(count):
                    length = ctypes.windll.shell32.DragQueryFileW(wparam, i, None, 0) + 1
                    buffer = ctypes.create_unicode_buffer(length)
                    ctypes.windll.shell32.DragQueryFileW(wparam, i, buffer, length)
                    filepath = buffer.value
                    self.after(50, lambda p=filepath: self._handle_dropped_file(p))
                ctypes.windll.shell32.DragFinish(wparam)
            except Exception:
                pass
            return 0
        return ctypes.windll.user32.CallWindowProcW(
            self._drag_drop_wndproc_old, hwnd, msg, wparam, lparam
        )

    _drag_drop_wndproc = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint, ctypes.c_longlong, ctypes.c_longlong
    )(_drag_drop_wndproc)

    def _handle_dropped_file(self, path: str) -> None:
        lower = path.lower()
        if lower.endswith((".mid", ".midi")):
            try:
                self.loaded_midi_actions = load_midi_actions(path)
                self.source_mode.set("midi")
                self.loaded_midi_name.set(Path(path).name)
                self.song_title.set(Path(path).stem)
                self.status.set(f"Dropped MIDI: {Path(path).name}")
                self.analyze_current_source(show_popup=False)
            except Exception as exc:
                messagebox.showerror("MIDI load failed", str(exc))
        elif lower.endswith((".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac")):
            self.status.set(f"Dropped audio: {Path(path).name}")
            self.open_audio_to_midi_tool(prefill_path=path)
        elif lower.endswith(SONG_EXPORT_SUFFIX) or lower.endswith(".json"):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict) and "text" in data:
                    data = dict(data)
                    data["id"] = new_song_id()
                    data["created_at"] = now_stamp()
                    data["updated_at"] = now_stamp()
                    self.library_entries.append(data)
                    self._save_song_library()
                    self.refresh_library_list()
                    self._load_song_entry_into_editor(data)
                    self.status.set(f"Imported: {data.get('title', Path(path).name)}")
            except Exception as exc:
                messagebox.showerror("Import failed", str(exc))

    def _focus_library_search(self) -> None:
        if hasattr(self, "library_search_entry"):
            self.library_search_entry.focus_set()

    def _init_synth(self) -> None:
        try:
            self.synth = MidiSynth()
            if self.synth._handle is not None:
                prog = MIDI_INSTRUMENTS.get(self.synth_instrument.get(), 0)
                self.synth.program_change(prog)
                self.status.set("MIDI synth ready. Use sidebar to change instrument.")
        except Exception:
            self.synth = None

    def _on_synth_toggle(self) -> None:
        if not self.synth and self.synth_enabled.get():
            self._init_synth()

    def _on_instrument_change(self, _event: object = None) -> None:
        if self.synth and self.synth._handle is not None:
            prog = MIDI_INSTRUMENTS.get(self.synth_instrument.get(), 0)
            self.synth.program_change(prog)
            self.status.set(f"Instrument: {self.synth_instrument.get()}")

    def _synth_note_on(self, midi_note: int, velocity: int = 100) -> None:
        if self.synth and self.synth_enabled.get() and self.synth._handle:
            vol = max(1, min(127, int(self.synth_volume.get() * velocity / 100)))
            self.synth.note_on(midi_note, vol)

    def _synth_note_off(self, midi_note: int) -> None:
        if self.synth and self.synth_enabled.get() and self.synth._handle:
            self.synth.note_off(midi_note)

    def _metronome_tick(self) -> None:
        if not hasattr(self, "keyboard_canvas"):
            return
        self.metronome_active = not self.metronome_active
        canvas = self.keyboard_canvas
        if self.metronome_active and hasattr(self, "_metro_rect"):
            canvas.itemconfigure(self._metro_rect, fill="#ff6b35")
            self.after(80, lambda: canvas.itemconfigure(self._metro_rect, fill=getattr(self, "_active_field", UI_FIELD)))

    def _draw_metronome_indicator(self) -> None:
        if not hasattr(self, "keyboard_canvas"):
            return
        canvas = self.keyboard_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        self._metro_rect = canvas.create_rectangle(
            w - 24, 4, w - 4, 20,
            fill=getattr(self, "_active_field", UI_FIELD),
            outline=getattr(self, "_active_accent", UI_ACCENT),
            tags="metro",
        )

    def _highlight_scale(self) -> None:
        root_name = self.scale_root.get()
        if root_name == "None":
            self.scale_highlighted.clear()
            self.draw_keyboard_preview()
            return
        try:
            root_midi = note_name_to_midi(root_name) % 12
        except Exception:
            return
        mode = self.scale_mode_var.get()
        interval_map = {
            "major": [0, 2, 4, 5, 7, 9, 11],
            "minor": [0, 2, 3, 5, 7, 8, 10],
            "harmonic minor": [0, 2, 3, 5, 7, 8, 11],
            "melodic minor": [0, 2, 3, 5, 7, 9, 11],
            "pentatonic major": [0, 2, 4, 7, 9],
            "pentatonic minor": [0, 3, 5, 7, 10],
            "blues": [0, 3, 5, 6, 7, 10],
            "chromatic": list(range(12)),
        }
        intervals = interval_map.get(mode, [0, 2, 4, 5, 7, 9, 11])
        pitches = {(root_midi + i) % 12 for i in intervals}
        self.scale_highlighted = {midi for midi in KEY_MAP if midi % 12 in pitches}
        self.draw_keyboard_preview()

    @staticmethod
    def _detect_chord_name(midi_notes: list[int]) -> str:
        if len(midi_notes) < 2:
            return ""
        pcs = sorted(set(n % 12 for n in midi_notes))
        if len(pcs) < 2:
            return ""
        intervals_set = {(p - pcs[0]) % 12 for p in pcs}
        templates = {
            "maj": {0, 4, 7}, "m": {0, 3, 7}, "dim": {0, 3, 6}, "aug": {0, 4, 8},
            "sus2": {0, 2, 7}, "sus4": {0, 5, 7},
            "maj7": {0, 4, 7, 11}, "m7": {0, 3, 7, 10}, "7": {0, 4, 7, 10},
            "dim7": {0, 3, 6, 9}, "m7b5": {0, 3, 6, 10}, "m(maj7)": {0, 3, 7, 11},
            "maj9": {0, 4, 7, 11, 2}, "m9": {0, 3, 7, 10, 2}, "9": {0, 4, 7, 10, 2},
            "6": {0, 4, 7, 9}, "m6": {0, 3, 7, 9},
        }
        best, best_score = "", 0
        for name, tpl in templates.items():
            if tpl.issubset(intervals_set) and len(tpl) > best_score:
                best_score, best = len(tpl), name
        if best and pcs:
            return f"{['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][pcs[0]]}{best}"
        return ""

    def _auto_save_tick(self) -> None:
        if self._is_dirty and getattr(self, "current_song_id", None):
            try:
                self.save_current_song()
                self.status.set("Auto-saved.")
            except Exception:
                pass
        self._auto_save_timer = self.after(120000, self._auto_save_tick)

    def _find_in_editor(self) -> None:
        if not hasattr(self, "score_text"):
            return
        dialog = tk.Toplevel(self)
        dialog.title("Find in Editor")
        dialog.geometry("380x120")
        dialog.configure(bg=getattr(self, "_active_bg", UI_BG))
        dialog.transient(self)
        dialog.resizable(False, False)
        find_var = tk.StringVar()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Find:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(frame, textvariable=find_var, width=30)
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        entry.focus_set()

        def do_find() -> None:
            query = find_var.get()
            if not query:
                return
            content = self.score_text.get("1.0", tk.END)
            start = self.score_text.search(query, "insert", tk.END, nocase=True)
            if start:
                self.score_text.tag_remove("find_highlight", "1.0", tk.END)
                self.score_text.tag_configure("find_highlight", background="#365880")
                end = f"{start}+{len(query)}c"
                self.score_text.tag_add("find_highlight", start, end)
                self.score_text.mark_set("insert", end)
                self.score_text.see(start)
            else:
                self.score_text.tag_remove("find_highlight", "1.0", tk.END)
                self.status.set(f"Not found: {query}")

        def find_next() -> None:
            query = find_var.get()
            if not query:
                return
            start = self.score_text.search(query, "insert", tk.END, nocase=True)
            if start:
                self.score_text.tag_remove("find_highlight", "1.0", tk.END)
                self.score_text.tag_configure("find_highlight", background="#365880")
                end = f"{start}+{len(query)}c"
                self.score_text.tag_add("find_highlight", start, end)
                self.score_text.mark_set("insert", end)
                self.score_text.see(start)

        entry.bind("<Return>", lambda _e: do_find())
        entry.bind("<Control-g>", lambda _e: find_next())
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(btn_frame, text="Find", command=do_find, style="Accent.TButton").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Next", command=find_next).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Close", command=dialog.destroy).pack(side=tk.LEFT)

    def _show_shortcuts(self) -> None:
        text = (
            "Keyboard Shortcuts:\n\n"
            "Ctrl+S          Save song\n"
            "Ctrl+N          New song\n"
            "Ctrl+O          Load MIDI file\n"
            "Ctrl+F          Focus library search\n"
            "Ctrl+G          Find in text editor\n"
            "Ctrl+Shift+F    Toggle favorite\n"
            "Ctrl+Shift+V    Paste from clipboard\n"
            "Ctrl+Shift+E    Export as plain text\n"
            "F1              Show this help\n"
            "F6              Play (customizable)\n"
            "F7              Pause/Resume (customizable)\n"
            "F8              Stop (customizable)\n\n"
            "Double-click library entry to load.\n"
            "Right-click library entry for menu\n"
            "  (Load, Duplicate, Queue, Star, Export, Delete).\n"
            "Drop .mid/.wav/.mp3 files onto the window.\n"
            "Drop .jjspiano.json files to import.\n"
            "Tap Tempo button: click 3+ times to set BPM.\n"
            "Loop: enable and set start/end in sidebar."
        )
        messagebox.showinfo("Keyboard Shortcuts", text)

    def _configure_theme(self) -> None:
        self._is_dark_theme = getattr(self, "_is_dark_theme", True)
        if self._is_dark_theme:
            bg, surface, surface_hover, field, border = UI_BG, UI_SURFACE, UI_SURFACE_HOVER, UI_FIELD, UI_BORDER
            text, muted, accent, accent_dark, accent_text = UI_TEXT, UI_MUTED, UI_ACCENT, UI_ACCENT_DARK, UI_ACCENT_TEXT
            danger, danger_dark, selection = UI_DANGER, UI_DANGER_DARK, UI_SELECTION
        else:
            bg, surface, surface_hover = "#f0f2f5", "#ffffff", "#e4e6eb"
            field, border = "#ffffff", "#ccd0d5"
            text, muted = "#1c1e21", "#606770"
            accent, accent_dark, accent_text = "#1877f2", "#166fe5", "#ffffff"
            danger, danger_dark, selection = "#e41e3f", "#c41a34", "#d2e3fc"

        self.configure(bg=bg)
        self.option_add("*Font", UI_FONT)
        self.option_add("*Menu.background", surface)
        self.option_add("*Menu.foreground", text)
        self.option_add("*Menu.activeBackground", surface_hover)
        self.option_add("*Menu.activeForeground", text)

        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        style = self.style
        style.configure(".", background=bg, foreground=text, font=UI_FONT)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Title.TLabel", background=bg, foreground=text, font=UI_TITLE_FONT)
        style.configure("Section.TLabel", background=bg, foreground=text, font=UI_SECTION_FONT)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Status.TLabel", background=bg, foreground=accent)

        button_options = {
            "background": surface,
            "foreground": text,
            "bordercolor": border,
            "lightcolor": surface,
            "darkcolor": surface,
            "focuscolor": accent,
            "padding": (11, 7),
            "relief": tk.FLAT,
        }
        style.configure("TButton", **button_options)
        style.map("TButton", background=[("pressed", field), ("active", surface_hover)],
                   foreground=[("disabled", muted), ("active", text)],
                   bordercolor=[("focus", accent), ("active", accent)])

        style.configure("Accent.TButton", background=accent, foreground=accent_text,
                         bordercolor=accent, lightcolor=accent, darkcolor=accent_dark,
                         focuscolor=accent, padding=(12, 7), relief=tk.FLAT)
        style.map("Accent.TButton", background=[("pressed", accent_dark), ("active", accent)],
                   foreground=[("disabled", muted), ("active", accent_text)])

        style.configure("Danger.TButton", background=danger_dark, foreground=text,
                         bordercolor=danger, lightcolor=danger_dark, darkcolor=danger_dark,
                         focuscolor=danger, padding=(11, 7), relief=tk.FLAT)
        style.map("Danger.TButton", background=[("pressed", danger_dark), ("active", danger)],
                   foreground=[("disabled", muted), ("active", text)])

        field_options = {"fieldbackground": field, "background": field, "foreground": text,
                         "bordercolor": border, "lightcolor": field, "darkcolor": field, "padding": 5}
        for sn in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(sn, **field_options)
            style.map(sn, fieldbackground=[("readonly", field), ("focus", field)],
                      background=[("readonly", field), ("focus", field)],
                      foreground=[("readonly", text), ("disabled", muted)],
                      bordercolor=[("focus", accent), ("active", accent)])
        style.configure("TCombobox", arrowcolor=muted, arrowsize=14)
        style.map("TCombobox", arrowcolor=[("active", accent), ("pressed", accent)])

        style.configure("TCheckbutton", background=bg, foreground=text, focuscolor=accent, padding=(0, 3))
        style.map("TCheckbutton", foreground=[("disabled", muted), ("active", text)], background=[("active", bg)])

        style.configure("Horizontal.TProgressbar", troughcolor=field, background=accent, bordercolor=border)
        style.configure("TSeparator", background=border)
        style.configure("TPanedwindow", background=bg)
        style.configure("TScrollbar", background=surface, troughcolor=field, bordercolor=bg, arrowcolor=muted)
        style.map("TScrollbar", background=[("active", surface_hover)], arrowcolor=[("active", text)])
        style.configure("Horizontal.TScale", background=bg, troughcolor=field, bordercolor=border)

        tree_opts = {"background": field, "foreground": text, "fieldbackground": field,
                     "bordercolor": border, "rowheight": 27}
        style.configure("Treeview", **tree_opts)
        style.configure("Treeview.Heading", background=surface, foreground=text, bordercolor=border,
                        font=UI_SECTION_FONT, padding=(6, 6))
        style.map("Treeview", background=[("selected", accent_dark)], foreground=[("selected", text)])

        style.configure("TNotebook", background=bg, bordercolor=border, tabmargins=(2, 2, 2, 0))
        style.configure("TNotebook.Tab", background=surface, foreground=text, bordercolor=border,
                        padding=(12, 5), font=UI_FONT)
        style.map("TNotebook.Tab", background=[("selected", field), ("active", surface_hover)],
                  foreground=[("selected", accent), ("active", text)])

        self._active_bg = bg
        self._active_fg = text
        self._active_field = field
        self._active_accent = accent
        self._active_selection = selection
        self._active_border = border
        self._active_key_white = "#f5f6f7" if not self._is_dark_theme else UI_KEY_WHITE
        self._active_key_white_active = "#54c8ff" if not self._is_dark_theme else UI_KEY_WHITE_ACTIVE
        self._active_key_black = "#1c1e21" if not self._is_dark_theme else UI_KEY_BLACK
        self._active_key_black_active = "#1877f2" if not self._is_dark_theme else UI_KEY_BLACK_ACTIVE
        self._active_key_outline = "#ccd0d5" if not self._is_dark_theme else UI_KEY_OUTLINE

    def toggle_theme(self) -> None:
        self._is_dark_theme = not getattr(self, "_is_dark_theme", True)
        self._configure_theme()
        if hasattr(self, "score_text"):
            self._style_text_widget(self.score_text)
        if hasattr(self, "library_listbox"):
            self._style_listbox(self.library_listbox)
        if hasattr(self, "keyboard_canvas"):
            self.draw_keyboard_preview()
        self.status.set(f"Theme: {'Dark' if self._is_dark_theme else 'Light'}")

    def _style_text_widget(self, widget: tk.Text) -> None:
        widget.configure(
            bg=getattr(self, "_active_field", UI_FIELD),
            fg=getattr(self, "_active_fg", UI_TEXT),
            insertbackground=getattr(self, "_active_accent", UI_ACCENT),
            selectbackground=getattr(self, "_active_selection", UI_SELECTION),
            selectforeground=getattr(self, "_active_fg", UI_TEXT),
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=getattr(self, "_active_border", UI_BORDER),
            highlightcolor=getattr(self, "_active_accent", UI_ACCENT),
            padx=10,
            pady=10,
        )

    def _style_listbox(self, widget: tk.Listbox) -> None:
        widget.configure(
            bg=getattr(self, "_active_field", UI_FIELD),
            fg=getattr(self, "_active_fg", UI_TEXT),
            selectbackground=UI_ACCENT_DARK,
            selectforeground=UI_TEXT,
            activestyle="none",
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=getattr(self, "_active_border", UI_BORDER),
            highlightcolor=getattr(self, "_active_accent", UI_ACCENT),
            font=("Segoe UI", 10),
        )

    @staticmethod
    def _add_tooltip(widget: tk.Widget, text: str) -> None:
        def show(_event: tk.Event) -> None:
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{widget.winfo_rootx() + widget.winfo_width() // 2}+{widget.winfo_rooty() - 28}")
            label = tk.Label(
                tip,
                text=text,
                bg=UI_SURFACE_HOVER,
                fg=UI_TEXT,
                font=("Segoe UI", 9),
                relief=tk.FLAT,
                padx=8,
                pady=3,
            )
            label.pack()
            widget._tip = tip

        def hide(_event: tk.Event) -> None:
            if hasattr(widget, "_tip"):
                widget._tip.destroy()
                del widget._tip

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _scroll_canvas_with_mousewheel(self, canvas: tk.Canvas, event: tk.Event) -> str:
        event_num = getattr(event, "num", None)
        if event_num == 4:
            units = -3
        elif event_num == 5:
            units = 3
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) if delta else 0
            if units == 0 and delta:
                units = -1 if delta > 0 else 1
        if units:
            canvas.yview_scroll(units, "units")
        return "break"

    def _bind_mousewheel_to_canvas(self, widget: tk.Widget, canvas: tk.Canvas) -> None:
        widget.bind("<MouseWheel>", lambda event: self._scroll_canvas_with_mousewheel(canvas, event))
        widget.bind("<Button-4>", lambda event: self._scroll_canvas_with_mousewheel(canvas, event))
        widget.bind("<Button-5>", lambda event: self._scroll_canvas_with_mousewheel(canvas, event))
        for child in widget.winfo_children():
            self._bind_mousewheel_to_canvas(child, canvas)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        title_row = ttk.Frame(root)
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.columnconfigure(0, weight=1)
        ttk.Label(title_row, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(title_row, textvariable=self.status, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

        control_bar = ttk.Frame(root)
        control_bar.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        for column in range(14):
            control_bar.columnconfigure(column, weight=0)
        control_bar.columnconfigure(13, weight=1)

        play_btn = ttk.Button(control_bar, text="Play", command=self.start_playback, style="Accent.TButton")
        play_btn.grid(row=0, column=0, padx=(0, 6))
        self._add_tooltip(play_btn, "Send keystrokes to Roblox (F6)")

        preview_btn = ttk.Button(control_bar, text="Preview", command=self.start_preview_playback)
        preview_btn.grid(row=0, column=1, padx=6)
        self._add_tooltip(preview_btn, "Play with keyboard highlight only, no keystrokes")

        pause_btn = ttk.Button(control_bar, text="Pause", command=self.toggle_pause)
        pause_btn.grid(row=0, column=2, padx=6)
        self._add_tooltip(pause_btn, "Pause/resume playback (F7)")

        stop_btn = ttk.Button(control_bar, text="Stop", command=self.stop_playback, style="Danger.TButton")
        stop_btn.grid(row=0, column=3, padx=6)
        self._add_tooltip(stop_btn, "Stop playback immediately (F8)")

        midi_btn = ttk.Button(control_bar, text="Load MIDI", command=self.load_midi)
        midi_btn.grid(row=0, column=4, padx=6)
        midi_btn.bind("<Button-3>", self._show_recent_midi_menu)
        self._add_tooltip(midi_btn, "Import a .mid file (Ctrl+O). Right-click for recent.")

        audio_btn = ttk.Button(control_bar, text="Audio to MIDI", command=self.open_audio_to_midi_tool)
        audio_btn.grid(row=0, column=5, padx=6)
        self._add_tooltip(audio_btn, "Convert WAV/MP3/FLAC to MIDI with AI")

        search_btn = ttk.Button(control_bar, text="Online Search", command=self.open_online_midi_search_tool)
        search_btn.grid(row=0, column=6, padx=6)
        self._add_tooltip(search_btn, "Search & download from Online Sequencer")

        self.record_btn = ttk.Button(control_bar, text="Record", command=self.toggle_recording)
        self.record_btn.grid(row=0, column=7, padx=6)
        self._add_tooltip(self.record_btn, "Start/stop recording keystrokes as a score")

        ttk.Label(control_bar, textvariable=self.loaded_midi_name, style="Muted.TLabel").grid(
            row=0, column=8, columnspan=6, sticky="e"
        )

        paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        paned.grid(row=2, column=0, sticky="nsew")

        library_frame = ttk.Frame(paned, padding=(0, 8, 14, 0))
        library_frame.columnconfigure(0, weight=1)
        library_frame.rowconfigure(1, weight=1)
        self._build_library_panel(library_frame)
        paned.add(library_frame, weight=1)

        editor_frame = ttk.Frame(paned, padding=(0, 8, 14, 0))
        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(2, weight=1)
        paned.add(editor_frame, weight=3)

        metadata = ttk.Frame(editor_frame)
        metadata.grid(row=0, column=0, sticky="ew")
        metadata.columnconfigure(1, weight=2)
        metadata.columnconfigure(3, weight=1)
        metadata.columnconfigure(5, weight=1)
        ttk.Label(metadata, text="Title").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(metadata, textvariable=self.song_title).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(metadata, text="Artist").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(metadata, textvariable=self.song_artist).grid(row=0, column=3, sticky="ew", padx=(0, 10))
        ttk.Label(metadata, text="Tags").grid(row=0, column=4, sticky="w", padx=(0, 6))
        ttk.Entry(metadata, textvariable=self.song_tags).grid(row=0, column=5, sticky="ew")
        ttk.Label(metadata, text="Notes").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(metadata, textvariable=self.song_notes).grid(
            row=1, column=1, columnspan=5, sticky="ew", pady=(6, 0)
        )

        editor_actions = ttk.Frame(editor_frame)
        editor_actions.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(editor_actions, text="Song text", style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Button(editor_actions, text="Analyze", command=self.analyze_current_source).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(editor_actions, text="Timeline", command=self.open_timeline_editor).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(editor_actions, text="Piano Roll", command=self.open_piano_roll_editor).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(editor_actions, text="Auto Cleanup MIDI", command=self.cleanup_loaded_midi_playability).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(editor_actions, text="MIDI to Text", command=self.convert_loaded_midi_to_text).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(editor_actions, text="Save Song", command=self.save_current_song, style="Accent.TButton").pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        self.queue_status = tk.StringVar(value="")
        ttk.Label(editor_actions, textvariable=self.queue_status, style="Muted.TLabel").pack(
            side=tk.RIGHT, padx=(12, 0)
        )

        self.score_text = tk.Text(editor_frame, wrap=tk.WORD, undo=True, font=("Consolas", 11))
        self.score_text.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        self._style_text_widget(self.score_text)
        self.score_text.insert(
            "1.0",
            "C4 D4 E4 F4 G4 A4 B4 C5\n"
            "[C4 E4 G4]:2 R:0.5 [D4 F4 A4]:2\n\n"
            "// Load your own MIDI for songs like Unravel, or paste a note chart here.\n",
        )

        keyboard_frame = ttk.Frame(editor_frame)
        keyboard_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        keyboard_frame.columnconfigure(0, weight=1)
        ttk.Label(keyboard_frame, text="Preview keyboard", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.keyboard_canvas = tk.Canvas(
            keyboard_frame,
            height=132,
            bg=getattr(self, "_active_field", UI_FIELD),
            highlightthickness=1,
            highlightbackground=getattr(self, "_active_border", UI_BORDER),
            highlightcolor=getattr(self, "_active_accent", UI_ACCENT),
        )
        self.keyboard_canvas.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.keyboard_canvas.bind("<Configure>", lambda _event: self.draw_keyboard_preview())

        scale_row = ttk.Frame(editor_frame)
        scale_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(scale_row, text="Scale:").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Combobox(scale_row, textvariable=self.scale_root, width=8, state="readonly",
                     values=["None"] + [midi_to_note_name(n) for n in range(note_name_to_midi("C2"), note_name_to_midi("C5") + 1) if "#" not in midi_to_note_name(n)]
                     ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Combobox(scale_row, textvariable=self.scale_mode_var, width=8, state="readonly",
                     values=["major", "minor", "harmonic minor", "melodic minor", "pentatonic major", "pentatonic minor", "blues", "chromatic"]
                     ).pack(side=tk.LEFT)
        ttk.Button(scale_row, text="Highlight", command=self._highlight_scale).pack(side=tk.LEFT, padx=(6, 0))
        self.scale_root.trace_add("write", lambda *_: self._highlight_scale())
        self.scale_mode_var.trace_add("write", lambda *_: self._highlight_scale())

        side_container = ttk.Frame(paned, padding=(14, 8, 0, 0))
        side_container.columnconfigure(0, weight=1)
        side_container.rowconfigure(0, weight=1)
        side_canvas = tk.Canvas(side_container, bg=getattr(self, "_active_bg", UI_BG), highlightthickness=0, borderwidth=0)
        side_scrollbar = ttk.Scrollbar(side_container, orient=tk.VERTICAL, command=side_canvas.yview)
        side_canvas.configure(yscrollcommand=side_scrollbar.set)
        side_canvas.grid(row=0, column=0, sticky="nsew")
        side_scrollbar.grid(row=0, column=1, sticky="ns")
        side = ttk.Frame(side_canvas, padding=(0, 0, 12, 0))
        side.columnconfigure(1, weight=1)
        side_window = side_canvas.create_window((0, 0), window=side, anchor="nw")
        side.bind("<Configure>", lambda _event: side_canvas.configure(scrollregion=side_canvas.bbox("all")))
        side_canvas.bind("<Configure>", lambda event: side_canvas.itemconfigure(side_window, width=event.width))
        paned.add(side_container, weight=1)

        row = 0
        ttk.Label(side, text="Playback", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self._spin(side, row, "BPM", self.bpm, 20, 320, 1)
        row += 1
        ttk.Button(side, text="Tap Tempo", command=self._tap_tempo).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=4
        )
        row += 1
        self._spin(side, row, "Speed", self.speed, 0.25, 3.0, 0.05)
        row += 1
        self._spin(side, row, "Default beats", self.default_beats, 0.05, 8.0, 0.05)
        row += 1
        self._spin(side, row, "Hold %", self.hold_percent, 0.1, 1.0, 0.05)
        row += 1
        self._spin(side, row, "Gap ms", self.gap_ms, 0, 250, 5)
        row += 1
        self._spin(side, row, "Start delay", self.start_delay, 0, 15, 0.5)
        row += 1
        self._spin(side, row, "Start at sec", self.playback_start_offset, 0, 3600, 0.5)
        row += 1
        self._spin(side, row, "End at sec", self.playback_end_at, 0, 3600, 0.5)
        row += 1
        self._spin(side, row, "Transpose", self.transpose, -36, 36, 1)
        row += 1

        ttk.Label(side, text="Highest key").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(side, textvariable=self.high_note, values=["C6", "C7"], state="readonly", width=18).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        row += 1
        ttk.Label(side, text="Range handling").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            side,
            textvariable=self.range_mode,
            values=["Auto-fit octaves", "Skip out-of-range", "Stop with error"],
            state="readonly",
            width=18,
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(side, text="Input method").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            side,
            textvariable=self.input_method,
            values=["Windows SendInput scan", "Windows SendInput vk", "Windows keybd_event", "PyAutoGUI"],
            state="readonly",
            width=24,
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Checkbutton(side, text="Preview only", variable=self.preview_only).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        ttk.Checkbutton(side, text="Loop playback", variable=self.loop_enabled).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        ttk.Checkbutton(side, text="Metronome", variable=self.metronome_enabled).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        self._spin(side, row, "Loop start (sec)", self.loop_start_sec, 0, 3600, 0.5)
        row += 1
        self._spin(side, row, "Loop end (sec)", self.loop_end_sec, 0, 3600, 0.5)
        row += 1

        ttk.Checkbutton(side, text="MIDI Synth output", variable=self.synth_enabled,
                         command=self._on_synth_toggle).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        ttk.Label(side, text="Instrument").grid(row=row, column=0, sticky="w", pady=4)
        inst_combo = ttk.Combobox(
            side, textvariable=self.synth_instrument,
            values=list(MIDI_INSTRUMENTS.keys()), state="readonly", width=22,
        )
        inst_combo.grid(row=row, column=1, sticky="ew", pady=4)
        inst_combo.bind("<<ComboboxSelected>>", self._on_instrument_change)
        row += 1
        self._spin(side, row, "Synth volume", self.synth_volume, 10, 127, 5)
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Practice Mode", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Checkbutton(side, text="Enable speed trainer", variable=self.practice_mode).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        self._spin(side, row, "Start speed", self.practice_start_speed, 0.10, 1.0, 0.05)
        row += 1
        self._spin(side, row, "Target speed", self.practice_target_speed, 0.25, 3.0, 0.05)
        row += 1
        self._spin(side, row, "Speed increment", self.practice_increment, 0.01, 0.25, 0.01)
        row += 1
        self._spin(side, row, "Loops per step", self.practice_loops_per_step, 1, 10, 1)
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Analysis", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(side, textvariable=self.analysis_summary, wraplength=270, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="ew"
        )
        row += 1
        ttk.Button(side, text="Apply Suggested Transpose", command=self.apply_suggested_transpose).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Timing Repair", style="Section.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self._spin(side, row, "Grid beats", self.timing_quantize_beats, 0.0, 1.0, 0.125)
        row += 1
        self._spin(side, row, "Grid strength", self.timing_quantize_strength, 0.0, 1.0, 0.05)
        row += 1
        self._spin(side, row, "Offset ms", self.timing_offset_ms, -1000, 1000, 10)
        row += 1
        ttk.Checkbutton(side, text="Auto timing offset", variable=self.timing_auto_offset).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        self._spin(side, row, "Min note beats", self.timing_min_note_beats, 0.02, 2.0, 0.02)
        row += 1
        self._spin(side, row, "Max note beats", self.timing_max_note_beats, 0.1, 16.0, 0.25)
        row += 1
        self._spin(side, row, "Gap ms", self.timing_gap_ms, 0, 100, 2)
        row += 1
        ttk.Button(side, text="Tighten Loaded MIDI", command=self.tighten_loaded_midi_timing).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Calibration", style="Section.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        ttk.Label(side, text="Test note").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(side, textvariable=self.calibration_note, values=NOTE_OPTIONS, state="readonly", width=18).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        row += 1
        self._spin(side, row, "Hold seconds", self.calibration_hold_seconds, 0.2, 5.0, 0.1)
        row += 1
        calibration_buttons = ttk.Frame(side)
        calibration_buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        calibration_buttons.columnconfigure(0, weight=1)
        calibration_buttons.columnconfigure(1, weight=1)
        calibration_buttons.columnconfigure(2, weight=1)
        ttk.Button(calibration_buttons, text="Test selected", command=self.test_selected_note).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(calibration_buttons, text="Test C6", command=lambda: self.test_note("C6")).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        ttk.Button(calibration_buttons, text="Test C7", command=lambda: self.test_note("C7")).grid(
            row=0, column=2, sticky="ew", padx=(4, 0)
        )
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Hotkeys", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(side, text="Play").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Button(side, textvariable=self.play_hotkey, command=lambda: self.begin_hotkey_capture("Play", self.play_hotkey)).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        row += 1
        ttk.Label(side, text="Pause").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Button(side, textvariable=self.pause_hotkey, command=lambda: self.begin_hotkey_capture("Pause", self.pause_hotkey)).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        row += 1
        ttk.Label(side, text="Stop").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Button(side, textvariable=self.stop_hotkey, command=lambda: self.begin_hotkey_capture("Stop", self.stop_hotkey)).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(side, text="Text format", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        help_text = (
            "Notes: C4 D#4 Bb3\n"
            "Durations: C4:0.5 G4:2\n"
            "Rests: R:1 or -:0.5\n"
            "Chords: [C4 E4 G4]:2\n"
            "Raw keys: key:q key:Q key:!"
        )
        ttk.Label(side, text=help_text, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Separator(side).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Button(side, text="Toggle Light/Dark Theme", command=self.toggle_theme).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=4
        )
        row += 1

        ttk.Progressbar(root, variable=self.progress, maximum=100).grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._bind_mousewheel_to_canvas(side_container, side_canvas)

    def _build_library_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Song Library", style="Section.TLabel").grid(row=0, column=0, sticky="w")

        self.library_search_var = tk.StringVar(value="")
        self.library_search_var.trace_add("write", lambda *_: self.refresh_library_list())
        search_frame = ttk.Frame(parent)
        search_frame.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        search_frame.columnconfigure(0, weight=1)
        self.library_search_entry = ttk.Entry(search_frame, textvariable=self.library_search_var)
        self.library_search_entry.grid(row=0, column=0, sticky="ew")
        self.library_search_entry.bind("<Escape>", lambda _e: self.library_search_var.set(""))

        self.library_show_favorites_var = tk.BooleanVar(value=False)
        self.library_show_favorites_var.trace_add("write", lambda *_: self.refresh_library_list())
        ttk.Checkbutton(
            search_frame, text="Favorites", variable=self.library_show_favorites_var
        ).grid(row=0, column=1, padx=(6, 0))

        self.library_sort_var = tk.StringVar(value="Date")
        ttk.Combobox(
            search_frame,
            textvariable=self.library_sort_var,
            values=["Date", "Title A-Z", "Artist A-Z", "Most Played"],
            state="readonly",
            width=14,
        ).grid(row=0, column=2, padx=(6, 0))
        self.library_sort_var.trace_add("write", lambda *_: self.refresh_library_list())

        list_frame = ttk.Frame(parent)
        list_frame.grid(row=2, column=0, sticky="nsew", pady=(4, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.library_listbox = tk.Listbox(list_frame, height=12, activestyle="dotbox", exportselection=False)
        self.library_listbox.grid(row=0, column=0, sticky="nsew")
        self._style_listbox(self.library_listbox)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.library_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.library_listbox.configure(yscrollcommand=scrollbar.set)
        self.library_listbox.bind("<Double-Button-1>", lambda _event: self.load_selected_song())
        self.library_listbox.bind("<Button-3>", self._show_library_context_menu)

        button_grid = ttk.Frame(parent)
        button_grid.grid(row=3, column=0, sticky="ew")
        button_grid.columnconfigure(0, weight=1)
        button_grid.columnconfigure(1, weight=1)
        ttk.Button(button_grid, text="New", command=self.new_song).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(button_grid, text="Load", command=self.load_selected_song).grid(row=0, column=1, sticky="ew")
        ttk.Button(button_grid, text="Save", command=self.save_current_song, style="Accent.TButton").grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=(6, 0)
        )
        ttk.Button(button_grid, text="Duplicate", command=self.duplicate_selected_song).grid(
            row=1, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Button(button_grid, text="Import", command=self.import_song).grid(
            row=2, column=0, sticky="ew", padx=(0, 4), pady=(6, 0)
        )
        ttk.Button(button_grid, text="Export", command=self.export_current_song).grid(
            row=2, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Button(button_grid, text="Batch Import", command=self.import_multiple_songs).grid(
            row=3, column=0, sticky="ew", padx=(0, 4), pady=(6, 0)
        )
        ttk.Button(button_grid, text="Export All", command=self.export_all_songs).grid(
            row=3, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Button(parent, text="Toggle Favorite", command=self.toggle_favorite_song).grid(
            row=4, column=0, sticky="ew", pady=(6, 0)
        )
        ttk.Button(parent, text="Delete Selected", command=self.delete_selected_song, style="Danger.TButton").grid(
            row=5, column=0, sticky="ew", pady=(6, 0)
        )
        queue_grid = ttk.Frame(parent)
        queue_grid.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        queue_grid.columnconfigure(0, weight=1)
        queue_grid.columnconfigure(1, weight=1)
        ttk.Button(queue_grid, text="Play Queue", command=self.play_queue, style="Accent.TButton").grid(
            row=0, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(queue_grid, text="Clear Queue", command=self.clear_queue).grid(
            row=0, column=1, sticky="ew", padx=(3, 0)
        )

        self.recent_label = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.recent_label, wraplength=180, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=7, column=0, sticky="ew", pady=(8, 0)
        )

        self.library_hint = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.library_hint, wraplength=180, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=8, column=0, sticky="ew", pady=(5, 0)
        )
        self.library_stats = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.library_stats, wraplength=180, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=9, column=0, sticky="ew", pady=(2, 0)
        )
        self.refresh_library_list()
        self._update_recent_label()

    def draw_keyboard_preview(self) -> None:
        if not hasattr(self, "keyboard_canvas"):
            return
        canvas: tk.Canvas = self.keyboard_canvas
        canvas.delete("all")
        self.preview_items.clear()
        self.preview_rects.clear()
        self.preview_text.clear()
        self.preview_is_black.clear()

        width = max(600, canvas.winfo_width())
        height = max(110, canvas.winfo_height())
        white_height = height - 18
        black_height = int(white_height * 0.62)
        white_width = width / len(WHITE_MIDI_NOTES)
        white_positions: dict[int, int] = {midi_note: index for index, midi_note in enumerate(WHITE_MIDI_NOTES)}

        for index, midi_note in enumerate(WHITE_MIDI_NOTES):
            x0 = index * white_width
            x1 = (index + 1) * white_width
            in_scale = not self.scale_highlighted or midi_note in self.scale_highlighted
            if midi_note in self.preview_active_notes:
                fill = getattr(self, "_active_key_white_active", UI_KEY_WHITE_ACTIVE)
            elif not in_scale:
                fill = "#1a1f26"
            else:
                fill = getattr(self, "_active_key_white", UI_KEY_WHITE)
            rect = canvas.create_rectangle(x0, 0, x1, white_height, fill=fill, outline=getattr(self, "_active_key_outline", UI_KEY_OUTLINE))
            binding = KEY_MAP[midi_note]
            note_text = canvas.create_text(
                (x0 + x1) / 2,
                white_height - 26,
                text=midi_to_note_name(midi_note),
                fill="#3b4550",
                font=("Segoe UI", 7),
            )
            key_text = canvas.create_text(
                (x0 + x1) / 2,
                white_height - 10,
                text=binding.label,
                fill="#657180",
                font=("Segoe UI", 8, "bold"),
            )
            self.preview_items[midi_note] = [rect, note_text, key_text]
            self.preview_rects[midi_note] = rect
            self.preview_text[midi_note] = [note_text, key_text]
            self.preview_is_black[midi_note] = False

        for midi_note in BLACK_MIDI_NOTES:
            previous_white = midi_note - 1
            while previous_white not in white_positions and previous_white >= min(WHITE_MIDI_NOTES):
                previous_white -= 1
            if previous_white not in white_positions:
                continue
            index = white_positions[previous_white]
            black_width = white_width * 0.62
            center = (index + 1) * white_width
            x0 = center - black_width / 2
            x1 = center + black_width / 2
            fill = getattr(self, "_active_key_black_active", UI_KEY_BLACK_ACTIVE) if midi_note in self.preview_active_notes else getattr(self, "_active_key_black", UI_KEY_BLACK)
            in_scale = not self.scale_highlighted or midi_note in self.scale_highlighted
            if not in_scale and midi_note not in self.preview_active_notes:
                fill = "#0a0d12"
            rect = canvas.create_rectangle(x0, 0, x1, black_height, fill=fill, outline="#020409")
            binding = KEY_MAP[midi_note]
            key_text = canvas.create_text(
                (x0 + x1) / 2,
                black_height - 16,
                text=binding.label,
                fill="#ffffff",
                font=("Segoe UI", 8, "bold"),
            )
            self.preview_items[midi_note] = [rect, key_text]
            self.preview_rects[midi_note] = rect
            self.preview_text[midi_note] = [key_text]
            self.preview_is_black[midi_note] = True

        canvas.create_text(
            8,
            height - 8,
            anchor="sw",
            text="C2",
            fill=UI_MUTED,
            font=("Segoe UI", 8, "bold"),
        )
        canvas.create_text(
            width - 8,
            height - 8,
            anchor="se",
            text="C7",
            fill=UI_MUTED,
            font=("Segoe UI", 8, "bold"),
        )
        self._draw_metronome_indicator()

    def _apply_keyboard_highlights(self) -> None:
        if not hasattr(self, "keyboard_canvas"):
            return
        kw = getattr(self, "_active_key_white", UI_KEY_WHITE)
        kwa = getattr(self, "_active_key_white_active", UI_KEY_WHITE_ACTIVE)
        kb = getattr(self, "_active_key_black", UI_KEY_BLACK)
        kba = getattr(self, "_active_key_black_active", UI_KEY_BLACK_ACTIVE)
        for midi_note, rect in self.preview_rects.items():
            active = midi_note in self.preview_active_notes
            is_black = self.preview_is_black.get(midi_note, False)
            vel = self.preview_note_velocities.get(midi_note, 100) / 127.0
            if active:
                if is_black:
                    r = min(255, int(40 + vel * 215))
                    g = min(255, int(189 + vel * 66))
                    b = min(255, int(253 - vel * 60))
                    fill = f"#{r:02x}{g:02x}{b:02x}"
                else:
                    r = min(255, int(135 + vel * 120))
                    g = min(255, int(220 - vel * 40))
                    b = min(255, int(255 - vel * 30))
                    fill = f"#{r:02x}{g:02x}{b:02x}"
            else:
                fill = kb if is_black else kw
            self.keyboard_canvas.itemconfigure(rect, fill=fill)

    def highlight_midi_notes(self, notes: set[int], add: bool = False) -> None:
        self.after(0, lambda: self._highlight_midi_notes(notes, add=add))

    def _highlight_midi_notes(self, notes: set[int], add: bool = False) -> None:
        if add:
            self.preview_active_notes.update(notes)
        else:
            self.preview_active_notes = set(notes)
        self._apply_keyboard_highlights()

    def unhighlight_midi_notes(self, notes: set[int]) -> None:
        self.after(0, lambda: self._unhighlight_midi_notes(notes))

    def _unhighlight_midi_notes(self, notes: set[int]) -> None:
        self.preview_active_notes.difference_update(notes)
        self._apply_keyboard_highlights()

    def clear_keyboard_highlights(self) -> None:
        self.after(0, self._clear_keyboard_highlights)

    def _clear_keyboard_highlights(self) -> None:
        self.preview_active_notes.clear()
        self._apply_keyboard_highlights()

    def _spin(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable, low: float, high: float, step: float) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Spinbox(parent, textvariable=variable, from_=low, to=high, increment=step, width=20).grid(
            row=row, column=1, sticky="ew", pady=4
        )

    def _default_library_entries(self) -> list[dict[str, object]]:
        created = now_stamp()
        return [
            {
                "id": new_song_id(),
                "title": "C Scale Warmup",
                "artist": "Studio",
                "tags": "warmup,test",
                "notes": "Use this to confirm Roblox focus and timing.",
                "text": "C4 D4 E4 F4 G4 A4 B4 C5 B4 A4 G4 F4 E4 D4 C4",
                "created_at": created,
                "updated_at": created,
            },
            {
                "id": f"{new_song_id()}-chords",
                "title": "Chord Test",
                "artist": "Studio",
                "tags": "warmup,chords",
                "notes": "Simple held chords for testing sustain and release.",
                "text": "[C4 E4 G4]:2 R:0.5 [D4 F4 A4]:2 R:0.5 [E4 G4 B4]:2",
                "created_at": created,
                "updated_at": created,
            },
        ]

    def _load_song_library(self) -> None:
        if not LIBRARY_PATH.exists():
            self.library_entries = self._default_library_entries()
            self._save_song_library()
            return
        try:
            data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.library_entries = [entry for entry in data if isinstance(entry, dict)]
            else:
                self.library_entries = []
            if not self.library_entries:
                self.library_entries = self._default_library_entries()
        except Exception:
            self.library_entries = self._default_library_entries()

    def _save_song_library(self) -> None:
        LIBRARY_PATH.write_text(json.dumps(self.library_entries, indent=2), encoding="utf-8")

    def _library_display_name(self, entry: dict[str, object]) -> str:
        title = str(entry.get("title") or "Untitled Song")
        artist = str(entry.get("artist") or "").strip()
        tags = str(entry.get("tags") or "").strip()
        is_fav = bool(entry.get("favorite", False))
        plays = int(entry.get("play_count", 0))
        prefix = "\u2605 " if is_fav else "  "
        label = f"{prefix}{title}"
        if artist:
            label += f" - {artist}"
        if tags:
            label += f"  [{tags}]"
        if plays > 0:
            label += f"  ({plays}x)"
        return label

    def _filtered_library_entries(self) -> list[dict[str, object]]:
        query = getattr(self, "library_search_var", tk.StringVar(value="")).get().strip().lower()
        fav_only = getattr(self, "library_show_favorites_var", tk.BooleanVar(value=False)).get()
        filtered = self.library_entries
        if fav_only:
            filtered = [e for e in filtered if bool(e.get("favorite", False))]
        if query:
            filtered = [
                e for e in filtered
                if query in str(e.get("title", "")).lower()
                or query in str(e.get("artist", "")).lower()
                or query in str(e.get("tags", "")).lower()
                or query in str(e.get("notes", "")).lower()
            ]
        return filtered

    def _sorted_library_entries(self) -> list[dict[str, object]]:
        entries = self._filtered_library_entries()
        sort_by = getattr(self, "library_sort_var", tk.StringVar(value="Date")).get()
        if sort_by == "Title A-Z":
            return sorted(entries, key=lambda e: str(e.get("title", "")).lower())
        elif sort_by == "Artist A-Z":
            return sorted(
                entries,
                key=lambda e: (
                    str(e.get("artist", "")).lower(),
                    str(e.get("title", "")).lower(),
                ),
            )
        elif sort_by == "Most Played":
            return sorted(
                entries,
                key=lambda e: (-int(e.get("play_count", 0)), str(e.get("updated_at", "")),),
            )
        return sorted(entries, key=lambda e: str(e.get("updated_at", "")), reverse=True)

    def refresh_library_list(self) -> None:
        if not hasattr(self, "library_listbox"):
            return
        self.library_listbox.delete(0, tk.END)
        visible = 0
        for entry in self._sorted_library_entries():
            self.library_listbox.insert(tk.END, self._library_display_name(entry))
            visible += 1
        total = len(self.library_entries)
        query = getattr(self, "library_search_var", tk.StringVar(value="")).get().strip()
        if query or (getattr(self, "library_show_favorites_var", tk.BooleanVar(value=False)).get()):
            self.library_hint.set(f"{visible} of {total} song{'s' if total != 1 else ''} shown.")
        else:
            self.library_hint.set(f"{total} saved song{'s' if total != 1 else ''}.")
        self._update_library_stats()

    def _update_library_stats(self) -> None:
        if not hasattr(self, "library_stats"):
            return
        total = len(self.library_entries)
        if total == 0:
            self.library_stats.set("")
            return
        favorites = sum(1 for e in self.library_entries if e.get("favorite"))
        total_plays = sum(int(e.get("play_count", 0)) for e in self.library_entries)
        parts = [f"{total} songs"]
        if favorites:
            parts.append(f"{favorites} starred")
        if total_plays:
            parts.append(f"{total_plays} plays")
        self.library_stats.set("  ".join(parts))

    def _selected_library_entry(self) -> dict[str, object] | None:
        if not hasattr(self, "library_listbox"):
            return None
        selection = self.library_listbox.curselection()
        if not selection:
            return None
        sorted_entries = self._sorted_library_entries()
        index = selection[0]
        if index >= len(sorted_entries):
            return None
        return sorted_entries[index]

    def toggle_favorite_song(self) -> None:
        entry = self._selected_library_entry()
        if entry is None:
            self.status.set("Select a song first.")
            return
        current = bool(entry.get("favorite", False))
        entry["favorite"] = not current
        entry["updated_at"] = now_stamp()
        self._save_song_library()
        self.refresh_library_list()
        title = entry.get("title", "Untitled Song")
        self.status.set(f"{'Starred' if not current else 'Unstarred'}: {title}")

    def queue_song(self) -> None:
        entry = self._selected_library_entry()
        if entry is None:
            self.status.set("Select a song first.")
            return
        sid = str(entry.get("id", ""))
        if sid in self.playback_queue:
            self.status.set(f"Already queued: {entry.get('title', '?')}")
            return
        self.playback_queue.append(sid)
        n = len(self.playback_queue)
        self.status.set(f"Queued ({n}): {entry.get('title', '?')}")
        self.queue_status.set(f"Queue: {n}" if n else "")

    def clear_queue(self) -> None:
        count = len(self.playback_queue)
        self.playback_queue.clear()
        self.status.set(f"Queue cleared ({count} songs removed).")
        self.queue_status.set("")

    def play_queue(self) -> None:
        if not self.playback_queue:
            self.status.set("Queue is empty. Right-click a song and select Queue.")
            return
        if self.play_thread and self.play_thread.is_alive():
            self.status.set("Already playing. Stop first.")
            return
        sid = self.playback_queue.pop(0)
        entry = self._find_song_by_id(sid)
        if entry is None:
            self.status.set("Queued song not found in library.")
            self.play_queue()
            return
        self._load_song_entry_into_editor(entry)
        remaining = len(self.playback_queue)
        self.queue_status.set(f"Queue: {remaining}" if remaining else "")
        self.status.set(f"Playing queue ({remaining} remaining): {entry.get('title', '?')}")
        self.start_playback()
        if self.play_thread:
            self.after(500, self._monitor_queue_playback)

    def _monitor_queue_playback(self) -> None:
        if self.play_thread and self.play_thread.is_alive():
            self.after(500, self._monitor_queue_playback)
            return
        if self.playback_queue:
            self.play_queue()

    def _show_library_context_menu(self, event: tk.Event) -> None:
        self.library_listbox.selection_clear(0, tk.END)
        index = self.library_listbox.nearest(event.y)
        if index >= 0:
            self.library_listbox.selection_set(index)
        entry = self._selected_library_entry()
        menu = tk.Menu(self, tearoff=0, bg=UI_SURFACE, fg=UI_TEXT,
                        activebackground=UI_SURFACE_HOVER, activeforeground=UI_TEXT)
        menu.add_command(label="Load", command=self.load_selected_song)
        menu.add_command(label="Duplicate", command=self.duplicate_selected_song)
        menu.add_command(label="Queue", command=self.queue_song)
        fav_label = "Unstar" if (entry and entry.get("favorite")) else "Star"
        menu.add_command(label=fav_label, command=self.toggle_favorite_song)
        menu.add_separator()
        menu.add_command(label="Export...", command=self.export_current_song)
        menu.add_command(label="Delete", command=self.delete_selected_song)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _update_recent_label(self) -> None:
        if not hasattr(self, "recent_label"):
            return
        recent = sorted(
            [e for e in self.library_entries if e.get("last_played")],
            key=lambda e: str(e.get("last_played", "")),
            reverse=True,
        )[:3]
        if not recent:
            self.recent_label.set("")
            return
        names = [str(e.get("title", "?"))[:28] for e in recent]
        self.recent_label.set("Recently played:\n" + "\n".join(f"  {n}" for n in names))

    def _current_song_text(self) -> str:
        return self.score_text.get("1.0", tk.END).strip()

    def _song_payload_from_editor(self) -> dict[str, object]:
        created_at = now_stamp()
        existing = self._find_song_by_id(self.current_song_id) if self.current_song_id else None
        return {
            "id": self.current_song_id or new_song_id(),
            "title": self.song_title.get().strip() or "Untitled Song",
            "artist": self.song_artist.get().strip(),
            "tags": self.song_tags.get().strip(),
            "notes": self.song_notes.get().strip(),
            "text": self._current_song_text(),
            "created_at": str(existing.get("created_at")) if existing else created_at,
            "updated_at": now_stamp(),
        }

    def _find_song_by_id(self, song_id: str | None) -> dict[str, object] | None:
        if not song_id:
            return None
        for entry in self.library_entries:
            if entry.get("id") == song_id:
                return entry
        return None

    def _load_song_entry_into_editor(self, entry: dict[str, object]) -> None:
        self.current_song_id = str(entry.get("id") or new_song_id())
        self.song_title.set(str(entry.get("title") or "Untitled Song"))
        self.song_artist.set(str(entry.get("artist") or ""))
        self.song_tags.set(str(entry.get("tags") or ""))
        self.song_notes.set(str(entry.get("notes") or ""))
        self.loaded_midi_actions = None
        self.loaded_midi_name.set("No MIDI loaded")
        self.source_mode.set("text")
        self.score_text.delete("1.0", tk.END)
        self.score_text.insert("1.0", str(entry.get("text") or ""))
        self.score_text.edit_modified(False)
        self.status.set(f"Loaded: {self.song_title.get()}")
        self._mark_clean()
        self._update_title()
        self.analyze_current_source(show_popup=False)

    def new_song(self) -> None:
        if self._is_dirty and not messagebox.askyesno("Unsaved changes", "You have unsaved changes. Create a new song anyway?"):
            return
        self.current_song_id = None
        self.song_title.set("Untitled Song")
        self.song_artist.set("")
        self.song_tags.set("")
        self.song_notes.set("")
        self.loaded_midi_actions = None
        self.loaded_midi_name.set("No MIDI loaded")
        self.source_mode.set("text")
        self.score_text.delete("1.0", tk.END)
        self.status.set("New song ready.")
        self.clear_keyboard_highlights()
        self._mark_clean()
        self._update_title()

    def save_current_song(self) -> None:
        try:
            payload = self._song_payload_from_editor()
            existing = self._find_song_by_id(str(payload["id"]))
            if existing:
                existing.clear()
                existing.update(payload)
            else:
                self.library_entries.append(payload)
            self.current_song_id = str(payload["id"])
            self._save_song_library()
            self.refresh_library_list()
            self.status.set(f"Saved: {payload['title']}")
            self._mark_clean()
            self._update_title()
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def load_selected_song(self) -> None:
        entry = self._selected_library_entry()
        if entry is None:
            self.status.set("Select a song first.")
            return
        if self._is_dirty and not messagebox.askyesno("Unsaved changes", "You have unsaved changes. Load this song anyway?"):
            return
        self._load_song_entry_into_editor(entry)

    def load_latest_song_on_startup(self) -> None:
        if not self.library_entries or self.current_song_id is not None:
            return
        sorted_entries = self._sorted_library_entries()
        if not sorted_entries:
            return
        if hasattr(self, "library_listbox"):
            self.library_listbox.selection_clear(0, tk.END)
            self.library_listbox.selection_set(0)
        self._load_song_entry_into_editor(sorted_entries[0])

    def delete_selected_song(self) -> None:
        entry = self._selected_library_entry()
        if entry is None:
            self.status.set("Select a song first.")
            return
        title = str(entry.get("title") or "Untitled Song")
        if not messagebox.askyesno("Delete song", f"Delete '{title}' from the studio library?"):
            return
        self.library_entries = [item for item in self.library_entries if item.get("id") != entry.get("id")]
        if self.current_song_id == entry.get("id"):
            self.current_song_id = None
        self._save_song_library()
        self.refresh_library_list()
        self.status.set(f"Deleted: {title}")

    def duplicate_selected_song(self) -> None:
        entry = self._selected_library_entry()
        if entry is None:
            self.status.set("Select a song first.")
            return
        duplicate = dict(entry)
        duplicate["id"] = new_song_id()
        duplicate["title"] = f"{entry.get('title') or 'Untitled Song'} Copy"
        duplicate["created_at"] = now_stamp()
        duplicate["updated_at"] = now_stamp()
        self.library_entries.append(duplicate)
        self._save_song_library()
        self.refresh_library_list()
        self.status.set(f"Duplicated: {duplicate['title']}")

    def export_current_song(self) -> None:
        payload = self._song_payload_from_editor()
        suggested = re.sub(r"[^A-Za-z0-9_. -]+", "", str(payload["title"])).strip() or "song"
        path = filedialog.asksaveasfilename(
            title="Export JJS Piano Studio song",
            defaultextension=SONG_EXPORT_SUFFIX,
            initialfile=f"{suggested}{SONG_EXPORT_SUFFIX}",
            filetypes=[("JJS Piano Studio song", f"*{SONG_EXPORT_SUFFIX}"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.status.set(f"Exported: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def export_all_songs(self) -> None:
        if not self.library_entries:
            self.status.set("No songs in library to export.")
            return
        directory = filedialog.askdirectory(title="Export all songs to folder")
        if not directory:
            return
        exported = 0
        errors: list[str] = []
        for entry in self.library_entries:
            try:
                title = str(entry.get("title", "untitled"))
                safe = re.sub(r"[^A-Za-z0-9_. -]+", "", title).strip() or "song"
                payload = {
                    "id": str(entry.get("id", "")),
                    "title": str(entry.get("title", "Untitled Song")),
                    "artist": str(entry.get("artist", "")),
                    "tags": str(entry.get("tags", "")),
                    "notes": str(entry.get("notes", "")),
                    "text": str(entry.get("text", "")),
                    "created_at": str(entry.get("created_at", "")),
                    "updated_at": str(entry.get("updated_at", "")),
                }
                dest = Path(directory) / f"{safe}{SONG_EXPORT_SUFFIX}"
                counter = 1
                while dest.exists():
                    dest = Path(directory) / f"{safe}_{counter}{SONG_EXPORT_SUFFIX}"
                    counter += 1
                dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                exported += 1
            except Exception as exc:
                errors.append(f"{entry.get('title', '?')}: {exc}")
        self.status.set(f"Exported {exported} of {len(self.library_entries)} songs.")
        if errors:
            messagebox.showwarning("Export warnings", "\n".join(errors[:8]))

    def import_song(self) -> None:
        path = filedialog.askopenfilename(
            title="Import JJS Piano Studio song",
            filetypes=[("JJS Piano Studio song", f"*{SONG_EXPORT_SUFFIX}"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "text" not in data:
                raise ValueError("That file does not look like a JJS Piano Studio song.")
            data = dict(data)
            data["id"] = new_song_id()
            data["created_at"] = now_stamp()
            data["updated_at"] = now_stamp()
            self.library_entries.append(data)
            self._save_song_library()
            self.refresh_library_list()
            self._load_song_entry_into_editor(data)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))

    def import_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception:
            self.status.set("Clipboard is empty or inaccessible.")
            return
        if not text.strip():
            self.status.set("Clipboard is empty.")
            return
        self.new_song()
        self.score_text.delete("1.0", tk.END)
        self.score_text.insert("1.0", text.strip())
        self.source_mode.set("text")
        self._mark_dirty()
        self._update_title()
        self.status.set("Pasted from clipboard. Edit and save.")

    def export_as_plain_text(self) -> None:
        text = self.score_text.get("1.0", tk.END).strip()
        if not text:
            self.status.set("No text to export.")
            return
        suggested = re.sub(r"[^A-Za-z0-9_. -]+", "", self.song_title.get()).strip() or "song"
        path = filedialog.asksaveasfilename(
            title="Export as plain text",
            defaultextension=".txt",
            initialfile=f"{suggested}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
            self.status.set(f"Exported text: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def import_multiple_songs(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Import multiple JJS Piano Studio songs",
            filetypes=[("JJS Piano Studio song", f"*{SONG_EXPORT_SUFFIX}"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not paths:
            return
        imported = 0
        errors: list[str] = []
        for path in paths:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if not isinstance(data, dict) or "text" not in data:
                    errors.append(f"{Path(path).name}: not a valid song file")
                    continue
                data = dict(data)
                data["id"] = new_song_id()
                data["created_at"] = now_stamp()
                data["updated_at"] = now_stamp()
                self.library_entries.append(data)
                imported += 1
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        if imported:
            self._save_song_library()
            self.refresh_library_list()
            self.status.set(f"Imported {imported} song{'s' if imported != 1 else ''}.")
        if errors:
            messagebox.showwarning("Import warnings", f"Imported {imported} successfully.\n\nIssues:\n" + "\n".join(errors[:8]))

    def _load_settings(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            numeric_settings: list[tuple[str, tk.Variable]] = [
                ("bpm", self.bpm),
                ("speed", self.speed),
                ("default_beats", self.default_beats),
                ("hold_percent", self.hold_percent),
                ("gap_ms", self.gap_ms),
                ("start_delay", self.start_delay),
                ("playback_start_offset", self.playback_start_offset),
                ("playback_end_at", self.playback_end_at),
                ("transpose", self.transpose),
                ("calibration_hold_seconds", self.calibration_hold_seconds),
                ("timing_quantize_beats", self.timing_quantize_beats),
                ("timing_quantize_strength", self.timing_quantize_strength),
                ("timing_offset_ms", self.timing_offset_ms),
                ("timing_min_note_beats", self.timing_min_note_beats),
                ("timing_max_note_beats", self.timing_max_note_beats),
                ("timing_gap_ms", self.timing_gap_ms),
                ("loop_start_sec", self.loop_start_sec),
                ("loop_end_sec", self.loop_end_sec),
                ("synth_volume", self.synth_volume),
                ("practice_start_speed", self.practice_start_speed),
                ("practice_target_speed", self.practice_target_speed),
                ("practice_increment", self.practice_increment),
                ("practice_loops_per_step", self.practice_loops_per_step),
            ]
            for key, variable in numeric_settings:
                if key in data:
                    variable.set(data[key])
            if isinstance(data.get("preview_only"), bool):
                self.preview_only.set(bool(data["preview_only"]))
            if isinstance(data.get("timing_auto_offset"), bool):
                self.timing_auto_offset.set(bool(data["timing_auto_offset"]))
            if isinstance(data.get("loop_enabled"), bool):
                self.loop_enabled.set(bool(data["loop_enabled"]))
            if isinstance(data.get("synth_enabled"), bool):
                self.synth_enabled.set(bool(data["synth_enabled"]))
            if isinstance(data.get("practice_mode"), bool):
                self.practice_mode.set(bool(data["practice_mode"]))
            if isinstance(data.get("is_dark_theme"), bool):
                stored_theme = bool(data["is_dark_theme"])
                if stored_theme != getattr(self, "_is_dark_theme", True):
                    self._is_dark_theme = stored_theme
                    self._configure_theme()
            string_val = data.get("synth_instrument")
            if isinstance(string_val, str) and string_val in MIDI_INSTRUMENTS:
                self.synth_instrument.set(string_val)

            string_settings: list[tuple[str, tk.StringVar, list[str] | None]] = [
                ("high_note", self.high_note, ["C6", "C7"]),
                ("range_mode", self.range_mode, ["Auto-fit octaves", "Skip out-of-range", "Stop with error"]),
                (
                    "input_method",
                    self.input_method,
                    ["Windows SendInput scan", "Windows SendInput vk", "Windows keybd_event", "PyAutoGUI"],
                ),
                ("calibration_note", self.calibration_note, NOTE_OPTIONS),
                ("play_hotkey", self.play_hotkey, None),
                ("pause_hotkey", self.pause_hotkey, None),
                ("stop_hotkey", self.stop_hotkey, None),
            ]
            for key, variable, allowed in string_settings:
                value = data.get(key)
                if isinstance(value, str) and value.strip() and (allowed is None or value in allowed):
                    variable.set(value)
            self._refresh_hotkey_state()
            self.settings_loaded = True
        except Exception as exc:
            self.status.set(f"Could not load saved settings: {exc}")

    def _save_settings(self) -> None:
        try:
            data = {
                "bpm": float(self.bpm.get()),
                "speed": float(self.speed.get()),
                "default_beats": float(self.default_beats.get()),
                "hold_percent": float(self.hold_percent.get()),
                "gap_ms": float(self.gap_ms.get()),
                "start_delay": float(self.start_delay.get()),
                "playback_start_offset": float(self.playback_start_offset.get()),
                "playback_end_at": float(self.playback_end_at.get()),
                "transpose": int(self.transpose.get()),
                "high_note": self.high_note.get(),
                "range_mode": self.range_mode.get(),
                "input_method": self.input_method.get(),
                "preview_only": bool(self.preview_only.get()),
                "calibration_note": self.calibration_note.get(),
                "calibration_hold_seconds": float(self.calibration_hold_seconds.get()),
                "timing_quantize_beats": float(self.timing_quantize_beats.get()),
                "timing_quantize_strength": float(self.timing_quantize_strength.get()),
                "timing_offset_ms": float(self.timing_offset_ms.get()),
                "timing_auto_offset": bool(self.timing_auto_offset.get()),
                "timing_min_note_beats": float(self.timing_min_note_beats.get()),
                "timing_max_note_beats": float(self.timing_max_note_beats.get()),
                "timing_gap_ms": float(self.timing_gap_ms.get()),
                "loop_enabled": bool(self.loop_enabled.get()),
                "loop_start_sec": float(self.loop_start_sec.get()),
                "loop_end_sec": float(self.loop_end_sec.get()),
                "synth_enabled": bool(self.synth_enabled.get()),
                "synth_instrument": self.synth_instrument.get(),
                "synth_volume": int(self.synth_volume.get()),
                "practice_mode": bool(self.practice_mode.get()),
                "practice_start_speed": float(self.practice_start_speed.get()),
                "practice_target_speed": float(self.practice_target_speed.get()),
                "practice_increment": float(self.practice_increment.get()),
                "practice_loops_per_step": int(self.practice_loops_per_step.get()),
                "is_dark_theme": bool(getattr(self, "_is_dark_theme", True)),
                "play_hotkey": self.play_hotkey.get(),
                "pause_hotkey": self.pause_hotkey.get(),
                "stop_hotkey": self.stop_hotkey.get(),
            }
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            self.status.set(f"Could not save settings: {exc}")

    def begin_hotkey_capture(self, action_name: str, variable: tk.StringVar) -> None:
        self.hotkey_capture_target = (action_name, variable)
        self.status.set(f"Press the key to use for {action_name}.")

    def _apply_captured_hotkey(self, hotkey_name: str) -> None:
        target = self.hotkey_capture_target
        if target is None:
            return
        action_name, variable = target
        self.hotkey_capture_target = None
        normalized_name = self._normalize_hotkey(hotkey_name)
        self.hotkey_capture_block_name = normalized_name
        self.hotkey_capture_block_until = time.perf_counter() + 0.75
        display_name = self._format_hotkey_name(hotkey_name)
        variable.set(display_name)
        if not self._refresh_hotkey_state():
            self.status.set("Hotkey conflict: choose different keys for Play, Pause, and Stop.")
            return
        self.status.set(f"{action_name} hotkey set to {display_name}.")

    def _on_tk_hotkey_capture(self, event: tk.Event) -> str | None:
        if self.hotkey_capture_target is None:
            return None
        hotkey_name = self._tk_hotkey_name(event)
        if not hotkey_name:
            self.status.set("That key cannot be used as a hotkey. Press another key.")
            return "break"
        self._apply_captured_hotkey(hotkey_name)
        return "break"

    def _start_hotkeys(self) -> None:
        if pynput_keyboard is None:
            self.status.set("Ready. Install pynput for global hotkeys: python -m pip install pynput")
            return

        def on_press(key: object) -> None:
            name = self._hotkey_name(key)
            if not name:
                return
            if self.recording.get() and self.record_start_time > 0:
                elapsed = time.perf_counter() - self.record_start_time
                self.recorded_events.append((elapsed, "down", name))
            if self.hotkey_capture_target is not None:
                self.after(0, lambda captured=name: self._apply_captured_hotkey(captured))
                return
            if name == self.hotkey_capture_block_name and time.perf_counter() < self.hotkey_capture_block_until:
                return
            if name in self.pressed_hotkey_parts:
                return
            self.pressed_hotkey_parts.add(name)
            if not self.hotkeys_unique:
                self.after(0, lambda: self.status.set("Hotkey conflict: choose different keys for Play, Pause, and Stop."))
                return
            if name == self.current_stop_hotkey:
                self.after(0, self.stop_playback)
            elif name == self.current_pause_hotkey:
                self.after(0, self.toggle_pause)
            elif name == self.current_play_hotkey:
                self.after(0, self.start_playback)

        def on_release(key: object) -> None:
            name = self._hotkey_name(key)
            if name:
                self.pressed_hotkey_parts.discard(name)
                if self.recording.get() and self.record_start_time > 0:
                    elapsed = time.perf_counter() - self.record_start_time
                    self.recorded_events.append((elapsed, "up", name))

        self.listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
        self.listener.daemon = True
        self.listener.start()

    @staticmethod
    def _normalize_hotkey(value: str) -> str:
        return value.strip().lower().replace(" ", "_").replace("-", "_")

    @staticmethod
    def _format_hotkey_name(value: str) -> str:
        normalized = PianoMacroApp._normalize_hotkey(value)
        labels = {
            "ctrl": "Ctrl",
            "alt": "Alt",
            "space": "Space",
            "enter": "Enter",
            "esc": "Esc",
            "tab": "Tab",
            "backspace": "Backspace",
            "delete": "Delete",
            "insert": "Insert",
            "home": "Home",
            "end": "End",
            "page_up": "Page Up",
            "page_down": "Page Down",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "caps_lock": "Caps Lock",
        }
        if re.fullmatch(r"f\d{1,2}", normalized):
            return normalized.upper()
        if len(normalized) == 1:
            return normalized.upper()
        return labels.get(normalized, normalized.replace("_", " ").title())

    def _refresh_hotkey_state(self) -> bool:
        self.current_play_hotkey = self._normalize_hotkey(self.play_hotkey.get())
        self.current_pause_hotkey = self._normalize_hotkey(self.pause_hotkey.get())
        self.current_stop_hotkey = self._normalize_hotkey(self.stop_hotkey.get())
        hotkeys = [self.current_play_hotkey, self.current_pause_hotkey, self.current_stop_hotkey]
        self.hotkeys_unique = all(hotkeys) and len(set(hotkeys)) == len(hotkeys)
        return self.hotkeys_unique

    def _on_hotkey_changed(self, *_: object) -> None:
        if not self._refresh_hotkey_state():
            self.status.set("Hotkey conflict: choose different keys for Play, Pause, and Stop.")
            return
        self.status.set(
            f"Hotkeys set: Play {self.play_hotkey.get()}, Pause {self.pause_hotkey.get()}, "
            f"Stop {self.stop_hotkey.get()}."
        )

    @staticmethod
    def _tk_hotkey_name(event: tk.Event) -> str | None:
        keysym = str(getattr(event, "keysym", "") or "")
        char = str(getattr(event, "char", "") or "")
        if char and len(char) == 1 and char.isprintable() and not char.isspace():
            return char.lower()
        if re.fullmatch(r"F\d{1,2}", keysym):
            return keysym.lower()
        mapping = {
            "space": "space",
            "Return": "enter",
            "KP_Enter": "enter",
            "Escape": "esc",
            "Tab": "tab",
            "BackSpace": "backspace",
            "Delete": "delete",
            "Insert": "insert",
            "Home": "home",
            "End": "end",
            "Prior": "page_up",
            "Next": "page_down",
            "Up": "up",
            "Down": "down",
            "Left": "left",
            "Right": "right",
            "Caps_Lock": "caps_lock",
            "Control_L": "ctrl",
            "Control_R": "ctrl",
            "Alt_L": "alt",
            "Alt_R": "alt",
        }
        return mapping.get(keysym)

    @staticmethod
    def _hotkey_name(key: object) -> str | None:
        if pynput_keyboard is None:
            return None
        if key in {pynput_keyboard.Key.ctrl, pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r}:
            return "ctrl"
        if key in {pynput_keyboard.Key.alt, pynput_keyboard.Key.alt_l, pynput_keyboard.Key.alt_r}:
            return "alt"
        if key == pynput_keyboard.Key.space:
            return "space"
        key_names = {
            "f1": "f1",
            "f2": "f2",
            "f3": "f3",
            "f4": "f4",
            "f5": "f5",
            "f6": "f6",
            "f7": "f7",
            "f8": "f8",
            "f9": "f9",
            "f10": "f10",
            "f11": "f11",
            "f12": "f12",
            "f13": "f13",
            "f14": "f14",
            "f15": "f15",
            "f16": "f16",
            "f17": "f17",
            "f18": "f18",
            "f19": "f19",
            "f20": "f20",
            "enter": "enter",
            "esc": "esc",
            "tab": "tab",
            "backspace": "backspace",
            "delete": "delete",
            "insert": "insert",
            "home": "home",
            "end": "end",
            "page_up": "page_up",
            "page_down": "page_down",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
            "caps_lock": "caps_lock",
        }
        for attribute, name in key_names.items():
            if hasattr(pynput_keyboard.Key, attribute) and key == getattr(pynput_keyboard.Key, attribute):
                return name
        char = getattr(key, "char", None)
        if char:
            return char.lower()
        return None

    def toggle_recording(self) -> None:
        if self.recording.get():
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        self.recorded_events.clear()
        self.record_start_time = time.perf_counter()
        self.recording.set(True)
        self.record_btn.configure(text="Stop Rec", style="Danger.TButton")
        self.status.set("Recording... Press Record again to stop.")

    def _stop_recording(self) -> None:
        self.recording.set(False)
        self.record_btn.configure(text="Record", style="TButton")
        if not self.recorded_events:
            self.status.set("Nothing recorded.")
            return
        self._build_score_from_recording()

    def _build_score_from_recording(self) -> None:
        label_to_midi: dict[str, int] = {}
        for midi_note, binding in KEY_MAP.items():
            label_to_midi[binding.label.lower()] = midi_note

        notes: list[tuple[float, float, int]] = []
        active: dict[str, float] = {}
        for timestamp, action, label in self.recorded_events:
            clean = label.lower()
            if action == "down" and clean not in active:
                active[clean] = timestamp
            elif action == "up" and clean in active:
                start = active.pop(clean)
                midi_note = label_to_midi.get(clean)
                if midi_note and timestamp > start:
                    notes.append((start, timestamp, midi_note))

        if not notes:
            self.status.set("No playable notes recorded.")
            return

        bpm = max(20.0, float(self.bpm.get()))
        beat_seconds = 60.0 / bpm
        tokens: list[str] = []
        notes.sort(key=lambda n: n[0])
        cursor = 0.0

        i = 0
        while i < len(notes):
            start, end, midi = notes[i]
            duration = max(0.01, end - start)
            beats = max(0.25, round(duration / beat_seconds * 4) / 4)
            chord_notes = [midi]
            j = i + 1
            while j < len(notes) and abs(notes[j][0] - start) < 0.05:
                chord_notes.append(notes[j][2])
                j += 1

            if start > cursor + 0.01:
                rest_beats = max(0.25, round((start - cursor) / beat_seconds * 4) / 4)
                tokens.append(f"R:{PianoMacroApp._format_beats(rest_beats)}")

            names = [midi_to_note_name(n) for n in sorted(set(chord_notes))]
            if len(names) == 1:
                token = names[0]
            else:
                token = "[" + " ".join(names) + "]"
            token += f":{PianoMacroApp._format_beats(beats)}"
            tokens.append(token)
            cursor = max(cursor, start + beats * beat_seconds)
            i = j

        text = "\n".join(" ".join(tokens[k:k + 8]) for k in range(0, len(tokens), 8))
        self.new_song()
        self.score_text.delete("1.0", tk.END)
        self.score_text.insert("1.0", text)
        self.song_title.set("Recording")
        self.song_notes.set(f"Recorded {len(notes)} notes at {bpm:.0f} BPM.")
        self.source_mode.set("text")
        self._mark_dirty()
        self._update_title()
        self.status.set(f"Recording loaded: {len(notes)} notes, {len(tokens)} events.")
        self.analyze_current_source(show_popup=False)
        self.recorded_events.clear()
        self.loaded_midi_actions = None
        self.loaded_midi_name.set("No MIDI loaded")
        self.source_mode.set("text")
        self.score_text.delete("1.0", tk.END)
        self.score_text.insert("1.0", "C4 D4 E4 F4 G4 A4 B4 C5 B4 A4 G4 F4 E4 D4 C4")
        self.status.set("Inserted a simple C scale.")

    def test_selected_note(self) -> None:
        self.test_note(self.calibration_note.get())

    def test_note(self, note_name: str) -> None:
        if self.play_thread and self.play_thread.is_alive():
            self.status.set("Already playing. Stop first, then test a note.")
            return
        try:
            midi_note = note_name_to_midi(note_name)
            if midi_note not in KEY_MAP:
                raise ValueError(f"{note_name} is not in the screenshot keyboard map.")
            hold_seconds = max(0.2, float(self.calibration_hold_seconds.get()))
            start_delay = max(0.0, float(self.start_delay.get()))
            self.sender.set_method(self.input_method.get())
        except Exception as exc:
            messagebox.showerror("Could not test note", str(exc))
            return

        self.stop_event.clear()
        self.pause_event.clear()
        self.progress.set(0.0)
        self.play_thread = threading.Thread(
            target=self._test_note_worker,
            args=(midi_note, note_name, hold_seconds, start_delay),
            daemon=True,
        )
        self.play_thread.start()

    def use_text_source(self) -> None:
        self.source_mode.set("text")
        self.status.set("Text source selected.")

    def load_midi(self) -> None:
        path = filedialog.askopenfilename(
            title="Load a MIDI file",
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            tracks = get_midi_track_info(path)
            track_indices: set[int] | None = None
            if len(tracks) > 1:
                selected = self._select_midi_tracks(tracks)
                if selected is None:
                    return
                track_indices = selected
            self.loaded_midi_actions = load_midi_actions(path, track_indices=track_indices)
        except Exception as exc:
            messagebox.showerror("MIDI load failed", str(exc))
            return
        self.source_mode.set("midi")
        self.loaded_midi_name.set(Path(path).name)
        self._add_recent_midi(path)
        self.status.set(f"Loaded MIDI: {Path(path).name}")
        self.analyze_current_source(show_popup=False)

    def _add_recent_midi(self, path: str) -> None:
        if path in self.recent_midi_files:
            self.recent_midi_files.remove(path)
        self.recent_midi_files.insert(0, path)
        self.recent_midi_files = self.recent_midi_files[:10]

    def _show_recent_midi_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=0, bg=UI_SURFACE, fg=UI_TEXT,
                        activebackground=UI_SURFACE_HOVER, activeforeground=UI_TEXT)
        if not self.recent_midi_files:
            menu.add_command(label="(no recent files)", state=tk.DISABLED)
        else:
            for path in self.recent_midi_files[:8]:
                name = Path(path).name
                menu.add_command(
                    label=f"{name[:50]}{'...' if len(name)>50 else ''}",
                    command=lambda p=path: self._load_recent_midi(p),
                )
            menu.add_separator()
            menu.add_command(label="Clear recent", command=lambda: self.recent_midi_files.clear())
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _load_recent_midi(self, path: str) -> None:
        if not Path(path).exists():
            self.recent_midi_files = [p for p in self.recent_midi_files if p != path]
            self.status.set("Recent MIDI file no longer exists.")
            return
        try:
            tracks = get_midi_track_info(path)
            track_indices: set[int] | None = None
            if len(tracks) > 1:
                selected = self._select_midi_tracks(tracks)
                if selected is None:
                    return
                track_indices = selected
            self.loaded_midi_actions = load_midi_actions(path, track_indices=track_indices)
        except Exception as exc:
            messagebox.showerror("MIDI load failed", str(exc))
            return
        self.source_mode.set("midi")
        self.loaded_midi_name.set(Path(path).name)
        self._add_recent_midi(path)
        self.status.set(f"Loaded MIDI: {Path(path).name}")
        self.analyze_current_source(show_popup=False)

    def _select_midi_tracks(self, tracks: list[dict[str, object]]) -> set[int] | None:
        dialog = tk.Toplevel(self)
        dialog.title("Select MIDI Tracks")
        dialog.geometry("450x350")
        dialog.configure(bg=getattr(self, "_active_bg", UI_BG))
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"Select tracks to load ({len(tracks)} tracks found):", style="Section.TLabel").pack(anchor="w")

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 12))
        listbox = tk.Listbox(list_frame, selectmode="extended", height=10, activestyle="dotbox")
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._style_listbox(listbox)

        for t in tracks:
            name = str(t.get("name", "?"))
            notes = int(t.get("notes", 0))
            chs = t.get("channels", [])
            ch_str = f"ch:{','.join(str(c) for c in chs)}" if chs else ""
            listbox.insert(tk.END, f"{name}  ({notes} notes)  {ch_str}")

        for i in range(len(tracks)):
            if int(tracks[i].get("notes", 0)) > 0:
                listbox.selection_set(i)

        result: set[int] | None = None

        def on_ok() -> None:
            nonlocal result
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("Track selection", "Select at least one track.", parent=dialog)
                return
            result = {int(tracks[i]["index"]) for i in sel}
            dialog.destroy()

        ttk.Button(frame, text="Select All", command=lambda: listbox.selection_set(0, tk.END)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(frame, text="Clear", command=lambda: listbox.selection_clear(0, tk.END)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(frame, text="Load Selected", command=on_ok, style="Accent.TButton").pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(frame, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

        dialog.wait_window()
        return result

    def _online_midi_repair_settings(self) -> dict[str, float | bool]:
        return {
            "bpm": max(1.0, float(self.bpm.get())),
            "quantize_beats": max(0.0, float(self.timing_quantize_beats.get())),
            "quantize_strength": max(0.0, min(1.0, float(self.timing_quantize_strength.get()))),
            "offset_seconds": float(self.timing_offset_ms.get()) / 1000.0,
            "min_note_beats": max(0.02, float(self.timing_min_note_beats.get())),
            "max_note_beats": max(0.1, float(self.timing_max_note_beats.get())),
            "gap_ms": max(0.0, float(self.timing_gap_ms.get())),
            "auto_offset": bool(self.timing_auto_offset.get()),
            "low_midi": note_name_to_midi("C2"),
            "high_midi": note_name_to_midi(self.high_note.get()),
        }

    def _apply_prepared_online_midi(self, prepared: PreparedOnlineMidi) -> None:
        self.loaded_midi_actions = list(prepared.actions)
        self.loaded_midi_name.set(prepared.path.name)
        self.source_mode.set("midi")
        if prepared.bpm is not None:
            self.bpm.set(round(prepared.bpm, 2))
        self.current_song_id = None
        self.song_title.set(prepared.result.title)
        self.song_artist.set(prepared.result.author)
        is_direct_midi = prepared.result.source == "Direct MIDI URL"
        self.song_tags.set("direct-midi" if is_direct_midi else "online-midi,onlinesequencer")
        if is_direct_midi:
            notes = [
                f"Downloaded direct MIDI URL: {prepared.result.url}",
                f"Saved MIDI: {prepared.path}",
            ]
        else:
            notes = [
                f"Downloaded from Online Sequencer #{prepared.result.sequence_id}: {prepared.result.url}",
                f"Generated MIDI: {prepared.path}",
            ]
        if prepared.trimmed_seconds > 0:
            notes.append(f"Skipped note-art intro: {prepared.trimmed_seconds:.1f}s, {prepared.trimmed_notes} notes removed.")
        if prepared.cleaned_before_notes and prepared.cleaned_after_notes:
            notes.append(
                f"Auto-cleaned for Roblox: {prepared.cleaned_before_notes} -> {prepared.cleaned_after_notes} notes."
            )
        if prepared.repaired:
            notes.append("Timing repair was applied during load.")
        self.song_notes.set("\n".join(notes))
        self.analysis_summary.set(prepared.summary)
        status_parts = []
        if prepared.trimmed_seconds > 0:
            status_parts.append("note-art trim")
        if prepared.repaired:
            status_parts.append("timing repair")
        if prepared.cleaned_before_notes and prepared.cleaned_after_notes:
            status_parts.append("Roblox cleanup")
        suffix = f" with {', '.join(status_parts)}" if status_parts else ""
        source_label = "direct MIDI" if is_direct_midi else "Online Sequencer MIDI"
        self.status.set(f"Loaded {source_label}{suffix}: {prepared.result.title}")

    def cleanup_loaded_midi_playability(self) -> None:
        if not self.loaded_midi_actions:
            self.status.set("Load or convert a MIDI first.")
            return
        try:
            settings = self.read_settings()
            cleaned_actions, before, after = cleanup_midi_actions_for_playability(
                self.loaded_midi_actions,
                bpm=settings.bpm,
                low_midi=settings.low_midi,
                high_midi=settings.high_midi,
                max_polyphony=4,
            )
            if not cleaned_actions:
                raise ValueError("Cleanup removed every note. Try a different MIDI.")
            self.loaded_midi_actions = cleaned_actions
            self.source_mode.set("midi")
            self.status.set(f"Auto-cleaned MIDI for playback: {before} -> {after} notes.")
            self.analyze_current_source(show_popup=False)
        except Exception as exc:
            messagebox.showerror("MIDI cleanup failed", str(exc))

    def open_timeline_editor(self) -> None:
        try:
            settings = self.read_settings()
            actions, source_name = self._base_actions_for_current_source(settings)
            notes = scheduled_actions_to_audio_notes(actions)
            if not actions:
                raise ValueError("Nothing to show on the timeline.")
        except Exception as exc:
            messagebox.showerror("Timeline", str(exc))
            return

        duration = max((action.seconds for action in actions), default=0.0)
        if notes:
            duration = max(duration, max(note.end for note in notes))
        duration = max(0.1, duration)

        window = tk.Toplevel(self)
        window.title("Timeline Editor")
        window.geometry("880x520")
        window.minsize(760, 440)
        window.configure(bg=UI_BG)
        window.transient(self)

        start_var = tk.DoubleVar(value=max(0.0, float(self.playback_start_offset.get())))
        end_var = tk.DoubleVar(value=max(0.0, float(self.playback_end_at.get())))
        status_var = tk.StringVar(value=f"{source_name} | {len(notes)} notes | {duration:.1f}s")

        root = ttk.Frame(window, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        ttk.Label(root, text="Timeline Editor", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        canvas = tk.Canvas(root, bg=UI_FIELD, highlightthickness=1, highlightbackground=UI_BORDER, height=250)
        canvas.grid(row=1, column=0, sticky="nsew", pady=(12, 10))

        controls = ttk.Frame(root)
        controls.grid(row=2, column=0, sticky="ew")
        for column in range(10):
            controls.columnconfigure(column, weight=1 if column in {8, 9} else 0)
        ttk.Label(controls, text="Start sec").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Spinbox(controls, textvariable=start_var, from_=0, to=max(3600.0, duration), increment=0.5, width=10).grid(
            row=0, column=1, padx=(0, 10)
        )
        ttk.Label(controls, text="End sec").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Spinbox(controls, textvariable=end_var, from_=0, to=max(3600.0, duration), increment=0.5, width=10).grid(
            row=0, column=3, padx=(0, 10)
        )
        ttk.Label(controls, text="0 = no end trim", style="Muted.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Label(root, textvariable=status_var, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))

        def clamp_window(update_vars: bool = False) -> tuple[float, float]:
            try:
                start_raw = float(start_var.get())
            except Exception:
                start_raw = 0.0
            try:
                end_raw = float(end_var.get())
            except Exception:
                end_raw = 0.0
            start = max(0.0, min(duration, start_raw))
            end = max(0.0, min(duration, end_raw))
            if end > 0 and end <= start:
                end = min(duration, start + 0.5)
            if update_vars:
                start_var.set(round(start, 3))
                end_var.set(round(end, 3) if end > 0 else 0.0)
            return start, end

        def draw_timeline() -> None:
            canvas.delete("all")
            width = max(1, canvas.winfo_width())
            height = max(1, canvas.winfo_height())
            margin = 18
            lane_top = 18
            lane_bottom = max(lane_top + 40, height - 66)
            left = margin
            right = max(left + 1, width - margin)
            usable = right - left
            canvas.create_line(left, lane_bottom, right, lane_bottom, fill=UI_BORDER)
            if notes:
                low = min(note.midi for note in notes)
                high = max(note.midi for note in notes)
                pitch_span = max(1, high - low)
                sample_step = max(1, len(notes) // 1800)
                for note in notes[::sample_step]:
                    x1 = left + usable * (note.start / duration)
                    x2 = left + usable * (note.end / duration)
                    y = lane_bottom - (lane_bottom - lane_top) * ((note.midi - low) / pitch_span)
                    canvas.create_rectangle(x1, y - 1, max(x1 + 1, x2), y + 1, fill=UI_ACCENT, outline="")
            bin_count = min(160, max(16, int(width / 5)))
            bins = [0] * bin_count
            for note in notes:
                index = min(bin_count - 1, max(0, int(note.start / duration * bin_count)))
                bins[index] += 1
            max_bin = max(bins) if bins else 1
            bar_top = height - 48
            bar_bottom = height - 20
            for index, count in enumerate(bins):
                x1 = left + usable * index / bin_count
                x2 = left + usable * (index + 1) / bin_count
                bar_h = (bar_bottom - bar_top) * (count / max_bin if max_bin else 0)
                canvas.create_rectangle(x1, bar_bottom - bar_h, x2, bar_bottom, fill=UI_SELECTION, outline="")
            start, end = clamp_window()
            start_x = left + usable * (start / duration)
            end_x = left + usable * (end / duration) if end > 0 else right
            canvas.create_rectangle(start_x, 0, end_x, height, outline=UI_ACCENT, width=2)
            canvas.create_line(start_x, 0, start_x, height, fill=UI_ACCENT, width=2)
            if end > 0:
                canvas.create_line(end_x, 0, end_x, height, fill=UI_DANGER, width=2)
            canvas.create_text(left, height - 6, text="0s", fill=UI_MUTED, anchor="sw")
            canvas.create_text(right, height - 6, text=f"{duration:.1f}s", fill=UI_MUTED, anchor="se")

        def set_from_click(event: tk.Event) -> None:
            width = max(1, canvas.winfo_width())
            margin = 18
            usable = max(1, width - margin * 2)
            seconds = max(0.0, min(duration, (float(event.x) - margin) / usable * duration))
            if getattr(event, "num", 1) == 3 or bool(getattr(event, "state", 0) & 0x0001):
                end_var.set(round(seconds, 2))
            else:
                start_var.set(round(seconds, 2))
            draw_timeline()

        def use_as_playback_window() -> None:
            start, end = clamp_window(update_vars=True)
            self.playback_start_offset.set(start)
            self.playback_end_at.set(end)
            self.status.set(f"Playback window set: start {start:.1f}s" + (f", end {end:.1f}s." if end > 0 else "."))

        def preview_window() -> None:
            use_as_playback_window()
            self.start_preview_playback()

        def apply_trim() -> None:
            if not self.loaded_midi_actions:
                messagebox.showerror("Timeline", "Load a MIDI before applying timeline edits.")
                return
            start, end = clamp_window(update_vars=True)
            trimmed = window_scheduled_actions(self.loaded_midi_actions, start, end)
            if not trimmed:
                messagebox.showerror("Timeline", "That trim would remove every note.")
                return
            self.loaded_midi_actions = trimmed
            self.playback_start_offset.set(0.0)
            self.playback_end_at.set(0.0)
            self.status.set(f"Applied timeline trim: start {start:.1f}s" + (f", end {end:.1f}s." if end > 0 else "."))
            self.analyze_current_source(show_popup=False)
            window.destroy()

        def delete_range() -> None:
            if not self.loaded_midi_actions:
                messagebox.showerror("Timeline", "Load a MIDI before deleting sections.")
                return
            start, end = clamp_window(update_vars=True)
            if end <= start:
                messagebox.showerror("Timeline", "Set an end time after the start time first.")
                return
            notes_before = scheduled_actions_to_audio_notes(self.loaded_midi_actions)
            kept: list[AudioMidiNote] = []
            for note in notes_before:
                if note.end <= start:
                    kept.append(note)
                elif note.start >= end:
                    kept.append(AudioMidiNote(note.start - (end - start), note.end - (end - start), note.midi, note.velocity))
            if not kept:
                messagebox.showerror("Timeline", "That delete would remove every note.")
                return
            self.loaded_midi_actions = audio_notes_to_actions(kept)
            self.playback_start_offset.set(0.0)
            self.playback_end_at.set(0.0)
            self.status.set(f"Deleted timeline range {start:.1f}s-{end:.1f}s.")
            self.analyze_current_source(show_popup=False)
            window.destroy()

        def cleanup_from_timeline() -> None:
            self.cleanup_loaded_midi_playability()
            window.destroy()

        button_row = ttk.Frame(root)
        button_row.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(button_row, text="Use Window", command=use_as_playback_window, style="Accent.TButton").pack(side=tk.LEFT)
        ttk.Button(button_row, text="Preview Window", command=preview_window).pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="Apply Trim to MIDI", command=apply_trim).pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="Delete Range", command=delete_range).pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="Auto Cleanup", command=cleanup_from_timeline).pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="Close", command=window.destroy).pack(side=tk.RIGHT)

        canvas.bind("<Configure>", lambda _event: draw_timeline())
        canvas.bind("<Button-1>", set_from_click)
        canvas.bind("<Button-3>", set_from_click)
        start_var.trace_add("write", lambda *_: draw_timeline())
        end_var.trace_add("write", lambda *_: draw_timeline())
        window.after(80, draw_timeline)

    def open_piano_roll_editor(self) -> None:
        try:
            settings = self.read_settings()
            actions, source_name = self._base_actions_for_current_source(settings)
            notes = scheduled_actions_to_audio_notes(actions)
        except Exception as exc:
            messagebox.showerror("Piano Roll", str(exc))
            return
        if not notes:
            messagebox.showinfo("Piano Roll", "No notes to display. Load a MIDI or enter a text score first.")
            return

        window = tk.Toplevel(self)
        window.title("Piano Roll Editor")
        window.geometry("1020x620")
        window.minsize(800, 440)
        window.configure(bg=getattr(self, "_active_bg", UI_BG))
        window.transient(self)

        bpm = max(1.0, float(self.bpm.get()))
        beat_seconds = 60.0 / bpm
        duration = max(note.end for note in notes)
        total_beats = duration / beat_seconds
        low_midi = min(note.midi for note in notes)
        high_midi = max(note.midi for note in notes)
        low_midi = max(24, low_midi - 4)
        high_midi = min(108, high_midi + 4)
        note_range = high_midi - low_midi + 1

        px_per_beat = 42
        px_per_note = 14
        left_margin = 52
        top_margin = 32
        canvas_width = max(600, left_margin + int(total_beats * px_per_beat) + 40)
        canvas_height = top_margin + note_range * px_per_note + 20

        root = ttk.Frame(window, padding=8)
        root.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(root)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(toolbar, text=f"Piano Roll - {source_name}  |  {len(notes)} notes  |  {bpm:.0f} BPM  |  {midi_to_note_name(low_midi)}-{midi_to_note_name(high_midi)}",
                  style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Apply", command=lambda: apply_changes(), style="Accent.TButton").pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(toolbar, text="Preview", command=lambda: preview_roll()).pack(side=tk.RIGHT, padx=4)
        ttk.Button(toolbar, text="Add Note", command=lambda: add_note_at_cursor()).pack(side=tk.RIGHT, padx=4)
        ttk.Button(toolbar, text="Delete Selected", command=lambda: delete_selected()).pack(side=tk.RIGHT, padx=4)

        canvas_frame = ttk.Frame(root)
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        h_scroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        canvas = tk.Canvas(canvas_frame, bg=getattr(self, "_active_field", UI_FIELD),
                           highlightthickness=1, highlightbackground=getattr(self, "_active_border", UI_BORDER),
                           scrollregion=(0, 0, canvas_width, canvas_height))
        canvas.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        h_scroll.configure(command=canvas.xview)
        v_scroll.configure(command=canvas.yview)
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)

        editable_notes: list[dict[str, object]] = [
            {"start": n.start, "end": n.end, "midi": n.midi, "velocity": n.velocity}
            for n in notes
        ]
        selected_index: int | None = [None]
        drag_data: dict[str, object] = {}
        cursor_beat = [0.0]

        def note_color(midi_note: int) -> str:
            name = midi_to_note_name(midi_note)
            if "#" in name:
                return "#1a1a2e"
            return "#16213e"

        def note_color_selected() -> str:
            return "#2980b9"

        def draw_grid() -> None:
            canvas.delete("grid")
            for b in range(0, int(total_beats) + 2):
                x = left_margin + b * px_per_beat
                is_bar = b % 4 == 0
                color = "#3a4455" if is_bar else "#252d38"
                width = 2 if is_bar else 1
                canvas.create_line(x, 0, x, canvas_height, fill=color, width=width, tags="grid")
                if b % 2 == 0:
                    canvas.create_text(x, 14, text=str(b), fill=getattr(self, "_active_muted", UI_MUTED) if hasattr(self, "_active_muted") else UI_MUTED,
                                      font=("Segoe UI", 7), tags="grid")
            for midi_note in range(low_midi, high_midi + 1):
                y = top_margin + (high_midi - midi_note) * px_per_note
                name = midi_to_note_name(midi_note)
                is_black = "#" in name
                color = "#1e2530" if is_black else "#252d38"
                canvas.create_rectangle(left_margin, y, canvas_width, y + px_per_note, fill=color, outline="", tags="grid")
                if not is_black:
                    canvas.create_text(left_margin - 6, y + px_per_note // 2, text=name,
                                      fill=getattr(self, "_active_muted", UI_MUTED) if hasattr(self, "_active_muted") else UI_MUTED,
                                      font=("Segoe UI", 8), anchor="e", tags="grid")

        def draw_notes() -> None:
            canvas.delete("note")
            for idx, note in enumerate(editable_notes):
                x1 = left_margin + float(note["start"]) / beat_seconds * px_per_beat
                x2 = left_margin + float(note["end"]) / beat_seconds * px_per_beat
                y1 = top_margin + (high_midi - int(note["midi"])) * px_per_note + 1
                y2 = y1 + px_per_note - 2
                color = note_color_selected() if idx == selected_index[0] else note_color(int(note["midi"]))
                r = canvas.create_rectangle(x1, y1, max(x1 + 1, x2), y2, fill=color,
                                            outline="#4a90d9" if idx == selected_index[0] else "#3a4a5a",
                                            width=2 if idx == selected_index[0] else 1, tags="note")
                canvas.tag_bind(r, "<Button-1>", lambda e, i=idx: select_note(i, e))
                canvas.tag_bind(r, "<B1-Motion>", lambda e, i=idx: drag_note(i, e))
                canvas.tag_bind(r, "<Button-3>", lambda e, i=idx: delete_note(i))

        def select_note(index: int, event: tk.Event) -> None:
            selected_index[0] = index
            drag_data["start_x"] = event.x
            drag_data["start_y"] = event.y
            drag_data["note_start"] = float(editable_notes[index]["start"])
            drag_data["note_end"] = float(editable_notes[index]["end"])
            drag_data["note_midi"] = int(editable_notes[index]["midi"])
            drag_data["resizing"] = abs(event.x - (left_margin + drag_data["note_end"] / beat_seconds * px_per_beat)) < 8
            draw_notes()

        def drag_note(index: int, event: tk.Event) -> None:
            if selected_index[0] != index:
                return
            dx = (event.x - drag_data.get("start_x", 0)) / px_per_beat * beat_seconds
            dy = -(event.y - drag_data.get("start_y", 0)) / px_per_note
            if drag_data.get("resizing"):
                new_end = max(float(drag_data["note_start"]) + 0.02, float(drag_data["note_end"]) + dx)
                editable_notes[index]["end"] = new_end
            else:
                new_start = max(0.0, float(drag_data["note_start"]) + dx)
                duration = float(drag_data["note_end"]) - float(drag_data["note_start"])
                new_midi = max(0, min(127, int(float(drag_data["note_midi"]) + round(dy))))
                editable_notes[index]["start"] = new_start
                editable_notes[index]["end"] = new_start + duration
                editable_notes[index]["midi"] = new_midi
            draw_notes()

        def delete_note(index: int) -> None:
            editable_notes.pop(index)
            selected_index[0] = None
            draw_notes()

        def delete_selected() -> None:
            if selected_index[0] is not None:
                delete_note(selected_index[0])

        def add_note_at_cursor() -> None:
            midi = low_midi + note_range // 2
            start = cursor_beat[0] * beat_seconds
            editable_notes.append({
                "start": start,
                "end": start + beat_seconds * 0.5,
                "midi": midi,
                "velocity": 84,
            })
            selected_index[0] = len(editable_notes) - 1
            draw_notes()

        def canvas_click(event: tk.Event) -> None:
            if event.x < left_margin:
                return
            beat = (canvas.canvasx(event.x) - left_margin) / px_per_beat
            midi = high_midi - int((canvas.canvasy(event.y) - top_margin) / px_per_note)
            if low_midi <= midi <= high_midi:
                start = max(0.0, beat * beat_seconds)
                editable_notes.append({
                    "start": start,
                    "end": start + beat_seconds * 0.5,
                    "midi": midi,
                    "velocity": 84,
                })
                selected_index[0] = len(editable_notes) - 1
                draw_notes()

        def apply_changes() -> None:
            if not editable_notes:
                return
            actions: list[ScheduledAction] = []
            for note in editable_notes:
                p = Playable("midi", int(note["midi"]))
                actions.append(ScheduledAction(float(note["start"]), "down", (p,)))
                actions.append(ScheduledAction(float(note["end"]), "up", (p,)))
            self.loaded_midi_actions = coalesce_scheduled_actions(actions)
            self.source_mode.set("midi")
            self.loaded_midi_name.set(f"{source_name} (edited)")
            self.status.set(f"Applied piano roll: {len(editable_notes)} notes.")
            self.analyze_current_source(show_popup=False)
            window.destroy()

        def preview_roll() -> None:
            apply_changes()
            self.start_preview_playback()

        canvas.bind("<Button-1>", lambda e: select_note(-1, e) if e.x < left_margin else canvas_click(e))
        canvas.bind("<Button-3>", lambda e: delete_selected() if selected_index[0] is not None else None)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        canvas.bind("<Shift-MouseWheel>", lambda e: canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        for i in range(10):
            canvas.bind(f"<Key-{i}>", lambda e: None)

        draw_grid()
        draw_notes()
        canvas.focus_set()

    def open_online_midi_search_tool(self) -> None:
        window = tk.Toplevel(self)
        window.title("Online MIDI Search")
        window.geometry("900x640")
        window.minsize(780, 540)
        window.configure(bg=UI_BG)
        window.transient(self)

        query_var = tk.StringVar(value="")
        limit_var = tk.IntVar(value=12)
        sort_var = tk.StringVar(value="Best playable")
        status_var = tk.StringVar(value="Search Online Sequencer by song name, artist, URL, or sequence ID.")
        detail_var = tk.StringVar(value="Select a result to see its page. MIDI files are generated locally from page data.")
        auto_repair_var = tk.BooleanVar(value=False)
        auto_trim_art_var = tk.BooleanVar(value=True)
        auto_cleanup_var = tk.BooleanVar(value=True)
        results_by_iid: dict[str, OnlineSequenceResult] = {}
        current_results: dict[str, list[OnlineSequenceResult]] = {"items": []}
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        busy = {"value": False}
        search_history: list[str] = load_search_history()

        root = ttk.Frame(window, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        ttk.Label(root, text="Online MIDI Search", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(root, textvariable=status_var, style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 10))

        search_panel = ttk.Frame(root)
        search_panel.grid(row=2, column=0, sticky="nsew")
        search_panel.columnconfigure(0, weight=1)
        search_panel.rowconfigure(1, weight=1)

        query_row = ttk.Frame(search_panel)
        query_row.grid(row=0, column=0, sticky="ew")
        query_row.columnconfigure(0, weight=1)
        query_entry = ttk.Entry(query_row, textvariable=query_var)
        query_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        history_var = tk.StringVar(value="History")
        history_menu = tk.Menu(query_entry, tearoff=0, bg=UI_SURFACE, fg=UI_TEXT, activebackground=UI_SURFACE_HOVER, activeforeground=UI_TEXT)
        history_button = ttk.Button(query_row, text="\u25BE", width=3, command=lambda: show_history_menu())
        history_button.grid(row=0, column=1, padx=(0, 8))

        def show_history_menu() -> None:
            history_menu.delete(0, tk.END)
            if not search_history:
                history_menu.add_command(label="(empty)", state=tk.DISABLED)
            else:
                for item in search_history[:20]:
                    history_menu.add_command(
                        label=item[:60] + ("..." if len(item) > 60 else ""),
                        command=lambda q=item: query_var.set(q) or start_search(),
                    )
                history_menu.add_separator()
                history_menu.add_command(label="Clear history", command=lambda: clear_history())
            try:
                history_menu.tk_popup(
                    query_entry.winfo_rootx(),
                    query_entry.winfo_rooty() + query_entry.winfo_height(),
                )
            finally:
                history_menu.grab_release()

        def clear_history() -> None:
            nonlocal search_history
            search_history = []
            save_search_history(search_history)

        ttk.Label(query_row, text="Limit").grid(row=0, column=2, padx=(0, 5))
        limit_box = ttk.Combobox(query_row, textvariable=limit_var, values=[6, 8, 12, 16, 20, 30, 50], width=6, state="readonly")
        limit_box.grid(row=0, column=3, padx=(0, 8))
        ttk.Label(query_row, text="Sort").grid(row=0, column=5, padx=(0, 5))
        sort_box = ttk.Combobox(
            query_row,
            textvariable=sort_var,
            values=list(ONLINE_SEARCH_SORT_OPTIONS),
            width=16,
            state="readonly",
        )
        sort_box.grid(row=0, column=6)

        tree_frame = ttk.Frame(search_panel)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("title", "author", "id", "plays", "notes", "updated", "source")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse", height=12)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        headings = {
            "title": ("Title", 330),
            "author": ("Author", 120),
            "id": ("ID", 78),
            "plays": ("Plays", 74),
            "notes": ("Notes", 74),
            "updated": ("Updated", 94),
            "source": ("Source", 90),
        }
        def current_selected_sequence_id() -> str | None:
            selection = tree.selection()
            if not selection:
                return None
            result = results_by_iid.get(selection[0])
            return result.sequence_id if result is not None else None

        def apply_column_sort(column: str) -> None:
            column_sorts = {
                "title": "Title A-Z",
                "author": "Author A-Z",
                "plays": "Most plays",
                "updated": "Newest",
            }
            if column == "notes":
                sort_var.set("Fewest notes" if sort_var.get() == "Most notes" else "Most notes")
            elif column in column_sorts:
                sort_var.set(column_sorts[column])
            else:
                sort_var.set("Best match")
            render_results(current_selected_sequence_id())
            if current_results["items"]:
                status_var.set(f"Sorted {len(current_results['items'])} results by {sort_var.get()}.")

        for column, (heading, width) in headings.items():
            tree.heading(column, text=heading, command=lambda selected_column=column: apply_column_sort(selected_column))
            tree.column(column, width=width, minwidth=60, anchor="w", stretch=column == "title")
        tree.column("id", anchor="center", stretch=False)
        tree.column("plays", anchor="e", stretch=False)
        tree.column("notes", anchor="e", stretch=False)

        detail_label = ttk.Label(search_panel, textvariable=detail_var, wraplength=820, justify=tk.LEFT, style="Muted.TLabel")
        detail_label.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        progress = ttk.Progressbar(search_panel, mode="indeterminate")
        progress.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        options_row = ttk.Frame(search_panel)
        options_row.grid(row=4, column=0, sticky="ew")
        options_row.columnconfigure(3, weight=1)
        ttk.Checkbutton(options_row, text="Run timing repair after load", variable=auto_repair_var).grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Checkbutton(options_row, text="Auto-skip note-art intro", variable=auto_trim_art_var).grid(
            row=0, column=1, sticky="w", padx=(0, 12)
        )
        ttk.Checkbutton(options_row, text="Auto-clean for Roblox", variable=auto_cleanup_var).grid(
            row=0, column=2, sticky="w", padx=(0, 12)
        )

        button_row = ttk.Frame(root)
        button_row.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        for column in range(7):
            button_row.columnconfigure(column, weight=1 if column in {0, 1} else 0)

        def selected_result() -> OnlineSequenceResult | None:
            selection = tree.selection()
            if not selection:
                status_var.set("Select a result first.")
                return None
            return results_by_iid.get(selection[0])

        def set_busy(enabled: bool, text: str | None = None) -> None:
            busy["value"] = enabled
            if text:
                status_var.set(text)
            widgets = (
                search_button,
                download_load_button,
                download_button,
                direct_url_button,
                open_page_button,
                copy_url_button,
            )
            for widget in widgets:
                widget.configure(state=tk.DISABLED if enabled else tk.NORMAL)
            sort_box.configure(state=tk.DISABLED if enabled else "readonly")
            limit_box.configure(state=tk.DISABLED if enabled else "readonly")
            if enabled:
                progress.start(12)
            else:
                progress.stop()

        def render_results(preferred_sequence_id: str | None = None) -> None:
            results_by_iid.clear()
            tree.delete(*tree.get_children())
            sorted_results = sort_online_sequence_results(current_results["items"], query_var.get(), sort_var.get())
            for index, result in enumerate(sorted_results):
                iid = f"{result.sequence_id}-{index}"
                results_by_iid[iid] = result
                tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        result.title,
                        result.author or "-",
                        result.sequence_id,
                        result.plays or "-",
                        result.notes or "-",
                        result.updated or "-",
                        result.source or "-",
                    ),
                )
            children = tree.get_children()
            if children:
                selected = children[0]
                if preferred_sequence_id is not None:
                    for iid in children:
                        candidate = results_by_iid.get(iid)
                        if candidate is not None and candidate.sequence_id == preferred_sequence_id:
                            selected = iid
                            break
                tree.selection_set(selected)
                tree.focus(selected)
                update_details()

        def fill_results(results: list[OnlineSequenceResult]) -> None:
            current_results["items"] = results
            render_results()

        def resort_from_dropdown() -> None:
            render_results(current_selected_sequence_id())
            if current_results["items"]:
                status_var.set(f"Sorted {len(current_results['items'])} results by {sort_var.get()}.")

        def poll_results() -> None:
            try:
                kind, payload = result_queue.get_nowait()
            except queue.Empty:
                if busy["value"]:
                    window.after(100, poll_results)
                return

            set_busy(False)
            if kind == "search_ok":
                results = payload if isinstance(payload, list) else []
                fill_results(results)
                status_var.set(
                    f"Found {len(results)} Online Sequencer result{'s' if len(results) != 1 else ''}; "
                    f"sorted by {sort_var.get()}."
                )
                if not results:
                    detail_var.set("No results found. Try a more specific title, artist, URL, or sequence ID.")
            elif kind == "download_ok":
                path, result = payload
                if isinstance(path, Path) and isinstance(result, OnlineSequenceResult):
                    status_var.set(f"Downloaded: {path.name}")
                    detail_var.set(f"Saved to {path}")
            elif kind == "load_ok":
                if isinstance(payload, PreparedOnlineMidi):
                    try:
                        self._apply_prepared_online_midi(payload)
                        status_var.set(f"Downloaded and loaded: {payload.path.name}")
                        detail_var.set(f"{payload.summary}\nLoaded {payload.result.title} from {payload.result.url}")
                    except Exception as exc:
                        messagebox.showerror("MIDI load failed", str(exc))
                        status_var.set("Downloaded, but loading failed.")
            elif kind == "error":
                message = str(payload)
                status_var.set(message)
                detail_var.set(message)
            else:
                status_var.set("Finished.")

        def start_search() -> None:
            if busy["value"]:
                return
            query = query_var.get().strip()
            if not query:
                status_var.set("Type a song name, Online Sequencer URL, or sequence ID first.")
                return
            nonlocal search_history
            search_history = add_to_search_history(query, search_history)
            save_search_history(search_history)
            try:
                limit = max(1, min(50, int(limit_var.get())))
            except Exception:
                limit = 12
            set_busy(True, f"Searching Online Sequencer for: {query}")
            detail_var.set("Searching public Online Sequencer pages, web results, and cleaned query variants.")

            def worker() -> None:
                try:
                    result_queue.put(("search_ok", search_online_sequences(query, limit=limit, sort_by=sort_var.get())))
                except Exception as exc:
                    result_queue.put(("error", str(exc)))

            threading.Thread(target=worker, daemon=True).start()
            window.after(100, poll_results)

        def download_selected(load_after: bool) -> None:
            if busy["value"]:
                return
            result = selected_result()
            if result is None:
                return
            auto_repair = bool(auto_repair_var.get())
            auto_trim_art = bool(auto_trim_art_var.get())
            auto_cleanup = bool(auto_cleanup_var.get())
            repair_settings = self._online_midi_repair_settings()
            if load_after:
                set_busy(True, f"Downloading and preparing MIDI for: {result.title}")
            else:
                set_busy(True, f"Downloading MIDI for: {result.title}")
            detail_var.set(f"Generating MIDI locally from {result.url}")

            def worker() -> None:
                try:
                    if load_after:
                        prepared = prepare_online_midi_load(
                            result,
                            auto_repair,
                            repair_settings,
                            trim_note_art_intro=auto_trim_art,
                            cleanup_playability=auto_cleanup,
                        )
                        result_queue.put(("load_ok", prepared))
                    else:
                        path = download_online_sequence_midi(result)
                        result_queue.put(("download_ok", (path, result)))
                except Exception as exc:
                    result_queue.put(("error", str(exc)))

            threading.Thread(target=worker, daemon=True).start()
            window.after(100, poll_results)

        def load_direct_midi_url() -> None:
            if busy["value"]:
                return
            url = simpledialog.askstring("Load MIDI URL", "Paste a direct .mid/.midi URL:", parent=window)
            if not url:
                return
            auto_trim_art = bool(auto_trim_art_var.get())
            auto_cleanup = bool(auto_cleanup_var.get())
            cleanup_settings = self._online_midi_repair_settings()
            set_busy(True, "Downloading direct MIDI URL.")
            detail_var.set(url)

            def worker() -> None:
                try:
                    prepared = prepare_direct_midi_load(
                        url,
                        trim_note_art_intro=auto_trim_art,
                        cleanup_playability=auto_cleanup,
                        cleanup_settings=cleanup_settings,
                    )
                    result_queue.put(("load_ok", prepared))
                except Exception as exc:
                    result_queue.put(("error", str(exc)))

            threading.Thread(target=worker, daemon=True).start()
            window.after(100, poll_results)

        def update_details(_event: object | None = None) -> None:
            result = selected_result()
            if result is None:
                return
            parts = [f"{result.title}"]
            if result.author:
                parts.append(f"by {result.author}")
            facts = []
            if result.plays:
                facts.append(f"{result.plays} plays")
            if result.notes:
                facts.append(f"{result.notes} notes")
            if result.updated:
                facts.append(f"updated {result.updated}")
            if facts:
                parts.append(" | ".join(facts))
            parts.append(result.url)
            parts.append("MIDI export: generated locally from Online Sequencer page data")
            detail_var.set("\n".join(parts))

        def open_selected_page() -> None:
            result = selected_result()
            if result is not None:
                webbrowser.open(result.url)

        def copy_selected_page_url() -> None:
            result = selected_result()
            if result is None:
                return
            window.clipboard_clear()
            window.clipboard_append(result.url)
            status_var.set("Copied Online Sequencer page URL.")

        search_button = ttk.Button(button_row, text="Search", command=start_search, style="Accent.TButton")
        search_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        download_load_button = ttk.Button(
            button_row,
            text="Download & Load",
            command=lambda: download_selected(True),
            style="Accent.TButton",
        )
        download_load_button.grid(row=0, column=1, sticky="ew", padx=6)
        download_button = ttk.Button(button_row, text="Download Only", command=lambda: download_selected(False))
        download_button.grid(row=0, column=2, padx=6)
        direct_url_button = ttk.Button(button_row, text="Load MIDI URL", command=load_direct_midi_url)
        direct_url_button.grid(row=0, column=3, padx=6)
        open_page_button = ttk.Button(button_row, text="Open Page", command=open_selected_page)
        open_page_button.grid(row=0, column=4, padx=6)
        copy_url_button = ttk.Button(button_row, text="Copy Page URL", command=copy_selected_page_url)
        copy_url_button.grid(row=0, column=5, padx=6)
        ttk.Button(button_row, text="Close", command=window.destroy).grid(row=0, column=6, padx=(6, 0))

        query_entry.bind("<Return>", lambda _event: start_search())
        sort_box.bind("<<ComboboxSelected>>", lambda _event: resort_from_dropdown())
        tree.bind("<<TreeviewSelect>>", update_details)
        tree.bind("<Double-Button-1>", lambda _event: download_selected(True))
        query_entry.focus_set()

    def open_audio_to_midi_tool(self, prefill_path: str = "") -> None:
        window = tk.Toplevel(self)
        window.title("Audio to MIDI")
        window.geometry("800x820")
        window.minsize(720, 720)
        window.configure(bg=UI_BG)
        window.transient(self)

        audio_path = tk.StringVar(value=prefill_path)
        preset = tk.StringVar(value="Best JJS arrangement")
        mode = tk.StringVar(value="Basic Pitch AI")
        arrangement_style_var = tk.StringVar(value="Balanced JJS")
        bpm_var = tk.DoubleVar(value=float(self.bpm.get()))
        auto_bpm_var = tk.BooleanVar(value=True)
        auto_timing_var = tk.BooleanVar(value=True)
        local_timing_var = tk.BooleanVar(value=True)
        smart_arranger_var = tk.BooleanVar(value=True)
        low_note_var = tk.StringVar(value="C2")
        high_note_var = tk.StringVar(value=self.high_note.get())
        sensitivity_var = tk.DoubleVar(value=0.55)
        min_note_beats_var = tk.DoubleVar(value=0.25)
        grid_beats_var = tk.DoubleVar(value=0.25)
        arrangement_strength_var = tk.DoubleVar(value=0.45)
        timing_nudge_ms_var = tk.DoubleVar(value=-25.0)
        sample_rate_var = tk.IntVar(value=22050)
        max_polyphony_var = tk.IntVar(value=4)
        max_note_beats_var = tk.DoubleVar(value=3.0)
        melody_boost_var = tk.DoubleVar(value=1.05)
        trim_silence_var = tk.BooleanVar(value=True)
        harmonic_only_var = tk.BooleanVar(value=True)
        melodia_trick_var = tk.BooleanVar(value=True)
        multipass_ai_var = tk.BooleanVar(value=True)
        key_cleanup_var = tk.BooleanVar(value=True)
        keep_bass_var = tk.BooleanVar(value=True)
        result_var = tk.StringVar(value="Choose an audio file, then convert.")

        root = ttk.Frame(window, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        def choose_audio() -> None:
            path = filedialog.askopenfilename(
                title="Choose audio",
                filetypes=[
                    ("Audio files", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac"),
                    ("All files", "*.*"),
                ],
            )
            if path:
                audio_path.set(path)
                result_var.set(f"Selected: {Path(path).name}")

        path_frame = ttk.Frame(root)
        path_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        path_frame.columnconfigure(0, weight=1)
        ttk.Label(path_frame, text="Audio file").grid(row=0, column=0, sticky="w", pady=(0, 4))
        entry_frame = ttk.Frame(path_frame)
        entry_frame.grid(row=1, column=0, sticky="ew")
        entry_frame.columnconfigure(0, weight=1)
        ttk.Entry(entry_frame, textvariable=audio_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(entry_frame, text="Browse", command=choose_audio).grid(row=0, column=1, padx=(6, 0))

        notebook = ttk.Notebook(root)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        basic_tab = ttk.Frame(notebook, padding=(12, 12, 12, 8))
        basic_tab.columnconfigure(1, weight=1)
        notebook.add(basic_tab, text="Basic")

        adv_tab = ttk.Frame(notebook, padding=(12, 12, 12, 8))
        adv_tab.columnconfigure(1, weight=1)
        notebook.add(adv_tab, text="Advanced")

        def b_row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        def a_row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        # ── Basic tab ──
        br = 0
        b_row(basic_tab, br, "Preset", ttk.Combobox(
            basic_tab, textvariable=preset, state="readonly",
            values=["Best JJS arrangement", "Lead melody only", "Rich piano arrangement",
                    "Dense AI transcription", "AI clean piano", "Fallback melody", "Fallback chords"],
        )); br += 1
        b_row(basic_tab, br, "Mode", ttk.Combobox(
            basic_tab, textvariable=mode, state="readonly",
            values=["Basic Pitch AI", "Melody / lead line", "Spectral chords"],
        )); br += 1
        b_row(basic_tab, br, "Arrangement", ttk.Combobox(
            basic_tab, textvariable=arrangement_style_var, state="readonly",
            values=["Balanced JJS", "Lead melody", "Rich piano", "Dense transcription", "Raw transcription"],
        )); br += 1

        bpm_f = ttk.Frame(basic_tab)
        bpm_f.columnconfigure(0, weight=1)
        ttk.Spinbox(bpm_f, textvariable=bpm_var, from_=40, to=260, increment=1).grid(row=0, column=0, sticky="ew")
        ttk.Button(bpm_f, text="Estimate", command=lambda: estimate_bpm_from_audio()).grid(row=0, column=1, padx=(6, 0))
        ttk.Checkbutton(bpm_f, text="Auto", variable=auto_bpm_var).grid(row=0, column=2, padx=(6, 0))
        b_row(basic_tab, br, "BPM", bpm_f); br += 1

        rng_f = ttk.Frame(basic_tab)
        rng_f.columnconfigure(0, weight=1); rng_f.columnconfigure(1, weight=1)
        ttk.Combobox(rng_f, textvariable=low_note_var, values=NOTE_OPTIONS, state="readonly").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Combobox(rng_f, textvariable=high_note_var, values=NOTE_OPTIONS, state="readonly").grid(row=0, column=1, sticky="ew", padx=(4, 0))
        b_row(basic_tab, br, "Note range", rng_f); br += 1

        b_row(basic_tab, br, "Sensitivity", ttk.Scale(basic_tab, variable=sensitivity_var, from_=0.15, to=0.95, orient=tk.HORIZONTAL)); br += 1
        b_row(basic_tab, br, "Min note beats", ttk.Spinbox(basic_tab, textvariable=min_note_beats_var, from_=0.05, to=2.0, increment=0.05)); br += 1

        opts_b = ttk.Frame(basic_tab)
        opts_b.grid(row=br, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(opts_b, text="Trim silence", variable=trim_silence_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(opts_b, text="Harmonic only", variable=harmonic_only_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(opts_b, text="Key cleanup", variable=key_cleanup_var).pack(side=tk.LEFT)

        # ── Advanced tab ──
        ar = 0
        a_row(adv_tab, ar, "Snap grid beats", ttk.Spinbox(adv_tab, textvariable=grid_beats_var, from_=0.0, to=1.0, increment=0.025)); ar += 1
        a_row(adv_tab, ar, "Snap strength", ttk.Scale(adv_tab, variable=arrangement_strength_var, from_=0.0, to=1.0, orient=tk.HORIZONTAL)); ar += 1
        a_row(adv_tab, ar, "Timing nudge ms", ttk.Spinbox(adv_tab, textvariable=timing_nudge_ms_var, from_=-250, to=250, increment=5)); ar += 1
        a_row(adv_tab, ar, "Sample rate", ttk.Combobox(adv_tab, textvariable=sample_rate_var, values=[16000, 22050, 32000, 44100], state="readonly", width=10)); ar += 1
        a_row(adv_tab, ar, "Max polyphony", ttk.Spinbox(adv_tab, textvariable=max_polyphony_var, from_=1, to=10, increment=1)); ar += 1
        a_row(adv_tab, ar, "Max note beats", ttk.Spinbox(adv_tab, textvariable=max_note_beats_var, from_=0.25, to=16.0, increment=0.25)); ar += 1
        a_row(adv_tab, ar, "Melody priority", ttk.Scale(adv_tab, variable=melody_boost_var, from_=0.0, to=2.0, orient=tk.HORIZONTAL)); ar += 1

        opts_a = ttk.Frame(adv_tab)
        opts_a.grid(row=ar, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        opts_a.columnconfigure(0, weight=1); opts_a.columnconfigure(1, weight=1)
        adv_opts = [
            ("Basic Pitch melody trick", melodia_trick_var),
            ("Multi-pass AI rescue", multipass_ai_var),
            ("Auto timing phase", auto_timing_var),
            ("Local beat grid", local_timing_var),
            ("Smart arranger", smart_arranger_var),
            ("Keep bass on strong beats", keep_bass_var),
        ]
        for idx, (text, var) in enumerate(adv_opts):
            col = idx % 2
            ttk.Checkbutton(opts_a, text=text, variable=var).grid(row=idx // 2, column=col, sticky="w", padx=(0, 16) if col == 0 else (16, 0), pady=2)

        progress = ttk.Progressbar(root, mode="indeterminate")
        progress.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(root, textvariable=result_var, wraplength=680, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=3, column=0, sticky="ew", pady=(0, 10)
        )
        buttons = ttk.Frame(root)
        buttons.grid(row=4, column=0, sticky="ew")
        for column in range(4):
            buttons.columnconfigure(column, weight=1)
        job_active = {"value": False}
        worker_state: dict[str, subprocess.Popen[bytes] | None] = {"process": None}

        def apply_preset(*_: object) -> None:
            selected = preset.get()
            if selected == "Best JJS arrangement":
                mode.set("Basic Pitch AI")
                arrangement_style_var.set("Balanced JJS")
                sensitivity_var.set(0.64)
                min_note_beats_var.set(0.08)
                grid_beats_var.set(0.125)
                arrangement_strength_var.set(0.42)
                sample_rate_var.set(32000)
                max_polyphony_var.set(4)
                max_note_beats_var.set(3.0)
                melody_boost_var.set(1.05)
                timing_nudge_ms_var.set(-25.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(True)
                harmonic_only_var.set(True)
                melodia_trick_var.set(True)
                multipass_ai_var.set(True)
                key_cleanup_var.set(True)
                keep_bass_var.set(True)
            elif selected == "Lead melody only":
                mode.set("Basic Pitch AI")
                arrangement_style_var.set("Lead melody")
                sensitivity_var.set(0.70)
                min_note_beats_var.set(0.06)
                grid_beats_var.set(0.125)
                arrangement_strength_var.set(0.50)
                sample_rate_var.set(32000)
                max_polyphony_var.set(1)
                max_note_beats_var.set(2.5)
                melody_boost_var.set(1.45)
                timing_nudge_ms_var.set(-30.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(True)
                harmonic_only_var.set(False)
                melodia_trick_var.set(True)
                multipass_ai_var.set(True)
                key_cleanup_var.set(True)
                keep_bass_var.set(False)
            elif selected == "Rich piano arrangement":
                mode.set("Basic Pitch AI")
                arrangement_style_var.set("Rich piano")
                sensitivity_var.set(0.62)
                min_note_beats_var.set(0.06)
                grid_beats_var.set(0.125)
                arrangement_strength_var.set(0.35)
                sample_rate_var.set(44100)
                max_polyphony_var.set(6)
                max_note_beats_var.set(4.0)
                melody_boost_var.set(0.85)
                timing_nudge_ms_var.set(-15.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(True)
                harmonic_only_var.set(True)
                melodia_trick_var.set(False)
                multipass_ai_var.set(True)
                key_cleanup_var.set(True)
                keep_bass_var.set(True)
            elif selected == "Dense AI transcription":
                mode.set("Basic Pitch AI")
                arrangement_style_var.set("Dense transcription")
                sensitivity_var.set(0.78)
                min_note_beats_var.set(0.04)
                grid_beats_var.set(0.0625)
                arrangement_strength_var.set(0.20)
                sample_rate_var.set(44100)
                max_polyphony_var.set(8)
                max_note_beats_var.set(6.0)
                melody_boost_var.set(0.55)
                timing_nudge_ms_var.set(0.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(False)
                harmonic_only_var.set(False)
                melodia_trick_var.set(False)
                multipass_ai_var.set(True)
                key_cleanup_var.set(False)
                keep_bass_var.set(True)
            elif selected == "AI clean piano":
                mode.set("Basic Pitch AI")
                arrangement_style_var.set("Balanced JJS")
                sensitivity_var.set(0.58)
                min_note_beats_var.set(0.12)
                grid_beats_var.set(0.25)
                arrangement_strength_var.set(0.50)
                sample_rate_var.set(32000)
                max_polyphony_var.set(4)
                max_note_beats_var.set(4.0)
                melody_boost_var.set(0.95)
                timing_nudge_ms_var.set(-10.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(True)
                harmonic_only_var.set(True)
                melodia_trick_var.set(False)
                multipass_ai_var.set(False)
                key_cleanup_var.set(True)
                keep_bass_var.set(True)
            elif selected == "Fallback melody":
                mode.set("Melody / lead line")
                arrangement_style_var.set("Lead melody")
                sensitivity_var.set(0.62)
                min_note_beats_var.set(0.15)
                grid_beats_var.set(0.125)
                arrangement_strength_var.set(0.50)
                sample_rate_var.set(32000)
                max_polyphony_var.set(1)
                max_note_beats_var.set(3.0)
                melody_boost_var.set(1.35)
                timing_nudge_ms_var.set(0.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(False)
                harmonic_only_var.set(True)
                melodia_trick_var.set(True)
                multipass_ai_var.set(False)
                key_cleanup_var.set(True)
                keep_bass_var.set(False)
            elif selected == "Fallback chords":
                mode.set("Spectral chords")
                arrangement_style_var.set("Balanced JJS")
                sensitivity_var.set(0.50)
                min_note_beats_var.set(0.20)
                grid_beats_var.set(0.25)
                arrangement_strength_var.set(0.55)
                sample_rate_var.set(32000)
                max_polyphony_var.set(4)
                max_note_beats_var.set(3.0)
                melody_boost_var.set(0.80)
                timing_nudge_ms_var.set(0.0)
                auto_bpm_var.set(True)
                auto_timing_var.set(True)
                local_timing_var.set(True)
                smart_arranger_var.set(False)
                harmonic_only_var.set(True)
                melodia_trick_var.set(True)
                multipass_ai_var.set(False)
                key_cleanup_var.set(True)
                keep_bass_var.set(True)

        preset.trace_add("write", apply_preset)
        apply_preset()

        def summarize_notes(
            notes: list[AudioMidiNote],
            elapsed: float,
            raw_note_count: int | None = None,
            source_counts: dict[str, object] | None = None,
            warnings: list[object] | None = None,
            bpm_used: float | None = None,
            detected_key: object | None = None,
            timing_offset_seconds: object | None = None,
            timing_offset_confidence: object | None = None,
            timing_nudge_ms: object | None = None,
            timing_grid_count: object | None = None,
            timing_grid_confidence: object | None = None,
            timing_grid_tempo: object | None = None,
            selected_arrangement: object | None = None,
            arrangement_quality_score: object | None = None,
            arrangement_candidates: object | None = None,
        ) -> str:
            source_text = ""
            if source_counts:
                readable = ", ".join(f"{key}: {value}" for key, value in source_counts.items())
                source_text = f"Sources: {readable}.\n"
            warning_text = ""
            if warnings:
                warning_text = "\n" + "\n".join(f"Warning: {item}" for item in warnings)
            bpm_text = f"BPM used: {bpm_used:.1f}.\n" if bpm_used is not None else ""
            key_text = f"Detected key: {detected_key}.\n" if detected_key else ""
            timing_text = ""
            grid_count = 0
            try:
                grid_count = int(timing_grid_count or 0)
            except Exception:
                grid_count = 0
            if grid_count > 0:
                try:
                    grid_confidence = float(timing_grid_confidence or 0.0)
                    grid_tempo = float(timing_grid_tempo or 0.0)
                    tempo_text = f", {grid_tempo:.1f} BPM" if grid_tempo > 0 else ""
                    timing_text = f"Local beat grid: {grid_count} points ({grid_confidence:.2f}{tempo_text}).\n"
                except Exception:
                    timing_text = f"Local beat grid: {grid_count} points.\n"
            elif timing_offset_seconds is not None:
                try:
                    timing_ms = float(timing_offset_seconds) * 1000.0
                    confidence = float(timing_offset_confidence or 0.0)
                    timing_text = f"Timing phase: {timing_ms:.0f}ms ({confidence:.2f}).\n"
                except Exception:
                    timing_text = ""
            if timing_nudge_ms is not None:
                try:
                    timing_text += f"Timing nudge: {float(timing_nudge_ms):+.0f}ms.\n"
                except Exception:
                    pass
            arranger_text = ""
            try:
                candidate_count = int(arrangement_candidates or 0)
            except Exception:
                candidate_count = 0
            if selected_arrangement and candidate_count > 1:
                try:
                    arranger_text = (
                        f"Smart arranger: {selected_arrangement} "
                        f"({float(arrangement_quality_score or 0.0):.1f}, {candidate_count} candidates).\n"
                    )
                except Exception:
                    arranger_text = f"Smart arranger: {selected_arrangement} ({candidate_count} candidates).\n"
            if not notes:
                raw_text = f" Raw detections: {raw_note_count}." if raw_note_count is not None else ""
                return (
                    f"Converted 0 notes.{raw_text}\n"
                    f"{bpm_text}"
                    f"{key_text}"
                    f"{timing_text}"
                    f"{arranger_text}"
                    f"{source_text}"
                    f"Try Lead melody only, raise sensitivity, or use a clearer audio file."
                    f"{warning_text}"
                )
            low_note = midi_to_note_name(min(note.midi for note in notes))
            high_note = midi_to_note_name(max(note.midi for note in notes))
            duration = max(note.end for note in notes)
            polyphony = max_audio_polyphony(notes)
            average_polyphony = average_audio_polyphony(notes)
            key_conflicts = max_visual_piano_key_conflicts(notes)
            raw_text = ""
            if raw_note_count is not None and raw_note_count != len(notes):
                raw_text = f"Merged detections: {raw_note_count} -> arranged notes: {len(notes)}.\n"
            return (
                f"Converted {len(notes)} notes in {elapsed:.1f}s.\n"
                f"{raw_text}"
                f"{bpm_text}"
                f"{key_text}"
                f"{timing_text}"
                f"{arranger_text}"
                f"{source_text}"
                f"Result length: {duration:.1f}s | Range: {low_note}-{high_note} | Avg/Max held: {average_polyphony:.1f}/{polyphony} | Key conflicts: {key_conflicts}\n"
                "Use Load as MIDI to play/preview, Send Text to edit/save, or Save MIDI."
                f"{warning_text}"
            )

        def finish_conversion(
            notes: list[AudioMidiNote],
            elapsed: float,
            raw_note_count: int | None = None,
            source_counts: dict[str, object] | None = None,
            warnings: list[object] | None = None,
            bpm_used: float | None = None,
            detected_key: object | None = None,
            timing_offset_seconds: object | None = None,
            timing_offset_confidence: object | None = None,
            timing_nudge_ms: object | None = None,
            timing_grid_count: object | None = None,
            timing_grid_confidence: object | None = None,
            timing_grid_tempo: object | None = None,
            selected_arrangement: object | None = None,
            arrangement_quality_score: object | None = None,
            arrangement_candidates: object | None = None,
        ) -> None:
            progress.stop()
            self.audio_conversion_notes = notes
            self.audio_conversion_source = audio_path.get()
            self.audio_conversion_bpm = float(bpm_var.get())
            result_var.set(
                summarize_notes(
                    notes,
                    elapsed,
                    raw_note_count,
                    source_counts,
                    warnings,
                    bpm_used,
                    detected_key,
                    timing_offset_seconds,
                    timing_offset_confidence,
                    timing_nudge_ms,
                    timing_grid_count,
                    timing_grid_confidence,
                    timing_grid_tempo,
                    selected_arrangement,
                    arrangement_quality_score,
                    arrangement_candidates,
                )
            )
            self.status.set("Audio conversion finished.")
            if notes:
                self.highlight_midi_notes({note.midi for note in notes[: min(12, len(notes))]})
                self.after(1200, self.clear_keyboard_highlights)

        def fail_conversion(error: Exception) -> None:
            progress.stop()
            job_active["value"] = False
            result_var.set(f"Conversion failed: {error}")
            messagebox.showerror("Audio conversion failed", str(error))

        def start_audio_worker(
            request: dict[str, object],
            on_success: object,
            busy_text: str,
        ) -> None:
            if job_active["value"]:
                result_var.set("A conversion job is already running.")
                return
            try:
                JOB_DIR.mkdir(exist_ok=True)
                job_id = uuid.uuid4().hex
                request_path = JOB_DIR / f"{job_id}_request.json"
                response_path = JOB_DIR / f"{job_id}_response.json"
                request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
                worker_path = Path(__file__).with_name("audio_to_midi_worker.py")
                creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                process = subprocess.Popen(
                    [sys.executable, str(worker_path), str(request_path), str(response_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                worker_state["process"] = process
            except Exception as exc:
                fail_conversion(exc)
                return

            job_active["value"] = True
            progress.start(12)
            result_var.set(busy_text)

            def poll() -> None:
                if response_path.exists():
                    try:
                        data = json.loads(response_path.read_text(encoding="utf-8"))
                        progress.stop()
                        job_active["value"] = False
                        worker_state["process"] = None
                        if not data.get("ok"):
                            raise RuntimeError(str(data.get("error", "Audio worker failed.")))
                        on_success(data)
                    except Exception as exc:
                        job_active["value"] = False
                        worker_state["process"] = None
                        fail_conversion(exc)
                    finally:
                        for path_to_remove in (request_path, response_path):
                            try:
                                path_to_remove.unlink(missing_ok=True)
                            except Exception:
                                pass
                    return

                if process.poll() is not None:
                    progress.stop()
                    job_active["value"] = False
                    worker_state["process"] = None
                    fail_conversion(RuntimeError("Audio worker exited without returning a result."))
                    return

                window.after(150, poll)

            window.after(150, poll)

        def start_conversion() -> None:
            path = audio_path.get().strip()
            if not path:
                messagebox.showerror("Audio conversion", "Choose an audio file first.")
                return
            if not Path(path).exists():
                messagebox.showerror("Audio conversion", "That audio file does not exist.")
                return
            try:
                low_midi = note_name_to_midi(low_note_var.get())
                high_midi = note_name_to_midi(high_note_var.get())
                if low_midi >= high_midi:
                    raise ValueError("Low note must be below high note.")
                bpm = max(1.0, float(bpm_var.get()))
                sensitivity = min(0.95, max(0.05, float(sensitivity_var.get())))
                min_note_beats = max(0.02, float(min_note_beats_var.get()))
                grid_beats = max(0.0, float(grid_beats_var.get()))
                arrangement_strength = max(0.0, min(1.0, float(arrangement_strength_var.get())))
                sample_rate = int(sample_rate_var.get())
                max_polyphony = max(1, min(10, int(max_polyphony_var.get())))
                max_note_beats = max(min_note_beats, float(max_note_beats_var.get()))
                melody_boost = max(0.0, min(2.0, float(melody_boost_var.get())))
                timing_nudge_ms = max(-250.0, min(250.0, float(timing_nudge_ms_var.get())))
            except Exception as exc:
                messagebox.showerror("Audio conversion", str(exc))
                return

            started = time.monotonic()
            selected_mode = mode.get()
            worker_mode = (
                "basic_pitch"
                if selected_mode == "Basic Pitch AI"
                else "melody"
                if selected_mode == "Melody / lead line"
                else "spectral"
            )
            request = {
                "job_type": "convert",
                "path": path,
                "mode": worker_mode,
                "bpm": bpm,
                "low_midi": low_midi,
                "high_midi": high_midi,
                "confidence_threshold": max(0.05, min(0.9, 1.0 - sensitivity)),
                "sensitivity": sensitivity,
                "min_note_beats": min_note_beats,
                "grid_beats": grid_beats,
                "sample_rate": sample_rate,
                "trim_silence": bool(trim_silence_var.get()),
                "harmonic_only": bool(harmonic_only_var.get()),
                "melodia_trick": bool(melodia_trick_var.get()),
                "multipass_ai": bool(multipass_ai_var.get()),
                "max_polyphony": max_polyphony,
                "auto_bpm": bool(auto_bpm_var.get()),
                "arrangement_style": arrangement_style_var.get(),
                "arrangement_auto_timing": bool(auto_timing_var.get()),
                "arrangement_local_timing": bool(local_timing_var.get()),
                "arrangement_smart_select": bool(smart_arranger_var.get()),
                "arrangement_max_polyphony": max_polyphony,
                "arrangement_grid_beats": grid_beats,
                "arrangement_quantize_strength": arrangement_strength,
                "arrangement_min_note_beats": min_note_beats,
                "arrangement_max_note_beats": max_note_beats,
                "arrangement_gap_ms": 18.0,
                "arrangement_keep_bass": bool(keep_bass_var.get()),
                "arrangement_melody_boost": melody_boost,
                "arrangement_key_cleanup": bool(key_cleanup_var.get()),
                "arrangement_timing_nudge_ms": timing_nudge_ms,
            }

            def on_success(data: dict[str, object]) -> None:
                bpm_used = data.get("bpm_used")
                if bpm_used is not None:
                    try:
                        bpm_var.set(round(float(bpm_used), 1))
                    except Exception:
                        pass
                notes = audio_notes_from_dicts(data.get("notes", []))
                raw_note_count = data.get("raw_note_count")
                finish_conversion(
                    notes,
                    float(data.get("elapsed", time.monotonic() - started)),
                    int(raw_note_count) if raw_note_count is not None else None,
                    data.get("source_counts") if isinstance(data.get("source_counts"), dict) else None,
                    data.get("warnings") if isinstance(data.get("warnings"), list) else None,
                    float(bpm_used) if bpm_used is not None else None,
                    data.get("detected_key"),
                    data.get("timing_offset_seconds"),
                    data.get("timing_offset_confidence"),
                    data.get("timing_nudge_ms"),
                    data.get("timing_grid_count"),
                    data.get("timing_grid_confidence"),
                    data.get("timing_grid_tempo"),
                    data.get("selected_arrangement"),
                    data.get("arrangement_quality_score"),
                    data.get("arrangement_candidates"),
                )

            start_audio_worker(request, on_success, "Converting audio in a worker process. The studio will stay responsive.")

        def estimate_bpm_from_audio() -> None:
            path = audio_path.get().strip()
            if not path:
                messagebox.showerror("BPM estimate", "Choose an audio file first.")
                return
            if not Path(path).exists():
                messagebox.showerror("BPM estimate", "That audio file does not exist.")
                return
            request = {
                "job_type": "estimate_bpm",
                "path": path,
                "sample_rate": int(sample_rate_var.get()),
                "trim_silence": bool(trim_silence_var.get()),
            }

            def on_success(data: dict[str, object]) -> None:
                tempo = float(data["tempo"])
                bpm_var.set(round(tempo))
                result_var.set(f"Estimated BPM: {tempo:.1f}. Adjust manually if it feels off.")

            start_audio_worker(request, on_success, "Estimating BPM in a worker process...")

        def require_result() -> list[AudioMidiNote] | None:
            if not self.audio_conversion_notes:
                result_var.set("Convert audio first.")
                return None
            return self.audio_conversion_notes

        def load_as_midi() -> None:
            notes = require_result()
            if notes is None:
                return
            self.loaded_midi_actions = audio_notes_to_actions(notes)
            source = Path(self.audio_conversion_source or "audio").stem
            self.loaded_midi_name.set(f"{source} (audio)")
            self.source_mode.set("midi")
            self.bpm.set(float(bpm_var.get()))
            self.status.set("Loaded arranged audio conversion as MIDI.")
            self.analyze_current_source(show_popup=False)

        def send_text_to_editor() -> None:
            notes = require_result()
            if notes is None:
                return
            text = audio_notes_to_text(notes, float(bpm_var.get()))
            self.score_text.delete("1.0", tk.END)
            self.score_text.insert("1.0", text)
            source = Path(self.audio_conversion_source or "Audio Conversion").stem
            self.song_title.set(source)
            self.song_notes.set("Converted from audio. Review rhythm/range, then save to library.")
            self.source_mode.set("text")
            self.bpm.set(float(bpm_var.get()))
            self.status.set("Sent audio conversion to text editor.")
            self.analyze_current_source(show_popup=False)

        def save_midi() -> None:
            notes = require_result()
            if notes is None:
                return
            source = Path(self.audio_conversion_source or "audio_conversion").stem
            path = filedialog.asksaveasfilename(
                title="Save converted MIDI",
                defaultextension=".mid",
                initialfile=f"{source}.mid",
                filetypes=[("MIDI files", "*.mid"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                write_audio_notes_to_midi(notes, float(bpm_var.get()), path)
                result_var.set(f"Saved MIDI: {Path(path).name}")
            except Exception as exc:
                messagebox.showerror("MIDI save failed", str(exc))

        ttk.Button(buttons, text="Convert", command=start_conversion, style="Accent.TButton").grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(buttons, text="Load as MIDI", command=load_as_midi).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(buttons, text="Send Text", command=send_text_to_editor).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(buttons, text="Save MIDI", command=save_midi).grid(row=0, column=3, sticky="ew", padx=(4, 0))

        details = (
            "Best JJS arrangement uses Basic Pitch plus a Roblox-friendly arranger: melody first, limited chords, "
            "strong-beat bass, softer timing, auto BPM, auto timing phase, key cleanup, and optional multi-pass rescue. "
            "Dense transcription is only useful for very clean sources."
        )
        ttk.Label(root, text=details, wraplength=640, justify=tk.LEFT, style="Muted.TLabel").grid(
            row=5, column=0, sticky="ew", pady=(16, 0)
        )

        def close_audio_tool() -> None:
            process = worker_state.get("process")
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_audio_tool)

    def _actions_for_current_source(self, settings: PlaybackSettings) -> tuple[list[ScheduledAction], str]:
        actions, source_name = self._base_actions_for_current_source(settings)
        return window_scheduled_actions(actions, settings.start_offset_seconds, settings.end_at_seconds), source_name

    def _base_actions_for_current_source(self, settings: PlaybackSettings) -> tuple[list[ScheduledAction], str]:
        if self.source_mode.get() == "midi" and self.loaded_midi_actions:
            return self.loaded_midi_actions, self.loaded_midi_name.get()
        text = self.score_text.get("1.0", tk.END)
        events = parse_text_score(text, settings.default_beats)
        return text_events_to_actions(events, settings), self.song_title.get().strip() or "text score"

    def _midi_notes_from_actions(self, actions: list[ScheduledAction]) -> list[int]:
        notes: list[int] = []
        for action in actions:
            if action.action != "down":
                continue
            for playable in action.notes:
                if playable.kind == "midi":
                    notes.append(int(playable.value))
        return notes

    def _suggest_transpose(self, notes: list[int], low_midi: int, high_midi: int) -> int:
        if not notes:
            return int(self.transpose.get())
        best: tuple[int, int, int, int] | None = None
        best_transpose = int(self.transpose.get())
        for transpose in range(-36, 37):
            shifted = [note + transpose for note in notes]
            out_count = sum(note < low_midi or note > high_midi for note in shifted)
            distance = sum(max(low_midi - note, 0, note - high_midi) for note in shifted)
            octave_preference = 0 if transpose % 12 == 0 else 1
            score = (out_count, distance, octave_preference, abs(transpose))
            if best is None or score < best:
                best = score
                best_transpose = transpose
        return best_transpose

    def _max_simultaneous_notes(self, actions: list[ScheduledAction]) -> int:
        active: set[tuple[str, int | str]] = set()
        max_active = 0
        for action in sorted(actions, key=lambda item: (item.seconds, 0 if item.action == "up" else 1)):
            for playable in action.notes:
                identity = (playable.kind, playable.value)
                if action.action == "down":
                    active.add(identity)
                else:
                    active.discard(identity)
            max_active = max(max_active, len(active))
        return max_active

    def analyze_current_source(self, show_popup: bool = True) -> None:
        try:
            settings = self.read_settings()
            actions, source_name = self._actions_for_current_source(settings)
            if not actions:
                raise ValueError("Nothing to analyze.")
            raw_notes = self._midi_notes_from_actions(actions)
            shifted_notes = [note + settings.transpose for note in raw_notes]
            out_count = sum(note < settings.low_midi or note > settings.high_midi for note in shifted_notes)
            raw_keys = sum(
                1
                for action in actions
                if action.action == "down"
                for playable in action.notes
                if playable.kind == "key"
            )
            duration = max(action.seconds for action in actions) / settings.speed if actions else 0.0
            suggested = self._suggest_transpose(raw_notes, settings.low_midi, settings.high_midi)
            self.last_suggested_transpose = suggested
            conflict_notes = [
                AudioMidiNote(note.start, note.end, note.midi + settings.transpose, note.velocity)
                for note in scheduled_actions_to_audio_notes(actions)
                if settings.low_midi <= note.midi + settings.transpose <= settings.high_midi
            ]
            key_conflicts = max_visual_piano_key_conflicts(conflict_notes)

            chords: list[str] = []
            if raw_notes:
                chord_buckets: dict[int, list[int]] = {}
                anotes = scheduled_actions_to_audio_notes(actions)
                for note in anotes:
                    bucket = int(note.start * settings.bpm / 60.0 / 0.5)
                    chord_buckets.setdefault(bucket, []).append(note.midi + settings.transpose)
                for bucket_notes in chord_buckets.values():
                    if len(bucket_notes) >= 2:
                        ch = self._detect_chord_name(bucket_notes)
                        if ch and ch not in chords:
                            chords.append(ch)
                        if len(chords) >= 6:
                            break

            if raw_notes:
                low_note = midi_to_note_name(min(shifted_notes))
                high_note = midi_to_note_name(max(shifted_notes))
                source_range = f"{low_note}-{high_note}"
            else:
                source_range = "raw keys only"

            chord_text = f"\nChords: {', '.join(chords)}" if chords else ""
            summary = (
                f"{source_name}\n"
                f"Length: {duration:.1f}s | Notes: {len(raw_notes)}"
                f"{f' | Raw keys: {raw_keys}' if raw_keys else ''}\n"
                f"Range after transpose: {source_range}\n"
                f"Out of range: {out_count} | Max held: {self._max_simultaneous_notes(actions)} | Key conflicts: {key_conflicts}\n"
                f"Suggested transpose: {suggested:+d}"
                f"{chord_text}"
            )
            self.analysis_summary.set(summary)
            if show_popup:
                messagebox.showinfo("Song analysis", summary)
        except Exception as exc:
            self.analysis_summary.set(f"Analysis failed: {exc}")
            if show_popup:
                messagebox.showerror("Analysis failed", str(exc))

    def apply_suggested_transpose(self) -> None:
        if self.last_suggested_transpose is None:
            self.analyze_current_source(show_popup=False)
        if self.last_suggested_transpose is None:
            self.status.set("No transpose suggestion yet.")
            return
        self.transpose.set(self.last_suggested_transpose)
        self.status.set(f"Applied transpose {self.last_suggested_transpose:+d}.")
        self.analyze_current_source(show_popup=False)
        self._preview_transpose_on_keyboard()

    def _tap_tempo(self) -> None:
        if not hasattr(self, "_tap_times"):
            self._tap_times: list[float] = []
        now = time.perf_counter()
        self._tap_times.append(now)
        self._tap_times = [t for t in self._tap_times if now - t < 4.0]
        if len(self._tap_times) >= 3:
            intervals = [self._tap_times[i] - self._tap_times[i - 1] for i in range(1, len(self._tap_times))]
            intervals.sort()
            median = intervals[len(intervals) // 2]
            if median > 0:
                bpm = round(60.0 / median)
                self.bpm.set(max(20, min(320, bpm)))
                self.status.set(f"Tap tempo: {self.bpm.get()} BPM ({len(self._tap_times)} taps)")
                return
        self.status.set(f"Tap tempo: {len(self._tap_times)} taps (need 3+)")

    def _preview_transpose_on_keyboard(self) -> None:
        try:
            settings = self.read_settings()
            actions, _ = self._actions_for_current_source(settings)
            if not actions:
                return
            transpose = int(self.transpose.get())
            notes: set[int] = set()
            for action in actions[:min(40, len(actions))]:
                for playable in action.notes:
                    if playable.kind == "midi":
                        fitted = self._fit_midi_note(int(playable.value) + transpose, settings)
                        if fitted is not None:
                            notes.add(fitted)
            if notes:
                self.highlight_midi_notes(notes)
                self.after(2000, self.clear_keyboard_highlights)
        except Exception:
            pass

    def tighten_loaded_midi_timing(self) -> None:
        if not self.loaded_midi_actions:
            self.status.set("Load or convert a MIDI first.")
            return
        try:
            bpm = max(1.0, float(self.bpm.get()))
            notes = scheduled_actions_to_audio_notes(self.loaded_midi_actions)
            if not notes:
                raise ValueError("No MIDI notes found to repair.")
            quantize_beats = max(0.0, float(self.timing_quantize_beats.get()))
            quantize_offset_seconds = 0.0
            offset_confidence = 0.0
            if bool(self.timing_auto_offset.get()) and quantize_beats > 0:
                quantize_offset_seconds, offset_confidence = estimate_note_grid_offset(notes, bpm, quantize_beats)
            repaired = repair_timing_notes(
                notes=notes,
                bpm=bpm,
                quantize_beats=quantize_beats,
                quantize_strength=max(0.0, min(1.0, float(self.timing_quantize_strength.get()))),
                start_offset_seconds=float(self.timing_offset_ms.get()) / 1000.0,
                min_note_beats=max(0.02, float(self.timing_min_note_beats.get())),
                max_note_beats=max(0.1, float(self.timing_max_note_beats.get())),
                gap_ms=max(0.0, float(self.timing_gap_ms.get())),
                quantize_offset_seconds=quantize_offset_seconds,
            )
            self.loaded_midi_actions = audio_notes_to_actions(repaired)
            self.source_mode.set("midi")
            if bool(self.timing_auto_offset.get()) and quantize_beats > 0:
                self.status.set(
                    f"Tightened MIDI timing: {len(repaired)} notes, "
                    f"auto offset {quantize_offset_seconds * 1000:.0f}ms ({offset_confidence:.2f})."
                )
            else:
                self.status.set(f"Tightened MIDI timing: {len(repaired)} notes.")
            self.analyze_current_source(show_popup=False)
        except Exception as exc:
            messagebox.showerror("Timing repair failed", str(exc))

    @staticmethod
    def _format_beats(beats: float) -> str:
        rounded = max(0.25, round(beats * 4) / 4)
        if math.isclose(rounded, round(rounded)):
            return str(int(round(rounded)))
        return f"{rounded:.2f}".rstrip("0").rstrip(".")

    def _fit_midi_note(self, midi_note: int, settings: PlaybackSettings) -> int | None:
        if settings.range_mode == "Auto-fit octaves":
            while midi_note < settings.low_midi:
                midi_note += 12
            while midi_note > settings.high_midi:
                midi_note -= 12
            if midi_note < settings.low_midi or midi_note > settings.high_midi:
                return None
            return midi_note
        if settings.range_mode == "Skip out-of-range":
            if midi_note < settings.low_midi or midi_note > settings.high_midi:
                return None
            return midi_note
        if midi_note < settings.low_midi or midi_note > settings.high_midi:
            raise ValueError(
                f"{midi_to_note_name(midi_note)} is outside "
                f"{midi_to_note_name(settings.low_midi)}-{midi_to_note_name(settings.high_midi)}."
            )
        return midi_note

    def convert_loaded_midi_to_text(self) -> None:
        if not self.loaded_midi_actions:
            self.status.set("Load a MIDI first.")
            return
        try:
            settings = self.read_settings()
            beat_seconds = 60.0 / settings.bpm
            down_groups: list[tuple[float, list[int]]] = []
            for action in self.loaded_midi_actions:
                if action.action != "down":
                    continue
                notes: list[int] = []
                for playable in action.notes:
                    if playable.kind != "midi":
                        continue
                    fitted = self._fit_midi_note(int(playable.value) + settings.transpose, settings)
                    if fitted is not None:
                        notes.append(fitted)
                if not notes:
                    continue
                if down_groups and abs(down_groups[-1][0] - action.seconds) <= 0.025:
                    down_groups[-1][1].extend(notes)
                else:
                    down_groups.append((action.seconds, notes))
            if not down_groups:
                raise ValueError("No note-on events could be converted.")

            tokens: list[str] = []
            for index, (seconds, notes) in enumerate(down_groups):
                next_seconds = down_groups[index + 1][0] if index + 1 < len(down_groups) else seconds + beat_seconds
                beats = max(0.25, (next_seconds - seconds) / beat_seconds)
                note_names = [midi_to_note_name(note) for note in sorted(set(notes))]
                if len(note_names) == 1:
                    token = note_names[0]
                else:
                    token = "[" + " ".join(note_names) + "]"
                token += f":{self._format_beats(beats)}"
                tokens.append(token)

            lines = [" ".join(tokens[i : i + 8]) for i in range(0, len(tokens), 8)]
            self.source_mode.set("text")
            self.score_text.delete("1.0", tk.END)
            self.score_text.insert("1.0", "\n".join(lines))
            self.song_title.set(Path(self.loaded_midi_name.get()).stem or self.song_title.get())
            self.song_notes.set("Converted from MIDI. Check timing, transpose, and range before saving.")
            self.status.set("Converted MIDI to editable text.")
            self.analyze_current_source(show_popup=False)
        except Exception as exc:
            messagebox.showerror("MIDI conversion failed", str(exc))

    def read_settings(self) -> PlaybackSettings:
        bpm = max(1.0, float(self.bpm.get()))
        speed = max(0.05, float(self.speed.get()))
        default_beats = max(0.01, float(self.default_beats.get()))
        hold_percent = min(1.0, max(0.05, float(self.hold_percent.get())))
        gap_ms = max(0.0, float(self.gap_ms.get()))
        start_delay = max(0.0, float(self.start_delay.get()))
        start_offset_seconds = max(0.0, float(self.playback_start_offset.get()))
        end_at_seconds = max(0.0, float(self.playback_end_at.get()))
        transpose = int(self.transpose.get())
        low_midi = note_name_to_midi("C2")
        high_midi = note_name_to_midi(self.high_note.get())
        return PlaybackSettings(
            bpm=bpm,
            speed=speed,
            default_beats=default_beats,
            hold_percent=hold_percent,
            gap_ms=gap_ms,
            start_delay=start_delay,
            start_offset_seconds=start_offset_seconds,
            end_at_seconds=end_at_seconds,
            transpose=transpose,
            low_midi=low_midi,
            high_midi=high_midi,
            range_mode=self.range_mode.get(),
        )

    def start_preview_playback(self) -> None:
        self._start_playback(preview_override=True)

    def start_playback(self) -> None:
        preview_only = bool(self.preview_only.get())
        if not preview_only and self.current_song_id:
            entry = self._find_song_by_id(self.current_song_id)
            if entry:
                entry["play_count"] = int(entry.get("play_count", 0)) + 1
                entry["last_played"] = now_stamp()
                self._update_recent_label()
        self._start_playback(preview_override=None)

    def _start_playback(self, preview_override: bool | None = None) -> None:
        if self.play_thread and self.play_thread.is_alive():
            self.status.set("Already playing. Use Pause or Stop.")
            return
        try:
            self.sender.set_method(self.input_method.get())
            settings = self.read_settings()
            preview_only = bool(self.preview_only.get()) if preview_override is None else bool(preview_override)
            actions, source_name = self._actions_for_current_source(settings)
            actions = coalesce_scheduled_actions(actions)
            if not actions:
                raise ValueError("Nothing to play.")
        except Exception as exc:
            messagebox.showerror("Could not start", str(exc))
            return

        self.stop_event.clear()
        self.pause_event.clear()
        self.progress.set(0.0)
        self.play_thread = threading.Thread(
            target=self._play_worker,
            args=(actions, settings, source_name, preview_only),
            daemon=True,
        )
        self.play_thread.start()

    def toggle_pause(self) -> None:
        if not self.play_thread or not self.play_thread.is_alive():
            self.status.set("Nothing is playing.")
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.status.set("Resumed.")
        else:
            self.pause_event.set()
            self.sender.release_all()
            self.status.set("Paused. Press pause hotkey again to resume.")

    def stop_playback(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.sender.release_all()
        if self.synth:
            self.synth.all_notes_off()
        self.clear_keyboard_highlights()
        self.status.set("Stopped.")
        self.progress.set(0.0)

    def _test_note_worker(self, midi_note: int, note_name: str, hold_seconds: float, start_delay: float) -> None:
        binding = KEY_MAP[midi_note]
        timer_enabled = begin_high_resolution_timer()
        try:
            self._thread_status(
                f"Testing {note_name} with {self.sender.method} key {binding.label!r} in "
                f"{start_delay:g}s. Focus Roblox now."
            )
            if not wait_until_precise(time.perf_counter() + start_delay, self.stop_event):
                return

            self.sender.key_down(binding)
            self._synth_note_on(midi_note)
            self.highlight_midi_notes({midi_note})
            self._thread_progress(50.0)
            if not wait_until_precise(time.perf_counter() + hold_seconds, self.stop_event):
                return
            self.sender.key_up(binding)
            self._synth_note_off(midi_note)
            self.clear_keyboard_highlights()
            self._thread_progress(100.0)
            self._thread_status(f"Tested {note_name}. Phone tuner should show about {note_name}.")
        except Exception as exc:
            self._thread_status(f"Error: {exc}")
            self.after(0, lambda error=exc: messagebox.showerror("Note test error", str(error)))
        finally:
            self.sender.release_all()
            end_high_resolution_timer(timer_enabled)

    def _resolve_playable(self, playable: Playable, settings: PlaybackSettings) -> KeyBinding | None:
        if playable.kind == "key":
            return key_label_to_binding(str(playable.value))

        midi_note = self._fit_midi_note(int(playable.value) + settings.transpose, settings)
        if midi_note is None:
            return None
        binding = KEY_MAP.get(midi_note)
        if binding is None:
            raise ValueError(f"No keyboard mapping for {midi_to_note_name(midi_note)}.")
        return binding

    def _resolved_midi_note_for_preview(self, playable: Playable, settings: PlaybackSettings) -> int | None:
        if playable.kind != "midi":
            return None
        return self._fit_midi_note(int(playable.value) + settings.transpose, settings)

    def _play_worker(
        self,
        actions: list[ScheduledAction],
        settings: PlaybackSettings,
        source_name: str,
        preview_only: bool,
    ) -> None:
        timer_enabled = begin_high_resolution_timer()
        loop_enabled = bool(self.loop_enabled.get())
        loop_start = max(0.0, float(self.loop_start_sec.get()))
        loop_end = max(0.0, float(self.loop_end_sec.get()))
        if loop_enabled and loop_end > 0 and loop_end <= loop_start:
            loop_end = 0.0
        loop_count = 0
        practice = bool(self.practice_mode.get()) and loop_enabled
        if practice:
            current_speed = max(0.10, float(self.practice_start_speed.get()))
            target_speed = max(current_speed, float(self.practice_target_speed.get()))
            increment = max(0.01, float(self.practice_increment.get()))
            loops_per_step = max(1, int(self.practice_loops_per_step.get()))
            self.practice_current_speed = current_speed
            self.practice_loop_count = 0
            settings.speed = current_speed
            self._thread_status(f"Practice mode: {current_speed:.0%} speed (target: {target_speed:.0%})")
        try:
            total = max(action.seconds for action in actions) if actions else 0.0
            loop_total = min(total, loop_end) - loop_start if loop_enabled and loop_end > 0 else total
            if preview_only:
                self._thread_status(f"Previewing {source_name}. No keys are being sent.")
            else:
                self._thread_status(
                    f"Starting {source_name} with {self.sender.method} in {settings.start_delay:g}s. "
                    "Focus Roblox now."
                )
                if not wait_until_precise(time.perf_counter() + settings.start_delay, self.stop_event):
                    return

            while True:
                start_time = time.perf_counter()
                if loop_enabled and loop_start > 0 and loop_count == 0:
                    action_start_idx = 0
                    for i, a in enumerate(actions):
                        if a.seconds >= loop_start:
                            action_start_idx = i
                            break
                    start_time -= loop_start / settings.speed
                else:
                    action_start_idx = 0

                index = action_start_idx
                while index < len(actions):
                    if self.stop_event.is_set():
                        return
                    if self.pause_event.is_set():
                        pause_started = time.perf_counter()
                        while self.pause_event.is_set():
                            if self.stop_event.is_set():
                                return
                            time.sleep(0.02)
                        start_time += time.perf_counter() - pause_started
                        continue

                    action = actions[index]
                    if loop_enabled and loop_end > 0 and action.seconds > loop_end:
                        break
                    if loop_enabled and loop_start > 0 and loop_count == 0 and action.seconds < loop_start:
                        index += 1
                        continue

                    target = action.seconds / settings.speed
                    target_deadline = start_time + target
                    now = time.perf_counter()
                    if now < target_deadline:
                        if not wait_until_precise(target_deadline, self.stop_event, self.pause_event):
                            continue
                        continue

                    bindings: list[KeyBinding] = []
                    preview_notes: set[int] = set()
                    for playable in action.notes:
                        binding = self._resolve_playable(playable, settings)
                        if binding is not None:
                            bindings.append(binding)
                        preview_note = self._resolved_midi_note_for_preview(playable, settings)
                        if preview_note is not None:
                            preview_notes.add(preview_note)

                    unique_bindings = list({binding.label: binding for binding in bindings}.values())
                    synth_notes: set[int] = set()
                    synth_vel_map: dict[int, int] = {}
                    for playable in action.notes:
                        if playable.kind == "midi":
                            fitted = self._fit_midi_note(int(playable.value) + settings.transpose, settings)
                            if fitted is not None:
                                synth_notes.add(fitted)
                                synth_vel_map[fitted] = 100
                    if action.action == "down":
                        if not preview_only:
                            for binding in sorted(unique_bindings, key=lambda item: item.shifted):
                                self.sender.key_down(binding)
                        for sn, vel in synth_vel_map.items():
                            self._synth_note_on(sn, vel)
                            self.preview_note_velocities[sn] = vel
                        if preview_notes:
                            self.highlight_midi_notes(preview_notes, add=True)
                        if bool(self.metronome_enabled.get()):
                            beat_pos = action.seconds * settings.bpm / 60.0
                            if abs(beat_pos - round(beat_pos)) < 0.06:
                                self.after(0, self._metronome_tick)
                    else:
                        if not preview_only:
                            for binding in unique_bindings:
                                self.sender.key_up(binding)
                        for sn in synth_notes:
                            self._synth_note_off(sn)
                            self.preview_note_velocities.pop(sn, None)
                        if preview_notes:
                            self.unhighlight_midi_notes(preview_notes)

                    if loop_total > 0:
                        effective_sec = action.seconds - (loop_start if loop_enabled else 0)
                        self._thread_progress(min(100.0, 100.0 * effective_sec / max(0.01, loop_total)))
                    elif total > 0:
                        self._thread_progress(min(100.0, 100.0 * action.seconds / total))
                    index += 1

                loop_count += 1
                if practice:
                    steps_done = loop_count // loops_per_step
                    new_speed = min(target_speed, current_speed + steps_done * increment)
                    if new_speed != settings.speed:
                        settings.speed = new_speed
                        self.practice_current_speed = new_speed
                        self._thread_status(
                            f"Practice: {new_speed:.0%} speed "
                            f"(step {steps_done + 1}, loop {loop_count % loops_per_step + 1}/{loops_per_step})"
                        )
                    if new_speed >= target_speed and loop_count >= loops_per_step:
                        self._thread_status(f"Practice complete! Reached {target_speed:.0%} speed.")
                        break
                if not loop_enabled and not practice:
                    break
                if not loop_enabled:
                    break
                if loop_count > 9999:
                    break
                self._thread_status(f"Loop {loop_count}...")

            self._thread_progress(100.0)
            status_text = "Finished."
            if loop_count > 1:
                status_text = f"Finished ({loop_count} loops)."
            self._thread_status(status_text)
        except Exception as exc:
            self._thread_status(f"Error: {exc}")
            self.after(0, lambda error=exc: messagebox.showerror("Playback error", str(error)))
        finally:
            self.sender.release_all()
            self.clear_keyboard_highlights()
            end_high_resolution_timer(timer_enabled)

    def _thread_status(self, text: str) -> None:
        self.after(0, lambda: self.status.set(text))

    def _thread_progress(self, value: float) -> None:
        self.after(0, lambda: self.progress.set(value))

    def _on_close(self) -> None:
        if self._is_dirty:
            answer = messagebox.askyesnocancel(
                "Unsaved changes",
                f"Save changes to '{self.song_title.get()}' before closing?"
            )
            if answer is None:
                return
            if answer:
                self.save_current_song()
        self._save_settings()
        try:
            self._save_song_library()
        except Exception:
            pass
        self.stop_event.set()
        self.sender.release_all()
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
        if self.synth:
            try:
                self.synth.close()
            except Exception:
                pass
        self.destroy()


def main() -> None:
    app = PianoMacroApp()
    app.mainloop()


if __name__ == "__main__":
    main()
