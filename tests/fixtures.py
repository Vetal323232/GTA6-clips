"""Synthetic test media generated with FFmpeg lavfi at test time (nothing committed).

These verify technical behavior only; they say nothing about highlight quality.
"""

from __future__ import annotations

import subprocess

from make_sample import make_sample  # noqa: F401  (re-export)


def _run(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_silent(path: str, duration: int = 20) -> None:
    """Video with a completely silent audio track."""
    _run(["-f", "lavfi", "-i", f"testsrc2=d={duration}:s=320x180:r=25",
          "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}",
          "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path])


def make_video_only(path: str, duration: int = 20) -> None:
    """Video without any audio stream."""
    _run(["-f", "lavfi", "-i", f"testsrc2=d={duration}:s=320x180:r=25", "-c:v", "libx264",
          "-preset", "ultrafast", path])


def make_bursts(path: str, duration: int, bursts, size="320x180", rate=25, scene_cut: float | None = None) -> None:
    """Quiet pink noise with 1 s loud white-noise bursts; optional hard scene cut."""
    loud = "+".join(f"between(t,{b},{b + 1})" for b in bursts) or "0"
    audio = (f"anoisesrc=d={duration}:c=pink:a=0.02:r=44100[bg];"
             f"anoisesrc=d={duration}:c=white:a=0.9:r=44100,volume='if({loud},1,0)':eval=frame[boom];"
             "[bg][boom]amix=inputs=2:normalize=0[a]")
    if scene_cut is None:
        video = ["-f", "lavfi", "-i", f"testsrc2=d={duration}:s={size}:r={rate}"]
        vmap = "0:v"
        fc = audio
    else:
        video = ["-f", "lavfi", "-i", f"testsrc2=d={duration}:s={size}:r={rate}",
                 "-f", "lavfi", "-i", f"color=c=red:d={duration}:s={size}:r={rate}"]
        fc = audio + f";[0:v][1:v]overlay=enable='gte(t,{scene_cut})'[v]"
        vmap = "[v]"
    _run([*video, "-filter_complex", fc, "-map", vmap, "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
          "-c:a", "aac", "-t", str(duration), path])


def make_long(path: str, minutes: int, bursts_every: int = 300) -> None:
    """Low-resolution long recording for streaming/memory checks."""
    d = minutes * 60
    bursts = list(range(bursts_every // 2, d - 2, bursts_every))
    make_bursts(path, d, bursts, size="160x90", rate=10)
