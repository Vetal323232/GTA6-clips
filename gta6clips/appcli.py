"""Subcommands for the full local workflow: serve, analyze, doctor."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time

from .config import load_config
from .media import MediaError, check_tools


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="gta6clips", description="GTA6 clips — локальный поиск и нарезка моментов")
    p.add_argument("--config", help="путь к config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="запустить веб-интерфейс на 127.0.0.1")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    a = sub.add_parser("analyze", help="импортировать файл/папку и проанализировать без интерфейса")
    a.add_argument("path")
    a.add_argument("--no-speech", action="store_true", help="отключить распознавание речи")
    a.add_argument("--no-visual", action="store_true", help="отключить анализ изображения")
    a.add_argument("--no-vad", action="store_true", help="отключить VAD")
    a.add_argument("--json", action="store_true", help="вывести результат в JSON")
    sub.add_parser("doctor", help="проверить FFmpeg, Python-пакеты и устройство для речи")
    args = p.parse_args(argv)

    missing = check_tools()
    if missing and args.cmd != "doctor":
        print(f"ошибка: не найдено {', '.join(missing)}. Установите FFmpeg и добавьте его в PATH.", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2

    if args.cmd == "doctor":
        return doctor(cfg)

    from .app import App
    app = App(cfg)
    if args.cmd == "serve":
        from .web import serve
        try:
            serve(app, port=args.port or int(cfg["general"]["port"]), open_browser=not args.no_browser)
        except OSError as e:
            print(f"ошибка: не удалось запустить сервер ({e}). Возможно, порт занят: укажите --port 8766", file=sys.stderr)
            return 1
        return 0

    # analyze
    try:
        results = app.import_path(args.path)
    except MediaError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        app.close()
        return 1
    overrides = {}
    if args.no_speech:
        overrides["speech_enabled"] = False
    if args.no_visual:
        overrides["visual"] = False
    if args.no_vad:
        overrides["vad"] = False
    report, code = [], 0
    try:
        for r in results:
            if r["status"] == "error":
                print(f"ошибка: {r['name']}: {r['error']}", file=sys.stderr)
                code = 1
                continue
            jid, _ = app.start_analysis(r["id"], overrides)
            last = ""
            while True:
                row = app.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
                if row["status"] not in ("queued", "running"):
                    break
                if not args.json and row["message"] != last:
                    last = row["message"]
                    print(f"  {r['name']}: {last}", flush=True)
                time.sleep(0.5)
            if row["status"] != "done":
                print(f"ошибка: {r['name']}: {row['error'] or row['message']}", file=sys.stderr)
                code = 1
                continue
            an = app.latest_analysis(r["id"])
            cands = [dict(c) for c in app.candidates(an["id"])]
            report.append({"source": r["name"], "job_id": jid, "elapsed_s": row["elapsed"],
                           "peak_rss_mb": row["peak_rss_mb"], "message": row["message"],
                           "candidates": [{k: c[k] for k in ("rank", "start", "end", "anchor", "score", "explanation")}
                                          for c in cands]})
            report[-1]["complete"] = bool(an["complete"])
            report[-1]["incomplete_reason"] = an["incomplete_reason"]
            if not args.json:
                print(f"{r['name']}: {row['message']} (за {row['elapsed']:.1f} с)")
                if not an["complete"]:
                    print("  Анализ неполный. После исправления повторите эту же команду: "
                          "звук и изображение возьмутся из кэша, повторится только недостающее.")
                for c in cands:
                    print(f"  {c['rank']:2d}. {c['start']:.1f}–{c['end']:.1f} с, оценка {c['score']:.2f}")
    except KeyboardInterrupt:
        print("Отмена…", file=sys.stderr)
        code = 130
    finally:
        app.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return code


def doctor(cfg) -> int:
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        print(f"{tool}: {path or 'НЕ НАЙДЕН'}")
        ok &= bool(path)
    print(f"Python: {sys.version.split()[0]}")
    from .speech import ESTIMATED_MODEL_SIZE, cuda_device_count, runtime_available
    avail, reason = runtime_available()
    print(f"faster-whisper: {'установлен' if avail else reason}")
    try:
        import onnxruntime  # noqa: F401
        print("VAD (Silero через onnxruntime): доступен")
    except Exception:
        print("VAD: недоступен (нужен пакет faster-whisper)")
    sp = cfg["speech"]
    print(f"Речь: {'включена' if sp['enabled'] else 'выключена'}, модель {sp['model']} "
          f"({ESTIMATED_MODEL_SIZE.get(sp['model'], 'размер неизвестен')}), устройство {sp['device']}/{sp['compute_type']}")
    if avail:
        n = cuda_device_count()
        print(f"CUDA-устройств по данным CTranslate2: {n}" + ("" if n else " (будет использоваться CPU)"))
    print(f"Папка данных: {cfg.data_dir}\nПапка экспорта: {cfg.output_dir}")
    return 0 if ok else 1
