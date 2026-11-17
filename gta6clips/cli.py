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
        description="Быстрая нарезка клипов по пикам громкости (без базы и интерфейса). "
                    "Интерфейс: gta6clips serve",
    )
    p.add_argument("video", help="видеофайл с геймплеем (mp4, mkv, mov, ...)")
    p.add_argument("-o", "--out", default="clips", help="папка для клипов (по умолчанию: clips)")
    p.add_argument("-b", "--before", type=float, default=5.0,
                   help="секунд до момента (по умолчанию 5)")
    p.add_argument("-a", "--after", type=float, default=3.0,
                   help="секунд после момента (по умолчанию 3)")
    p.add_argument("-s", "--sensitivity", type=float, default=2.0,
                   help="насколько громче обычного должен быть момент, в стандартных "
                        "отклонениях; меньше — больше клипов (по умолчанию 2.0)")
    p.add_argument("--min-db", type=float, default=None,
                   help="фиксированный порог громкости в дБFS вместо --sensitivity, например -12")
    p.add_argument("--window", type=float, default=0.5,
                   help="окно анализа в секундах (по умолчанию 0.5)")
    p.add_argument("--merge-gap", type=float, default=3.0,
                   help="объединять моменты ближе этого числа секунд (по умолчанию 3)")
    p.add_argument("--max-len", type=float, default=60.0,
                   help="максимальная длина клипа в секундах (по умолчанию 60)")
    p.add_argument("-n", "--top", type=int, default=None,
                   help="оставить только N самых громких клипов")
    p.add_argument("--copy", action="store_true",
                   help="без перекодирования (быстро, но резы по ключевым кадрам)")
    p.add_argument("--dry-run", action="store_true",
                   help="только показать найденные моменты")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("serve", "analyze", "doctor"):
        from .appcli import main as app_main
        return app_main(argv)
    args = build_parser().parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"ошибка: {tool} не найден в PATH. Установите FFmpeg.", file=sys.stderr)
            return 2
    video = args.video
    if not Path(video).is_file():
        print(f"ошибка: файл {video} не найден", file=sys.stderr)
        return 2

    try:
        duration = video_duration(video)
        print(f"Анализ звука: {video} ({fmt(duration)})...")
        levels = read_loudness(video, args.window)
        moments = find_moments(levels, args.window, args.sensitivity, args.min_db, args.merge_gap)
        clips = plan_clips(moments, duration, args.before, args.after, args.max_len, args.top)
    except RuntimeError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1

    if not clips:
        print("Ярких моментов не найдено. Попробуйте меньшую --sensitivity (например 1.5).")
        return 0

    print(f"Найдено моментов: {len(clips)}")
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
        print(f"Нарезка {path} ...")
        try:
            cut_clip(video, c, path, copy=args.copy)
        except RuntimeError as e:
            print(f"ошибка: {e}", file=sys.stderr)
            return 1
    print(f"Готово. Клипов: {len(clips)}, папка {out_dir}/")
    return 0
