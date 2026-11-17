"""Command line entry point: `gta6clips VIDEO`."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .cut import cut_clip, plan_clips, video_duration
from .detect import find_moments, read_loudness


def fmt(t: float) -> str:
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gta6clips",
        description="Cut highlight clips from gameplay video using audio loudness peaks.",
    )
    p.add_argument("video", help="input gameplay video (mp4, mkv, mov, ...)")
    p.add_argument("-o", "--out", default="clips", help="output folder (default: clips)")
    p.add_argument("-b", "--before", type=float, default=5.0,
                   help="seconds to keep before a highlight (default: 5)")
    p.add_argument("-a", "--after", type=float, default=3.0,
                   help="seconds to keep after a highlight (default: 3)")
    p.add_argument("-s", "--sensitivity", type=float, default=2.0,
                   help="how far above normal loudness counts as a highlight, in "
                        "standard deviations; lower finds more clips (default: 2.0)")
    p.add_argument("--min-db", type=float, default=None,
                   help="fixed loudness threshold in dBFS instead of --sensitivity, e.g. -12")
    p.add_argument("--window", type=float, default=0.5,
                   help="analysis window in seconds (default: 0.5)")
    p.add_argument("--merge-gap", type=float, default=3.0,
                   help="merge loud moments closer than this many seconds (default: 3)")
    p.add_argument("--max-len", type=float, default=60.0,
                   help="longest allowed clip in seconds (default: 60)")
    p.add_argument("-n", "--top", type=int, default=None,
                   help="only keep the N loudest clips")
    p.add_argument("--copy", action="store_true",
                   help="stream copy instead of re-encoding (fast, less precise cuts)")
    p.add_argument("--dry-run", action="store_true",
                   help="list detected clips without cutting anything")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"error: {tool} not found on PATH. Install ffmpeg first.", file=sys.stderr)
            return 2
    video = args.video
    if not Path(video).is_file():
        print(f"error: {video} does not exist", file=sys.stderr)
        return 2

    try:
        duration = video_duration(video)
        print(f"Analysing audio of {video} ({fmt(duration)})...")
        levels = read_loudness(video, args.window)
        moments = find_moments(levels, args.window, args.sensitivity, args.min_db, args.merge_gap)
        clips = plan_clips(moments, duration, args.before, args.after, args.max_len, args.top)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not clips:
        print("No highlights found. Try a lower --sensitivity (e.g. 1.5).")
        return 0

    print(f"Found {len(clips)} highlight(s):")
    for i, c in enumerate(clips, 1):
        print(f"  {i:3d}. {fmt(c.start)} - {fmt(c.end)}  ({c.duration:.1f}s, peak {c.peak_db:.1f} dBFS)")
    if args.dry_run:
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video).stem
    ext = Path(video).suffix if args.copy else ".mp4"
    for i, c in enumerate(clips, 1):
        name = f"{stem}_clip{i:03d}_{int(c.start)}s{ext}"
        path = out_dir / name
        print(f"Cutting {path} ...")
        try:
            cut_clip(video, c, path, copy=args.copy)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    print(f"Done. {len(clips)} clip(s) saved to {out_dir}/")
    return 0
