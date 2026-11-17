# Milestone 1 status

Execution environment for every check below: cloud Linux (Ubuntu 24.04.5, x86_64, 4 CPU, 15 GB RAM, no GPU),
Python 3.13.18, FFmpeg 6.1.1. **Nothing was run on Windows 11.** Full suite:
`.venv/bin/python -m unittest discover -s tests` → 53 tests OK (also OK with system Python without speech packages: 2 skipped — browser and real-VAD recovery).

| Requirement | Implemented / Partial / Missing | Verified / Partially verified / Unverified | Evidence | Notes |
| --- | --- | --- | --- | --- |
| 1. Local, free runtime, no Claude/paid APIs | Implemented | Verified (Linux) | `pip install -r requirements.txt -e .` in clean venv + `gta6clips analyze` | Core needs only numpy + FFmpeg |
| 1. CPU int8 default, GPU only after real init, CPU fallback | Implemented | Partially verified | `tests/test_speech_subs.py` (stand-in model: no CUDA → CPU, CUDA init error → CPU) | No GPU here; real CUDA path Unverified |
| 1. Model cache/download docs, size estimate | Implemented | Unverified | `gta6clips/speech.py` `ESTIMATED_MODEL_SIZE`, README | huggingface.co blocked by network policy; ≈480 MB is an estimate |
| 2A. Single file and folder import, Cyrillic/space paths | Implemented | Verified | `test_app.py::test_import_dedupe_and_cyrillic_paths`, `test_ui_smoke.py` | Path typed into UI (browser can't expose full paths) |
| 2A. Works without Twitch, transcription can be disabled, missing model non-fatal | Implemented | Verified | `test_missing_model_is_reported_not_fatal`; manual run with blocked download → "распознавание недоступно", analysis done | |
| 2A. Recovery after unavailable speech/VAD (retry, cache reuse, decisions kept) | Implemented | Verified (stand-in model) | `SpeechRecoveryTests`, `RetryButtonTests` in `tests/test_speech_subs.py` | Analysis marked incomplete; retried on rerun or via UI button; old analysis kept (`superseded_by`) |
| 8. Install instructions for the unmerged PR branch | Implemented | Partially verified | `git clone --branch claude/project-thread-b4clfe …` and the switch/pull sequence run on Linux git; branch checked with `git ls-remote` | PowerShell itself not run |
| 2A. Attribution + usage notes | Implemented | Partially verified | `sources.attribution/usage_notes`, UI form | Stored/displayed; no dedicated test |
| 2B/2C. Twitch discovery and retrieval | Missing | — | — | Этап 2 by design; nullable platform columns already in `sources` |
| 3. Baseline audio-peak detector retained | Implemented | Verified | legacy CLI + `test_clips.py`; `loud_global` signal in `scoring.py` | |
| 3. Rolling baseline, energy rise, scene changes | Implemented | Verified (synthetic) | `test_analysis_candidates_respect_invariants_and_cache` (visual available, anchors at bursts) | Synthetic only |
| 3. VAD (Silero via faster-whisper) | Implemented | Partially verified | runs on synthetic noise in venv; unavailable path tested with system Python | No real speech fixture |
| 3. Timestamped transcription, RU/EN cues, game overrides | Implemented | Partially verified | chunk offsets with stand-in model; `test_cues_ru_en_and_yo`, `test_game_override` | Real recognition Unverified (no model) |
| 3. Unavailable signals marked, not faked | Implemented | Verified | contributions `available: false` asserted in `test_app.py` | |
| 3. Candidate stores source, range, anchor, score, contributions, Russian explanation | Implemented | Verified | `candidates` table; `test_app.py` asserts | |
| 3. HUD extension point, off by default, no templates | Implemented | Verified | `test_hud_extension_point_adds_evidence` (`tests/hud_dummy.py`) | |
| 3. Range invariants after merge/adjust; anchor inside | Implemented | Verified | `test_ranges.py` (1500 randomized plans), regression tests | |
| 3. Speech/silence and scene boundaries | Implemented | Verified (unit) | `test_speech_boundary_preferred_over_scene_cut` | Usefulness on real footage Unverified |
| 3. Manual edits respected, anchor flagged | Implemented | Verified | `ManualEditTests`, UI smoke | |
| 3. 20–60 s target without stretching | Implemented | Verified (unit) | `test_target_min_takes_real_context_only` | |
| 4. 127.0.0.1 stdlib server, no Node/build | Implemented | Verified | `test_foreign_host_rejected`, `make_server` refuses other hosts | |
| 4. Import, settings, progress, cancel, preview, explanations, edit, crop preview, subtitles toggle, SRT, review, export, open folder | Implemented | Verified (headless Chromium) | `test_ui_smoke.py::test_browser_import_review_export` | "Open folder" not clicked in test (no desktop) |
| 4. Russian errors/empty states; refresh doesn't duplicate jobs | Implemented | Verified | `test_bad_import_shows_russian_error`, reload check in UI smoke, `test_cancel_and_rerun` | |
| 5. MP4 H.264/AAC 1080x1920 fit-over-blur, crop layouts | Implemented | Verified | `test_export_validation_skip_and_forced` (ffprobe + full decode) | |
| 5. Loudness normalisation without clipping | Implemented | Unverified | `loudnorm I=-14 TP=-1.5` in `render.py` | Levels not measured in tests |
| 5. Video-only input → silent AAC | Implemented | Verified | `test_video_only_export_gets_silent_aac` | |
| 5. SRT clipping/rebasing, burned subtitles, Cyrillic font | Implemented | Verified | `test_subtitles.py` (±50 ms), `test_srt_written_and_subtitles_burned` | Font: Arial (Windows) / DejaVu Sans (Linux); Windows rendering Unverified |
| 5. Temp render → ffprobe → full decode → rename; duration ±0.2 s | Implemented | Verified | same export test; no `*.part.mp4` left | |
| 6. SQLite + migrations, nullable platform fields | Implemented | Verified | `test_schema_version`; `db.py` `MIGRATIONS` | |
| 6. Source identity, dedupe, cache invalidation, cache keys | Implemented | Verified | `test_moved_copy_keeps_identity`, `test_modified_media_invalidates_analysis` | |
| 6. Export identity, skip, forced → new name | Implemented | Verified | `test_export_validation_skip_and_forced` | |
| 6. Streaming / bounded memory | Implemented | Verified (synthetic) | `tools/measure_memory.py --minutes 10 60` (see below) | |
| 6. Cancellation kills children, rerunnable; interrupted jobs | Implemented | Verified | `test_cancel_and_rerun`, `test_interrupted_jobs_marked_on_restart` | |
| 6. Corrupted/missing/no-audio/silent input | Implemented | Verified | `test_corrupted_file_is_a_clear_error`, `test_missing_source_reported`, `test_silence_gives_no_candidates_without_error` | |
| 6. Disk-full handling | Partial | Unverified | `friendly_os_error`, `ffmpeg_error_text` | Not simulated |
| 7. Interval marking, accept/reject, top-K, missed marks, notes, job time/hardware | Implemented | Verified (logic) | `test_evaluation_counts_and_missed_marks`, UI smoke opens evaluation page | |
| 7. Highlight quality on real gameplay | Missing | Unverified | — | No real GTA VI footage available |
| 8. Russian README, PowerShell commands, config.example.toml, pinned requirements | Implemented | Unverified on Windows | `README.md`, `config.example.toml`, `requirements*.txt` | winget IDs checked against winget-pkgs manifests only; Windows wheels for cp312 resolved with `pip download --platform win_amd64` |
| 9. Both reported regressions | Implemented | Verified | pre-fix worktree `3431477`: 2 FAIL + 3 ERROR; head: OK | |
| 9. Browser smoke test | Implemented | Verified | `test_ui_smoke.py` (Playwright, headless Chromium 153) | |

## Analysis time and memory (not full-pipeline throughput)

`tools/measure_memory.py --minutes 10 60` measures **analysis only**: a synthetic 160x90, 10 fps source, Whisper **disabled**,
**no vertical export**. Signals: loudness, VAD (Silero), scene change at 2 fps. Each file runs in its own `gta6clips analyze`
process, and RSS of the whole process tree is sampled every 0.1 s. Run on the code at this status update.

| Input | Analysis wall time | Peak tree RSS | Python / ffmpeg at peak | Candidates |
| --- | --- | --- | --- | --- |
| 10 min (15.7 MB) | 8.1 s | 220.6 MB | 162.0 / 58.6 MB | 2 |
| 60 min (94.3 MB) | 35.7 s | 220.6 MB | 164.4 / 56.3 MB | 10 |

The peak doesn't grow with duration. Allocations that do depend on duration are per-window arrays (a few float32 values per 0.5 s, so ~7,200 per hour), the scene-score array, VAD segment list and a rolling-median window view (~3.5 MB per hour while scoring). These numbers don't cover Whisper model loading or transcription time, rendering, real full-resolution footage, or Windows.

## Implementation decisions

- Kept `gta6clips/detect.py`, `cut.py` and the original CLI unchanged as the quick path. The new pipeline (`app.py`) reuses the loudness detector idea as `loud_global` and adds anchor-aware planning in `ranges.py`, because the old `plan_clips` doesn't track event anchors.
- Python ≥ 3.12 (uses `tomllib` and PEP 701 f-strings). Recommended version on Windows is 3.12.
- Paths are typed or pasted rather than chosen in a browser file picker, because browsers don't expose full local paths.
- Burned subtitles: ffmpeg runs with `cwd` set to a temp folder and a relative `subs.srt`, which avoids escaping `C:\` in filtergraph syntax.
- Speech is transcribed in 300 s chunks to keep memory bounded. A word that falls across a chunk boundary can be lost, and the UI shows this note.
- Incomplete analyses (speech or VAD requested but unavailable for a fixable reason) aren't served from cache. They're retried only if the missing runtime can now be imported, and the result supersedes the old analysis (kept in the DB) with decisions carried over by anchor ±1 s.
- One background worker runs jobs one at a time, so the UI stays responsive. Jobs are deduplicated by a key built from parameters.
