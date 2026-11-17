"""Measure processing time and peak process-tree memory of `gta6clips analyze`.

Generates low-resolution synthetic recordings (FFmpeg lavfi) of the given
lengths, runs the same analysis configuration on each in a fresh process and
samples RSS of the whole process tree (Python + ffmpeg children) every 0.1 s.

Usage:  python tools/measure_memory.py --minutes 10 60 [--keep DIR]
Synthetic media verifies streaming behaviour only, not highlight quality.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from fixtures import make_long  # noqa: E402


def tree_rss(proc: psutil.Process) -> tuple[int, dict]:
    total, parts = 0, {}
    for p in [proc, *proc.children(recursive=True)]:
        try:
            rss = p.memory_info().rss
            name = p.name()
        except psutil.Error:
            continue
        total += rss
        parts[name] = parts.get(name, 0) + rss
    return total, parts


def run(path: Path, workdir: Path, extra: list[str]) -> dict:
    cfg = workdir / "config.toml"
    cfg.write_text(f'[general]\ndata_dir = "{(workdir / "data").as_posix()}"\noutput_dir = "{(workdir / "out").as_posix()}"\n',
                   encoding="utf-8")
    cmd = [sys.executable, "-m", "gta6clips", "analyze", str(path), "--json", *extra]
    start = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=workdir,
                            env={**__import__("os").environ, "GTA6CLIPS_CONFIG": str(cfg)})
    ps = psutil.Process(proc.pid)
    peak, peak_parts, samples = 0, {}, []
    while proc.poll() is None:
        try:
            total, parts = tree_rss(ps)
        except psutil.Error:
            break
        if total > peak:
            peak, peak_parts = total, parts
        samples.append((round(time.monotonic() - start, 1), round(total / 2**20, 1)))
        time.sleep(0.1)
    out, err = proc.communicate()
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        raise SystemExit(f"analyze failed: {err.decode(errors='replace')[-2000:]}")
    report = json.loads(out)
    return {"elapsed_s": round(elapsed, 2), "peak_tree_mb": round(peak / 2**20, 1),
            "peak_parts_mb": {k: round(v / 2**20, 1) for k, v in peak_parts.items()},
            "candidates": len(report[0]["candidates"]), "job_elapsed_s": report[0]["elapsed_s"],
            "rss_timeline_mb": samples[:: max(1, len(samples) // 20)]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, nargs="+", default=[10, 60])
    ap.add_argument("--keep")
    ap.add_argument("--speech", action="store_true", help="enable speech (needs the model)")
    args = ap.parse_args()
    base = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="gta6clips-mem-"))
    base.mkdir(parents=True, exist_ok=True)
    results = {"hardware": {"os": platform.platform(), "cpu_count": psutil.cpu_count(),
                            "ram_gb": round(psutil.virtual_memory().total / 2**30, 1), "python": platform.python_version()},
               "signals": "audio loudness, VAD (Silero), scene change at 2 fps" + (", speech" if args.speech else
                                                                                    "; speech disabled"),
               "runs": []}
    for m in args.minutes:
        media = base / f"synthetic_{m}min_160x90.mp4"
        if not media.exists():
            t = time.monotonic()
            make_long(str(media), m)
            print(f"generated {media.name} in {time.monotonic() - t:.0f} s", file=sys.stderr)
        work = base / f"run_{m}"
        work.mkdir(exist_ok=True)
        r = run(media, work, [] if args.speech else ["--no-speech"])
        r["minutes"] = m
        r["file_mb"] = round(media.stat().st_size / 2**20, 1)
        results["runs"].append(r)
        print(json.dumps(r, ensure_ascii=False), file=sys.stderr)
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
