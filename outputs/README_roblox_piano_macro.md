# JJS Piano Studio

JJS Piano Studio is a Windows-focused GUI for playing the Visual Pianos style
keyboard layout shown in your screenshot. It includes a song library, MIDI
import, MIDI-to-text conversion, range analysis, transpose suggestions, hotkeys,
calibration tests, and a live preview keyboard.

## Install

Open PowerShell in this folder and run:

```powershell
python -m pip install -r requirements.txt
```

Then start the app:

```powershell
python jjs_piano_studio.py
```

After setup, you only need the start command.

Use Python 3.11 if the audio-to-MIDI packages fail on newer Python versions.

## Quick Play

1. Open Roblox/JJS and sit at the piano.
2. Start `jjs_piano_studio.py`.
3. Keep `Input method` on `Windows SendInput scan`.
4. Click inside the Roblox game window/piano area.
5. Press `F6` to play after the start delay.
6. Use `F7` to pause/resume and `F8` to stop.

Hotkeys can be changed in the `Hotkeys` section: click a hotkey button, then
press the key you want to use. The app saves your controls, timing, range,
transpose, and input method when it closes.

## Studio Workflow

- Use the left `Song Library` to create, save, duplicate, import, export, and
  delete songs.
- Fill in title, artist, tags, and notes above the editor.
- Paste text notation into the editor, or click `Load MIDI`.
- Click `Online MIDI Search` to search public Online Sequencer songs by title,
  URL, or sequence ID, then download and load a generated MIDI directly into the
  studio.
- Use `Load MIDI URL` inside Online MIDI Search for direct `.mid` links from
  other MIDI sites.
- Click `Audio to MIDI` to convert a local `.wav`, `.mp3`, `.flac`, `.ogg`,
  `.m4a`, or `.aac` file into playable notes.
- Click `Analyze` to check length, note count, range, max held notes, and
  suggested transpose.
- Click `Apply Suggested Transpose` if the analyzer finds a better fit.
- Click `Preview` or use `Preview only` to watch the keyboard animation without
  sending keys.
- Use `Timeline` to preview, trim, delete bad sections, or set a playback
  window.
- Use `Auto Cleanup MIDI` to simplify dense MIDI files for Roblox playback.
- Click `MIDI to Text` after loading a MIDI to convert it into editable notation
  that can be saved in the library.

## Online MIDI Search

The `Online MIDI Search` tool searches Online Sequencer's public sequence list
first, then falls back to regular web search if needed. You can type a song
name, artist, full Online Sequencer URL, or just a sequence ID.
Search tries a few cleaned query variants automatically, so titles like
`artist - song midi piano` can still find the underlying sequence. Results can
be sorted by best playable match, exact match, plays, note count, newest update,
title, or author; click the table headings for quick sorting. `Best playable`
is the default because it favors relevant piano-style results with reasonable
note counts over huge arrangements that are likely to be messy in Roblox.

Online Sequencer currently exports MIDI in browser-side JavaScript instead of a
simple static download link. The studio handles this by downloading the public
sequence page, decoding its embedded sequence data, and generating a `.mid` file
locally. Downloaded files are saved in `online_midis` beside the app.
The importer matches Online Sequencer's timing grid, and `Download & Load`
prepares the MIDI in the background so larger songs do not freeze the window
while they are parsed.
`Auto-skip note-art intro` is enabled by default. It detects extremely dense
intro note art and starts playback after that section instead of smashing the
Roblox piano with thousands of decorative notes.
`Auto-clean for Roblox` can simplify downloaded files by reducing impossible
polyphony, octave-fitting notes into range, and removing Visual Pianos key
conflicts. Turn it off if you want the raw MIDI.
Use `Load MIDI URL` for direct `.mid`/`.midi` links from other sites such as
VGMusic or MIDIWorld.

Recommended workflow:

1. Click `Online MIDI Search`.
2. Search for a song, such as `unravel tokyo ghoul`.
3. Select a result.
4. Click `Download & Load`.
5. Use `Analyze`, `Apply Suggested Transpose`, and optionally `Tighten Loaded MIDI`.

Double-clicking a result also downloads and loads it. Use `Download Only` if you
want to save the generated `.mid` without replacing the currently loaded song.

## Preview And Timeline

`Preview` plays the current song on the built-in keyboard only. It does not send
keys to Roblox.

`Start at sec` and `End at sec` in the Playback section let you skip or stop at
specific times without permanently editing the MIDI. Set `End at sec` to `0` to
play through the end.

`Timeline` opens a visual note overview. Left-click the timeline to set the
start time; right-click to set the end time. From there you can preview the
window, apply the trim to the loaded MIDI, delete a bad range, or run automatic
Roblox cleanup.

## Audio To MIDI

The `Audio to MIDI` tool converts local audio files into notes. The default path
is now `Best JJS arrangement`: Basic Pitch AI first, then a Roblox-friendly
arranger that prioritizes the recognizable melody, limits chord clutter, keeps
bass mostly on strong beats, and softens timing instead of hard-snapping every
note. The default AI presets also run a slower rescue pass so quiet melody notes
missed by the clean pass have another chance to be detected.
The arranger now chooses the lead line with a smooth melody-contour pass across
neighboring note buckets, so isolated loud background notes are less likely to
steal the melody.
It also suppresses weak harmonic ghost notes, such as octave or overtone echoes
that line up with a stronger fundamental, while preserving upper notes that look
like part of the actual melody.
Repeated same-pitch melody notes are preserved as separate retriggers instead of
being swallowed into one long held note.
For the main presets, `Smart arranger` can audition several Roblox-friendly
arrangement shapes and keep the one with the best balance of melody continuity,
note density, chord clarity, and keyboard playability.

Conversion runs in a separate worker process, so the studio should stay
responsive while audio is being analyzed. If you close the Audio to MIDI window
mid-conversion, the worker is stopped.

Recommended first try:

1. Click `Audio to MIDI`.
2. Browse for your audio file.
3. Start with `Preset: Best JJS arrangement`.
4. Leave `Auto BPM` checked unless you already know the exact tempo.
5. Click `Convert`.
6. Click `Load as MIDI` to preview/play it, or `Send Text` to edit and save it.
7. Use `Save MIDI` if you want a standalone `.mid` file.

Presets:

- `Best JJS arrangement`: best first pass for most songs and Roblox playback.
- `Lead melody only`: strongest choice when the song sounds unrecognizable.
- `Rich piano arrangement`: fuller output for clean piano/acoustic sources.
- `Dense AI transcription`: keeps much more raw detail, but can sound messy.
- `AI clean piano`: stricter timing and cleaner chord choices for piano-like audio.
- `Fallback melody`: old lightweight one-note pitch tracker.
- `Fallback chords`: old experimental spectral chord detector.

Mode notes:

- `Basic Pitch AI` is the main engine and should produce far more notes than the
  old converter. If Basic Pitch is missing or fails, the default conversion path
  now tries a hybrid fallback and shows a warning in the result box.
- `Melody / lead line` is only a fallback for one clear note at a time.
- `Spectral chords` is only a fallback for simple piano/chord audio.
- `Arrangement: Balanced JJS` is the recommended default.
- `Arrangement: Lead melody` is best when mixed audio creates wrong background notes.
- `Arrangement: Rich piano` is best for clean piano covers or isolated instrument audio.
- `Arrangement: Dense transcription` is for experimentation, not the first pass.
- `Arrangement: Raw transcription` bypasses the arranger and is usually less playable.
- Higher `Sensitivity` catches more notes and also more mistakes.
- Lower `Min note beats` catches quick notes but can create clutter.
- `Snap strength` controls how strongly notes are pulled toward the beat grid.
- `Timing nudge ms` shifts detected notes before arrangement. Negative values
  play earlier and are useful when converted notes feel consistently late.
- `Auto BPM` estimates tempo during conversion, then can refine half-time or
  double-time guesses from the detected note starts before arranging.
- `Auto timing phase` estimates where the beat grid actually starts from the
  detected note onsets. Leave it on if a song feels consistently early, late, or
  off-grid after conversion.
- `Local beat grid` uses beat tracking to snap notes to the song's own changing
  pulse instead of one rigid global BPM grid. Leave it on for full songs; turn it
  off if the beat tracker locks onto drums or syncopation incorrectly.
- `Smart arranger` tries a small set of arrangement variants and chooses the one
  that scores best for JJS playback. The result box shows which candidate won.
- `Multi-pass AI rescue` runs a second melody-sensitive Basic Pitch pass and
  blends both detections before arrangement. It is slower but usually better for
  full songs.
- `Key cleanup` estimates the likely song key and nudges weak off-key artifacts
  into nearby in-key notes. Strong accidentals are preserved.
- Harmonic ghost cleanup runs automatically for the main arranged presets. Use
  `Dense AI transcription` or `Raw transcription` if you want to inspect more of
  the raw detected notes.
- `Melody priority` raises or lowers how aggressively the arranger protects the
  top-line melody.
- `Max notes at once` matters for Roblox: smaller values often sound clearer.
  The arranger also removes impossible Visual Pianos key conflicts, such as a
  white key and its shifted black-key partner on the same physical keyboard key.
  Balanced and Rich arrangements also use adaptive texture, so fast melody runs
  automatically get fewer harmony notes than slower held sections.
- `Prefer harmonic content` now preprocesses Basic Pitch audio too; keep it on
  for full songs with drums, but turn it off if it removes too much of a vocal
  melody or lead synth.
- `Basic Pitch melody cleanup` can reduce extra harmony notes in melody-heavy
  songs. Turn it off for clean piano/chord-heavy songs.
- If output is too messy, try `Lead melody only`, lower sensitivity, lower max
  notes at once, or increase `Min note beats`.
- If output misses too much, raise sensitivity, lower `Min note beats`, or try
  `Dense AI transcription`.

## Fixing Bad Timing

Audio transcription often finds the right notes but places them slightly early,
late, or off-grid. The Audio to MIDI arranger already does a timing pass before
`Load as MIDI`. Use `Timing Repair` after previewing if the loaded MIDI still
feels early, late, too loose, or too robotic.

The conversion result shows `Timing phase` in milliseconds. A confident nonzero
phase means the arranger detected that the song's grid starts slightly after the
trimmed audio begins, and it used that phase while snapping notes.
If the result shows `Local beat grid`, the arranger used beat-tracked local
subdivisions instead of the global phase. This is usually better for real songs
whose tempo drifts or whose detected BPM is only approximately right.
It also shows `Timing nudge`; if the whole song still feels late, try a more
negative value such as `-40ms` or `-60ms`, then convert again. If it feels early,
move the nudge toward zero or positive values.

Recommended first pass:

- `Grid beats`: `0.25`
- `Grid strength`: `0.55`
- `Offset ms`: `0`
- `Auto timing offset`: on
- `Min note beats`: `0.10`
- `Max note beats`: `4`
- `Gap ms`: `12`

Workflow:

1. Convert audio with `Audio to MIDI`.
2. Click `Load as MIDI`.
3. Turn on `Preview only`.
4. Press `F6` and watch/listen to the preview.
5. Leave `Auto timing offset` on for the first repair pass.
6. If notes feel late, set `Offset ms` to a negative value like `-40`.
7. If notes feel early, set `Offset ms` to a positive value like `40`.
8. If rhythm is messy, raise `Grid strength` toward `0.8`.
9. If rhythm feels too robotic, lower `Grid strength` toward `0.25`.
10. Click `Tighten Loaded MIDI` after changing timing values.

For fast songs, try `Grid beats: 0.125`. For slower/simple songs, `0.25` is
usually cleaner.

If `Basic Pitch AI` says it is not installed, run:

```powershell
python -m pip install basic-pitch setuptools
```

Basic Pitch officially supports Python up to 3.11. If installation fails on a
newer Python, install Python 3.11 and run the studio with that interpreter.
The hybrid fallback can still produce a playable draft without Basic Pitch, but
Basic Pitch is strongly recommended for songs with multiple instruments.

Saved songs are stored beside the app in `piano_studio_library.json`. Exported
songs use `.jjspiano.json`.

## Text Score Format

```text
C4 D4 E4 F4 G4 A4 B4 C5
C4:0.5 D4:0.5 E4:1
[C4 E4 G4]:2 R:0.5 [D4 F4 A4]:2
```

- Notes use scientific pitch names: `C4`, `D#4`, `Bb3`.
- Durations are in beats: `C4:0.5`, `G4:2`.
- Rests are `R`, `rest`, `-`, or `_`.
- Chords use brackets: `[C4 E4 G4]:2`.
- Raw keyboard labels can be used with `key:q`, `key:Q`, `key:!`.

## Tuner Check

The screenshot has 36 white keys. If the first white key is C2, the last white
key should be C7.

Use `Calibration`:

1. Click `Test C6`, then focus Roblox during the countdown. The tuner should
   read about C6. This uses the `l` key.
2. Click `Test C7`, then focus Roblox during the countdown. The tuner should
   read about C7. This uses the `m` key.
3. Keep `Highest key` on `C7` if C7 works. Use `C6` only if the game really
   stops there.

## If Roblox Does Not Play

- Use `F6` while Roblox is already focused instead of clicking `Play` and then
  tabbing over.
- Focus the actual Roblox Player/game window, not only the website tab.
- Press `Esc` in Roblox so chat/text boxes are closed.
- Click directly inside the game canvas/piano area.
- Increase `Start delay` to 5 seconds, press `F6`, then click Roblox.
- Keep `Input method` on `Windows SendInput scan` since that worked on your
  setup. If needed, try `Windows SendInput vk`, `Windows keybd_event`, then
  `PyAutoGUI`.
- On some keyboards, function keys require `Fn+F6`, `Fn+F7`, or `Fn+F8`.
- Run Python and Roblox at the same permission level.

## For Songs Like Unravel

I cannot include a full copyrighted transcription in the app. The best route is:

1. Download or create a `.mid` file you own or have permission to use.
2. Click `Load MIDI`.
3. Click `Analyze`.
4. Apply the suggested transpose if needed.
5. Use `Preview only` to check the rhythm visually.
6. Click `MIDI to Text` if you want to save/edit the result in the library.
7. Focus Roblox and press `F6`.

## Notes

- The app targets the common 61-key C2-C7 Visual Pianos layout.
- Some computer-keyboard chords are physically awkward because shifted black
  keys share keys with white notes. The studio deduplicates the output as well
  as the keyboard input model allows.
- This is intended only for games/servers where piano macro playback is allowed.
