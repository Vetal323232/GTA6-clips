"""Cut clips out of a video with ffmpeg."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .detect import Moment


@dataclass
class Clip:
    start: float
    end: float
    peak_db: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def video_duration(video: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", video],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def plan_clips(
    moments: list[Moment],
    duration: float,
    before: float = 5.0,
    after: float = 3.0,
    max_len: float = 60.0,
    top: int | None = None,
) -> list[Clip]:
    """Pad each moment, clamp to the video, merge overlaps, keep the loudest."""
    clips: list[Clip] = []
    for m in sorted(moments, key=lambda m: m.start):
        start = max(0.0, m.start - before)
        end = min(duration, m.end + after)
        if end - start > max_len:  # very long loud stretch: centre on the peak
            start = max(0.0, m.peak_time - max_len / 2)
            end = min(duration, start + max_len)
        if clips and start <= clips[-1].end:
            prev = clips[-1]
            prev.end = max(prev.end, end)
            prev.peak_db = max(prev.peak_db, m.peak_db)
        else:
            clips.append(Clip(start, end, m.peak_db))
    if top is not None:
        keep = sorted(clips, key=lambda c: c.peak_db, reverse=True)[:top]
        clips = sorted(keep, key=lambda c: c.start)
    return clips


def cut_clip(video: str, clip: Clip, out_path: Path, copy: bool = False) -> None:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{clip.start:.3f}",
           "-i", video, "-t", f"{clip.duration:.3f}"]
    if copy:
        # Fast, but cuts snap to keyframes so edges can be off by a few seconds.
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    cmd.append(str(out_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed cutting {out_path.name}:\n{result.stderr.strip()}")
