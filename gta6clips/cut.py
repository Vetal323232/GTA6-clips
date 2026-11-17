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
    """Return the video's length in seconds, or raise RuntimeError if unreadable."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", video],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ffprobe exited with code {result.returncode}"
        raise RuntimeError(
            f"не удалось прочитать {video}. Файл повреждён или это не видео.\n{detail}"
        )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"не удалось определить длительность {video} ({e})") from None
    if duration <= 0:
        raise RuntimeError(f"у файла {video} нулевая длительность")
    return duration


def plan_clips(
    moments: list[Moment],
    duration: float,
    before: float = 5.0,
    after: float = 3.0,
    max_len: float = 60.0,
    top: int | None = None,
) -> list[Clip]:
    """Pad each moment, clamp to the video, merge overlaps, keep the loudest.

    Guarantees every clip is at most `max_len` seconds and clips never overlap.
    Overlapping moments are merged only while the merged clip still fits in
    `max_len`; otherwise the next clip starts where the previous one ends.
    """
    clips: list[Clip] = []
    for m in sorted(moments, key=lambda m: m.start):
        start = max(0.0, m.start - before)
        end = min(duration, m.end + after)
        if end - start > max_len:  # very long loud stretch: centre on the peak
            start = max(0.0, min(m.peak_time - max_len / 2, duration - max_len))
            end = min(duration, start + max_len)
        if clips and start <= clips[-1].end:
            prev = clips[-1]
            if end <= prev.end:  # fully inside the previous clip
                prev.peak_db = max(prev.peak_db, m.peak_db)
                continue
            if end - prev.start <= max_len:
                prev.end = end
                prev.peak_db = max(prev.peak_db, m.peak_db)
                continue
            start = prev.end  # too long to merge: continue right after
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
        raise RuntimeError(f"ffmpeg не смог вырезать {out_path.name}:\n{result.stderr.strip()}")
