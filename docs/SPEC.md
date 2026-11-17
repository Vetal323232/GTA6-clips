# GTA6 clips — product specification

Source: the product owner's specification for PR #1, saved verbatim (sections 1–9).
The Milestone 1 implementation status is tracked in [STATUS.md](STATUS.md).

## 1. Product goal, runtime, and cost

Build a local tool with this workflow:
Discover or import footage -> analyze candidate moments -> review and adjust -> export selected clips.

GTA VI is the intended primary game. Game and channel settings must be configurable. Do not assume a release date, available footage, a Twitch category ID, or a specific GTA VI HUD.

Use Python, FFmpeg/ffprobe, local storage, and free local models where useful. Do not introduce paid AI APIs or cloud inference into the default workflow.

The application must operate without Claude Code after development.

Inspect actual hardware and installed software. Provide a usable CPU configuration. Default speech inference to CPU int8 when supported. GPU acceleration is optional and must be selected only after the speech runtime confirms that a usable device and compatible dependencies are available. Fall back clearly to CPU if GPU initialization fails.

Document model caches and initial downloads. Show an actual or clearly labeled estimated download size when available; do not invent an exact number. Respect the 1 GB asset limit in the operating rules.

## 2. Source workflows

### 2A. Local sources — implement in Milestone 1.

Support an individual local video and a folder of recordings, including long gameplay sessions and existing short clips. Document how file selection and folder paths work in the actual UI/runtime.

Keep this workflow functional without Twitch credentials. Allow transcription to be disabled. Missing speech models must not prevent baseline detection, review, and export.

Preserve source attribution and optional usage-permission notes.

### 2B. Twitch discovery — Milestone 2 only.

Before implementation, check current official Twitch documentation.

Support real category lookup, discovery of existing clips by game or selected channels, date ranges, language/duration filters, pagination, rate limits, and duplicate prevention.

Do not silently substitute another game. Permit another game to be selected explicitly for testing.

Store available platform metadata: platform, clip ID, original URL, channel, creator, title, creation time, duration, language, view count, and usage notes.

Do not describe one view-count snapshot as measured growth. If recency affects ranking, document how age is handled.

### 2C. Authorized media retrieval — Milestone 2 only.

Separate finding clip metadata from downloading media. Implement retrieval only through mechanisms supported by the platform and the user's actual authorization.

If retrieval is unavailable, keep the source link and metadata, explain the missing access, allow a local file to be associated with the source, and continue with other eligible items.

Do not fabricate successful API calls, downloadable media, or authorization.

## 3. Candidate detection and boundaries

Retain the existing audio-peak detector as a baseline. Loudness is candidate evidence, not proof of an entertaining moment.

Add signals progressively during Milestone 1:
- Loudness relative to a rolling local baseline.
- Sudden changes in audio energy.
- Voice activity and silence when the selected local runtime supports them.
- Timestamped speech transcription.
- Configurable Russian and English transcript cues, with optional game-specific overrides.
- Low-frame-rate visual activity or scene-change scores.

Use a suitable maintained local speech-recognition implementation, such as faster-whisper. Check official faster-whisper/CTranslate2 documentation for actual Python, Windows, CPU, and GPU compatibility.

Default to a small multilingual model rather than an English-only model. Preserve the spoken language by default, allow a language override, and report uncertainty or unavailable transcription honestly.

A missing optional signal must be represented as unavailable, not assigned fabricated observations.

For each candidate, store its source, start/end, event anchor, ranking score, signal contributions, and a factual Russian explanation of the selection.

Keep heuristic scoring understandable and configurable. Do not require custom training, a large local language model, or automatic learning from user decisions.

Provide an optional HUD-detector extension point, disabled by default. Do not ship guessed GTA VI templates. HUD events can add evidence; they are not mandatory confirmation.

Provide configurable setup and aftermath padding. Use speech/silence boundaries when available and helpful. Scene changes may suggest a boundary but must not unconditionally replace a better contextual boundary.

For every automatically generated or shortened range:
- 0 <= start < end <= source duration.
- Duration does not exceed the configured maximum.
- The designated event anchor remains inside the range.
- Overlapping candidates are merged or suppressed without creating an overlength clip.
- Reapply invariants after every merge or boundary adjustment.

Manual edits must be respected. If a user intentionally excludes an anchor, update or flag the anchor rather than silently undoing the requested edit.

Target 20–60 seconds by default, configurable. Naturally shorter clips are permitted; never stretch, duplicate, or invent footage to satisfy a minimum.

## 4. Local review interface

Use a lightweight Python web server bound to 127.0.0.1 only, server-rendered HTML, and minimal vanilla JavaScript. No Node requirement or frontend build step.

Support:
- Importing a file or folder.
- Choosing source and processing settings.
- Starting a job with progress.
- Cancelling a running job.
- Previewing candidates.
- Inspecting signal explanations and source information.
- Editing start/end times.
- Choosing a layout and previewing an adjustable crop.
- Turning burned-in subtitles on/off and exporting separate SRT files.
- Accepting/rejecting candidates, with an optional reason.
- Exporting selected clips and opening the configured output folder.

Preserve essential gameplay information in the default preview/export layout.

Keep errors and empty states clear and in Russian. Silence and "no candidates" can be normal outcomes; do not report them as crashes.

Keep the UI responsive during analysis and rendering. A browser refresh must not accidentally duplicate a running job.

## 5. Video and subtitle output

Default output:
- MP4, H.264 video, AAC audio.
- 1080 x 1920 vertical layout.
- Entire gameplay frame fitted over a blurred background.
- Optional adjustable crop with preview.
- At most five clips per run, configurable.
- Sensible audio levels without clipping.

Do not remove essential gameplay/HUD through an unconditional center crop. Do not add unrequested music, translation, misleading captions, or automatic publishing.

For video-only input, either support export with a documented silent AAC track or clearly explain the supported behavior. Missing audio must not cause an unhandled exception.

Support optional burned-in subtitles and separate SRT files. Use a font with Latin and Cyrillic coverage and readable margins.

Subtitle handling must:
- Discard source cues that do not intersect the selected clip.
- Intersect overlapping cues with the clip interval.
- Rebase the resulting timestamps to the start of the clip.
- Keep 0 <= cue_start < cue_end <= exported duration.
- Preserve order and valid SRT syntax.
- Handle edits to clip boundaries by regenerating timing.

The +/-50 ms subtitle test tolerance applies to deterministic timestamp conversion, not to speech-recognition accuracy.

Write incomplete renders to temporary files. Mark an export successful only after metadata checks and a complete decode.
Use ffprobe to check streams, dimensions, and duration.
Use a full-decode equivalent of:
`ffmpeg -v error -i OUTPUT -map 0:v:0 -map 0:a:0? -f null -`

Handle platform-specific quoting correctly and capture failures.

## 6. Storage, caching, and reliability

Use SQLite with schema versioning and migrations for sources, jobs, analysis results/cache references, candidates, review decisions, exports, and evaluation records.

Include nullable platform metadata fields now to reduce later schema changes, without promising that future migrations will be unnecessary.

Define and document source identity and export identity.
- Re-importing an unchanged source must not create a duplicate source.
- Modified media must invalidate affected cached analysis.
- Include relevant detector/model versions and parameters in analysis cache keys.
- Re-exporting the same candidate with identical settings must skip the existing export unless forced.
- A forced export must get a new filename, not overwrite a previous export.

Stream or chunk long media processing. Do not load the entire decoded audio/video into memory. Keep caches and temporary files documented and bounded.

Handle unsupported/corrupted input, missing tools/models, absent speech/audio, disk-space failures, unavailable sources, cancellation, and interrupted jobs.

Cancellation must terminate the relevant child processes, preserve database consistency, and leave the job rerunnable. On restart, identify interrupted jobs rather than treating them as successful.

Keep downloaded secrets and private media out of committed fixtures.

## 7. Quality evaluation

Separate:
- Technical correctness of import, timestamps, detection signals, and export.
- Actual usefulness of selected gameplay highlights.

Allow manual marking of interesting source intervals and accepting/rejecting candidates.

Record:
- Accepted top-K candidates, including the number actually reviewed.
- Marked intervals not covered by any candidate under a documented matching rule/tolerance.
- Boundary/context notes.
- Per-job processing time, enabled signals/models, and hardware.

Report pending/unreviewed candidates separately from rejected ones.
Do not automatically use rejection decisions as training labels.

Start with a small representative recording when available. Do not require many hours of labeled footage to build the tool.

Synthetic media verifies technical behavior only. If real gameplay is unavailable, complete independent technical work and explicitly defer highlight-quality conclusions.

## 8. Documentation and delivery

Deliver:
- The working application and tests.
- Compatible, reproducible dependency configuration for a supported Python version.
- A documented CPU installation path; GPU dependencies are optional and separately documented.
- config.example with placeholders, plus appropriate ignore rules.
- A Russian README with exact Windows PowerShell setup and launch commands.
- A local installation path for Python dependencies.
- Guidance for installing Python and FFmpeg, including verified winget package identifiers where appropriate; do not run system-wide installs without authorization.
- First-run, model download/cache, CPU/GPU, configuration, and troubleshooting instructions.

Clearly distinguish commands actually exercised on Windows from commands checked only by inspection or on another OS.

Document Twitch as "Этап 2" until it is implemented. Do not present it as currently available.

Save the complete numbered specification, sections 1–9, as docs/SPEC.md.
Maintain docs/STATUS.md with a concise table covering all nine sections:
Requirement | Implemented / Partial / Missing | Verified / Partially verified / Unverified | Evidence | Notes
Use precise file references and commands. Keep the file under 150 lines; record brief implementation decisions below the table.

## 9. Verification and roadmap

Mandatory technical coverage:
- Both reported regressions.
- Candidate bounds, maximum duration, overlap handling, and anchors.
- Correct subtitle clipping/rebasing, including cues crossing either boundary.
- Silence, no-audio input, and corrupted files.
- File paths with spaces and Cyrillic.
- Playable exports, expected codecs/dimensions, and duration within +/-0.2 seconds of the selected range, allowing documented frame alignment.
- Duplicate prevention, cache invalidation, and safe forced re-export.
- Job cancellation and rerun.
- A browser-driven smoke test of the local import -> review -> export workflow.
- Memory behavior on a long synthetic source.

Use deterministic tests where possible. Property-style randomized cases are appropriate for interval invariants; a new testing framework is not required solely for that purpose.

Generate clearly named synthetic fixtures with FFmpeg lavfi at test time. Do not commit large binaries.

Prefer an available offline speech fixture for transcription integration. If none exists, test subtitle conversion with explicit timestamped cues, but label actual speech recognition and transcript cue behavior Unverified until exercised.

Use a low-resolution fixture of at least 60 minutes for the streaming check. Compare with a shorter fixture using the same processing configuration. Measure peak memory across the job process tree, including Python, FFmpeg, and other worker processes. Report model loading separately where relevant.
Do not call streaming behavior verified merely because a long file finishes. Examine memory growth and explain any duration-dependent allocations.
If the execution environment cannot measure required process-tree memory or run the long check within its limits, report the verification gap instead of inventing a number.

For UI testing, use the available browser-control tool or headless browser automation such as Playwright. Do not assume a specific browser pane exists. Report the actual steps and outcomes.

Roadmap:
- Milestone 1: usable local workflow and technical verification.
- Milestone 2: Twitch discovery and supported authorized retrieval.
- Milestone 3: evaluation on representative gameplay and evidence-based ranking improvements.

This run implements Milestone 1 only.
