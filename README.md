# JJS Piano Studio

Windows GUI for playing Visual Pianos-style Roblox/JJS piano layouts with
hotkeys, MIDI import, online MIDI search, live preview, and audio-to-MIDI
conversion.

## Quick Start

Open PowerShell in the `outputs/` directory and run:

```powershell
python -m pip install -r requirements.txt
python jjs_piano_studio.py
```

## Main Files

```
outputs/
  jjs_piano_studio/         # Core package
    __init__.py, __main__.py
    constants.py             # Colours, paths, key maps, note utilities
    models.py                # Data classes (Playable, ScoreEvent, etc.)
    sender.py                # Windows key-sending engine
  roblox_piano_macro.py     # Main GUI and playback engine
  audio_to_midi_worker.py   # Background audio conversion worker
  jjs_piano_studio.py        # Entry point launcher
  requirements.txt           # Python dependencies
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+S` | Save song |
| `Ctrl+N` | New song |
| `Ctrl+O` | Load MIDI |
| `Ctrl+F` | Focus library search |
| `Ctrl+Shift+F` | Toggle favorite |
| `F6` / `F7` / `F8` | Play / Pause / Stop (customizable) |

## Features

- **Song Library** with search, favorites, sort options, and play count tracking
- **Online MIDI Search** with multi-source lookup and smart relevance scoring
- **Audio-to-MIDI** using Spotify's Basic Pitch AI
- **Text Score Editor** with notation parsing (notes, chords, rests, raw keys)
- **MIDI Import/Export** with timing repair and Roblox-friendly cleanup
- **Preview Keyboard** with real-time note highlighting
- **Global Hotkeys** for hands-free playback control
