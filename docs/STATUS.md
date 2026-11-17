# Milestone 1 status

Baseline snapshot before Milestone 1 work (PR #1 head `40cb61b`). Updated as work lands.

| Requirement | Implemented / Partial / Missing | Verified / Partially verified / Unverified | Evidence | Notes |
| --- | --- | --- | --- | --- |
| 1. Local, free runtime (Python + FFmpeg, no paid APIs) | Partial | Verified (Linux) | `python -m unittest discover -s tests` (10 OK) | No config, no speech, no GPU logic |
| 2A. Local file import | Partial | Verified (Linux) | `tests/test_clips.py::EndToEndTests` | CLI only; no folder import, no attribution notes |
| 2B/2C. Twitch | Missing | — | — | Этап 2 |
| 3. Audio-peak baseline detector | Implemented | Verified (synthetic) | `gta6clips/detect.py`, `test_detects_bursts` | Global median+std threshold only |
| 3. Rolling baseline, energy change, VAD, transcript, cues, visual | Missing | — | — | |
| 3. Range invariants (bounds, max length, overlap) | Partial | Verified | `test_merged_clip_never_exceeds_max_len`, randomized test | No anchors, no manual edits |
| 4. Local review web UI | Missing | — | — | |
| 5. Vertical 1080x1920 export, subtitles, output validation | Missing | — | `gta6clips/cut.py` cuts source aspect only | |
| 6. SQLite, caching, identities, jobs, cancellation | Missing | — | — | |
| 7. Quality evaluation | Missing | — | — | |
| 8. Russian README, config.example, Windows guide | Missing | — | README is English | |
| 9. Regression tests | Implemented | Verified | Pre-fix worktree at `3431477`: 2 FAIL + 3 ERROR; head: OK | |
