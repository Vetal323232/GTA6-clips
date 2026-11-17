"""Find highlight moments from audio loudness peaks.

The audio track is decoded by ffmpeg to mono 16-bit PCM, split into short
windows, and each window's loudness is measured in dBFS. A window counts as a
highlight when it is much louder than the rest of the video (explosions,
gunfire, crashes). Nearby loud windows are grouped into one moment.
"""

from __future__ import annotations

import array
import math
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass

SAMPLE_RATE = 8000  # plenty for loudness, keeps memory low on long videos
SILENCE_DB = -90.0


@dataclass
class Moment:
    start: float  # seconds, first loud window
    end: float  # seconds, end of last loud window
    peak_time: float  # seconds, loudest window
    peak_db: float


def read_loudness(video: str, window: float = 0.5) -> list[float]:
    """Return loudness in dBFS for each `window`-second chunk of the audio."""
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", video,
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    chunk_samples = max(1, int(SAMPLE_RATE * window))
    chunk_bytes = chunk_samples * 2
    levels: list[float] = []
    with tempfile.TemporaryFile() as err, subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=err
    ) as proc:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(chunk_bytes)
            if len(data) % 2:
                data = data[:-1]
            if not data:
                break
            samples = array.array("h", data)
            if sys.byteorder == "big":
                samples.byteswap()
            mean_sq = sum(s * s for s in samples) / len(samples)
            rms = math.sqrt(mean_sq) / 32768.0
            levels.append(20 * math.log10(rms) if rms > 0 else SILENCE_DB)
        code = proc.wait()
        if code != 0:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg could not read audio from {video}:\n{msg}")
    if not levels:
        raise RuntimeError(f"No audio track found in {video}")
    return levels


def find_moments(
    levels: list[float],
    window: float = 0.5,
    sensitivity: float = 2.0,
    min_db: float | None = None,
    merge_gap: float = 3.0,
) -> list[Moment]:
    """Group loud windows into moments.

    A window is "loud" when it is at least `sensitivity` standard deviations
    above the median loudness (or above `min_db` if given). Loud windows
    closer than `merge_gap` seconds are merged into one moment.
    """
    audible = [l for l in levels if l > SILENCE_DB]
    if not audible:
        return []
    if min_db is None:
        median = statistics.median(audible)
        spread = statistics.pstdev(audible) or 1.0
        threshold = median + sensitivity * spread
    else:
        threshold = min_db

    moments: list[Moment] = []
    for i, level in enumerate(levels):
        if level < threshold:
            continue
        t = i * window
        if moments and t - moments[-1].end <= merge_gap:
            m = moments[-1]
            m.end = t + window
            if level > m.peak_db:
                m.peak_db, m.peak_time = level, t
        else:
            moments.append(Moment(start=t, end=t + window, peak_time=t, peak_db=level))
    return moments
