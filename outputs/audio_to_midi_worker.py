from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

# Ensure the package parent is on sys.path for subprocess imports
_package_root = Path(__file__).resolve().parent
if str(_package_root) not in sys.path:
    sys.path.insert(0, str(_package_root))

from roblox_piano_macro import (
    arrange_audio_notes_for_jjs,
    audio_key_label,
    audio_notes_to_dicts,
    blend_audio_note_passes,
    convert_audio_basic_pitch_to_notes,
    convert_audio_melody_to_notes,
    convert_audio_spectral_to_notes,
    estimate_audio_key,
    estimate_local_beat_grid,
    estimate_note_grid_offset,
    estimate_audio_bpm,
    refine_bpm_from_note_starts,
    smart_arrange_audio_notes_for_jjs,
)


def run_job(request_path: Path, response_path: Path) -> None:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    job_type = request["job_type"]
    started = time.monotonic()

    if job_type == "estimate_bpm":
        tempo = estimate_audio_bpm(
            path=request["path"],
            sample_rate=int(request["sample_rate"]),
            trim_silence=bool(request["trim_silence"]),
        )
        response = {
            "ok": True,
            "job_type": job_type,
            "tempo": tempo,
            "elapsed": time.monotonic() - started,
        }
    elif job_type == "convert":
        warnings: list[str] = []
        source_counts: dict[str, int] = {}
        bpm = float(request["bpm"])
        estimated_bpm: float | None = None
        timing_offset_seconds = 0.0
        timing_offset_confidence = 0.0
        timing_grid_seconds: list[float] = []
        timing_grid_count = 0
        timing_grid_confidence = 0.0
        timing_grid_tempo = 0.0
        selected_arrangement = ""
        arrangement_quality_score = 0.0
        arrangement_candidates = 0
        arrangement_style = str(request.get("arrangement_style", "Balanced JJS"))
        arrangement_will_run = arrangement_style != "Raw transcription"
        if bool(request.get("auto_bpm", False)):
            try:
                estimated_bpm = estimate_audio_bpm(
                    path=request["path"],
                    sample_rate=int(request["sample_rate"]),
                    trim_silence=bool(request["trim_silence"]),
                )
                bpm = estimated_bpm
            except Exception as exc:
                warnings.append(f"Automatic BPM estimate failed; used manual BPM: {exc}")
        common = {
            "path": request["path"],
            "bpm": bpm,
            "low_midi": int(request["low_midi"]),
            "high_midi": int(request["high_midi"]),
            "min_note_beats": float(request["min_note_beats"]),
            "grid_beats": 0.0 if arrangement_will_run else float(request["grid_beats"]),
            "sample_rate": int(request["sample_rate"]),
            "trim_silence": bool(request["trim_silence"]),
            "harmonic_only": bool(request["harmonic_only"]),
        }
        if request["mode"] == "basic_pitch":
            try:
                notes = convert_audio_basic_pitch_to_notes(
                    sensitivity=float(request["sensitivity"]),
                    melodia_trick=bool(request["melodia_trick"]),
                    postprocess=False,
                    **common,
                )
                source_counts["ai_main"] = len(notes)
                if bool(request.get("multipass_ai", True)):
                    try:
                        rescue_common = dict(common)
                        rescue_common["harmonic_only"] = False
                        rescue_common["grid_beats"] = 0.0
                        rescue_common["min_note_beats"] = max(0.03, float(common["min_note_beats"]) * 0.70)
                        rescue_notes = convert_audio_basic_pitch_to_notes(
                            sensitivity=min(0.95, float(request["sensitivity"]) + 0.12),
                            melodia_trick=True,
                            postprocess=False,
                            **rescue_common,
                        )
                        source_counts["ai_rescue"] = len(rescue_notes)
                        notes = blend_audio_note_passes(
                            [notes, rescue_notes],
                            bpm=bpm,
                            low_midi=int(request["low_midi"]),
                            high_midi=int(request["high_midi"]),
                        )
                    except Exception as exc:
                        warnings.append(f"AI rescue pass failed; used main pass only: {exc}")
            except Exception as exc:
                warnings.append(f"Basic Pitch failed; used hybrid fallback: {exc}")
                fallback_melody = convert_audio_melody_to_notes(
                    confidence_threshold=max(0.08, min(0.75, 1.0 - float(request["sensitivity"]))),
                    **common,
                )
                fallback_spectral = convert_audio_spectral_to_notes(
                    sensitivity=float(request["sensitivity"]),
                    max_polyphony=int(request["max_polyphony"]),
                    **common,
                )
                source_counts["fallback_melody"] = len(fallback_melody)
                source_counts["fallback_spectral"] = len(fallback_spectral)
                notes = blend_audio_note_passes(
                    [fallback_melody, fallback_spectral],
                    bpm=bpm,
                    low_midi=int(request["low_midi"]),
                    high_midi=int(request["high_midi"]),
                )
        elif request["mode"] == "melody":
            notes = convert_audio_melody_to_notes(
                confidence_threshold=float(request["confidence_threshold"]),
                **common,
            )
            source_counts["melody"] = len(notes)
        else:
            notes = convert_audio_spectral_to_notes(
                sensitivity=float(request["sensitivity"]),
                max_polyphony=int(request["max_polyphony"]),
                **common,
            )
            source_counts["spectral"] = len(notes)
        if bool(request.get("auto_bpm", False)) and notes:
            refined_bpm = refine_bpm_from_note_starts(notes, bpm)
            if abs(refined_bpm - bpm) >= max(1.5, bpm * 0.015):
                warnings.append(f"Auto BPM refined from {bpm:.1f} to {refined_bpm:.1f} using detected notes.")
                bpm = refined_bpm
        raw_note_count = len(notes)
        detected_key = audio_key_label(estimate_audio_key(notes))
        if arrangement_will_run and bool(request.get("arrangement_auto_timing", True)):
            arrangement_grid_beats = float(request.get("arrangement_grid_beats", request["grid_beats"]))
            if bool(request.get("arrangement_local_timing", True)):
                try:
                    timing_grid_seconds, timing_grid_tempo, timing_grid_confidence = estimate_local_beat_grid(
                        path=request["path"],
                        sample_rate=int(request["sample_rate"]),
                        trim_silence=bool(request["trim_silence"]),
                        grid_beats=arrangement_grid_beats,
                    )
                    timing_grid_count = len(timing_grid_seconds)
                    if timing_grid_count < 8 or timing_grid_confidence < 0.20:
                        timing_grid_seconds = []
                        timing_grid_count = 0
                    elif bool(request.get("auto_bpm", False)) and timing_grid_tempo:
                        if abs(timing_grid_tempo - bpm) >= max(2.0, bpm * 0.025):
                            warnings.append(
                                f"Local beat grid tempo is {timing_grid_tempo:.1f}; kept BPM {bpm:.1f} for note lengths."
                            )
                except Exception as exc:
                    warnings.append(f"Local timing grid failed; used global timing phase: {exc}")
                    timing_grid_seconds = []
                    timing_grid_count = 0
                    timing_grid_confidence = 0.0
                    timing_grid_tempo = 0.0
            if not timing_grid_seconds:
                timing_offset_seconds, timing_offset_confidence = estimate_note_grid_offset(
                    notes,
                    bpm=bpm,
                    grid_beats=arrangement_grid_beats,
                )
        if arrangement_style != "Raw transcription":
            arrangement_kwargs = {
                "notes": notes,
                "bpm": bpm,
                "low_midi": int(request["low_midi"]),
                "high_midi": int(request["high_midi"]),
                "style": arrangement_style,
                "max_polyphony": int(request.get("arrangement_max_polyphony", request["max_polyphony"])),
                "grid_beats": float(request.get("arrangement_grid_beats", request["grid_beats"])),
                "quantize_strength": float(request.get("arrangement_quantize_strength", 0.65)),
                "min_note_beats": float(request.get("arrangement_min_note_beats", request["min_note_beats"])),
                "max_note_beats": float(request.get("arrangement_max_note_beats", 4.0)),
                "gap_ms": float(request.get("arrangement_gap_ms", 18.0)),
                "keep_bass": bool(request.get("arrangement_keep_bass", True)),
                "melody_boost": float(request.get("arrangement_melody_boost", 0.95)),
                "key_cleanup": bool(request.get("arrangement_key_cleanup", False)),
                "quantize_offset_seconds": timing_offset_seconds,
                "timing_nudge_seconds": float(request.get("arrangement_timing_nudge_ms", 0.0)) / 1000.0,
                "timing_grid_seconds": timing_grid_seconds,
            }
            if bool(request.get("arrangement_smart_select", False)):
                notes, selected_arrangement, arrangement_quality_score, arrangement_candidates = smart_arrange_audio_notes_for_jjs(
                    **arrangement_kwargs
                )
            else:
                notes = arrange_audio_notes_for_jjs(**arrangement_kwargs)
                selected_arrangement = arrangement_style
                arrangement_candidates = 1
        response = {
            "ok": True,
            "job_type": job_type,
            "notes": audio_notes_to_dicts(notes),
            "raw_note_count": raw_note_count,
            "source_counts": source_counts,
            "warnings": warnings,
            "bpm_used": bpm,
            "estimated_bpm": estimated_bpm,
            "detected_key": detected_key,
            "timing_offset_seconds": timing_offset_seconds,
            "timing_offset_confidence": timing_offset_confidence,
            "timing_grid_count": timing_grid_count,
            "timing_grid_confidence": timing_grid_confidence,
            "timing_grid_tempo": timing_grid_tempo,
            "timing_nudge_ms": float(request.get("arrangement_timing_nudge_ms", 0.0)),
            "selected_arrangement": selected_arrangement,
            "arrangement_quality_score": arrangement_quality_score,
            "arrangement_candidates": arrangement_candidates,
            "elapsed": time.monotonic() - started,
        }
    else:
        raise ValueError(f"Unknown audio worker job: {job_type!r}")

    response_path.write_text(json.dumps(response, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: audio_to_midi_worker.py <request.json> <response.json>", file=sys.stderr)
        return 2

    request_path = Path(sys.argv[1])
    response_path = Path(sys.argv[2])
    try:
        run_job(request_path, response_path)
        return 0
    except Exception as exc:
        response = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        response_path.write_text(json.dumps(response, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
