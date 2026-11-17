"""Application services shared by the web UI, the CLI and the tests."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .db import Database, dumps, loads, now
from .jobs import JobContext, JobManager
from .media import MediaError, Runner, find_videos, fingerprint, friendly_os_error, probe
from .ranges import manual_edit
from .render import LAYOUTS, RENDER_VERSION, normalize_crop, render_clip
from .scoring import DETECTOR_VERSION, HudEvent, detect, find_cues, fmt_t
from .signals import Features, SignalStatus, audio_pass, visual_pass
from .speech import ENGINE_VERSION, Segment, transcribe
from .subtitles import Cue as SubCue, clip_cues, to_srt

FEATURES_VERSION = "f1"


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w\-. ]+", "_", Path(name).stem, flags=re.UNICODE).strip(" .") or "clip"
    return stem[:60]


class App:
    def __init__(self, cfg: Config, start_jobs: bool = True):
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.models_dir = self.data_dir / "models"
        self.db = Database(self.data_dir / "gta6clips.sqlite3")
        self.jobs = JobManager(self.db, {"analyze": self._run_analysis, "export": self._run_export}) if start_jobs else None

    def close(self) -> None:
        if self.jobs:
            self.jobs.shutdown()
        self.db.close()

    # ---------------------------------------------------------------- sources
    def import_file(self, path: str, attribution: str = "", usage_notes: str = "", game: str | None = None) -> dict:
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.is_file():
            raise MediaError(f"Файл не найден: {p}")
        info = probe(p)
        if not info.has_video:
            raise MediaError(f"В файле {p.name} нет видеодорожки")
        fp = fingerprint(p)
        st = p.stat()
        meta = {"path": str(p), "filename": p.name, "size": st.st_size, "mtime": st.st_mtime,
                "duration": info.duration, "has_video": int(info.has_video), "has_audio": int(info.has_audio),
                "width": info.width, "height": info.height, "fps": info.fps, "status": "ok", "error": None}
        extra = {k: v for k, v in (("attribution", attribution), ("usage_notes", usage_notes)) if v}
        with self.db.transaction():
            row = self.db.one("SELECT id FROM sources WHERE fingerprint=?", (fp,))
            if row:
                self.db.update("sources", row["id"], {**meta, **extra})
                return {"id": row["id"], "status": "existing", "name": p.name}
            row = self.db.one("SELECT id FROM sources WHERE path=?", (str(p),))
            if row:
                self.db.update("sources", row["id"], {**meta, **extra, "fingerprint": fp})
                return {"id": row["id"], "status": "modified", "name": p.name}
            sid = self.db.insert("sources", {**meta, **extra, "fingerprint": fp, "imported_at": now(),
                                             "game": game or self.cfg["general"]["game"], "platform": "local"})
            return {"id": sid, "status": "new", "name": p.name}

    def import_path(self, path: str, recursive: bool = True, attribution: str = "", usage_notes: str = "") -> list[dict]:
        p = Path(path.strip().strip('"')).expanduser()
        if p.is_dir():
            files = find_videos(p, recursive)
            if not files:
                raise MediaError(f"В папке {p} не найдено видеофайлов")
        elif p.is_file():
            files = [p]
        else:
            raise MediaError(f"Путь не найден: {p}")
        results = []
        for f in files:
            try:
                results.append(self.import_file(str(f), attribution, usage_notes))
            except MediaError as e:
                results.append({"id": None, "status": "error", "name": f.name, "error": str(e)})
            except OSError as e:
                results.append({"id": None, "status": "error", "name": f.name, "error": str(friendly_os_error(e, str(f)))})
        return results

    def source(self, source_id: int):
        return self.db.one("SELECT * FROM sources WHERE id=?", (source_id,))

    def _refresh_source(self, source_id: int):
        """Re-check the file before a job: availability and content changes."""
        src = self.source(source_id)
        if src is None:
            raise MediaError("Источник не найден в базе")
        p = Path(src["path"])
        if not p.is_file():
            self.db.update("sources", source_id, {"status": "missing", "error": "Файл недоступен"})
            raise MediaError(f"Исходный файл недоступен: {p}. Верните файл на место или импортируйте заново.")
        st = p.stat()
        if st.st_size != src["size"] or abs(st.st_mtime - src["mtime"]) > 1e-3:
            info = probe(p)
            fp = fingerprint(p)
            other = self.db.one("SELECT id FROM sources WHERE fingerprint=? AND id<>?", (fp, source_id))
            if other:
                raise MediaError(f"Файл {p.name} изменился и теперь совпадает с другим источником (№{other['id']})")
            self.db.update("sources", source_id, {"fingerprint": fp, "size": st.st_size, "mtime": st.st_mtime,
                                                  "duration": info.duration, "has_audio": int(info.has_audio),
                                                  "width": info.width, "height": info.height, "fps": info.fps,
                                                  "status": "ok", "error": None})
        return self.source(source_id)

    # --------------------------------------------------------------- analysis
    def analysis_params(self, overrides: dict | None = None) -> dict:
        a = dict(self.cfg["analysis"])
        sp = dict(self.cfg["speech"])
        for k, v in (overrides or {}).items():
            if k.startswith("speech_"):
                sp[k[len("speech_"):]] = v
            else:
                a[k] = v
        return {"analysis": a, "speech": sp, "weights": dict(self.cfg["weights"]), "hud": dict(self.cfg["hud"])}

    def cache_key(self, fp: str, params: dict, game: str | None) -> str:
        sp = params["speech"]
        speech_key = ([sp["model"], sp.get("language") or "auto", ENGINE_VERSION, sp.get("chunk_seconds")]
                      if sp.get("enabled") else None)
        return _hash("analysis", fp, DETECTOR_VERSION, FEATURES_VERSION, params["analysis"], params["weights"],
                     speech_key, self.cfg.cues_for(game) if sp.get("enabled") else None,
                     self._hud_identity(params["hud"]), game)

    def start_analysis(self, source_id: int, overrides: dict | None = None) -> tuple[int, bool]:
        src = self.source(source_id)
        if src is None:
            raise MediaError("Источник не найден")
        params = self.analysis_params(overrides)
        key = _hash("job-analyze", source_id, src["fingerprint"], params)
        return self.jobs.submit("analyze", key, {"source_id": source_id, "params": params}, source_id=source_id)

    def latest_analysis(self, source_id: int):
        src = self.source(source_id)
        return self.db.one("SELECT * FROM analyses WHERE source_id=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
                           (source_id, src["fingerprint"]))

    def _features(self, ctx: JobContext, src, a: dict) -> Features:
        key = _hash("features", src["fingerprint"], FEATURES_VERSION, a["window"], bool(a.get("vad")),
                    bool(a.get("visual")), a.get("visual_fps"))
        path = self.cache_dir / "features" / f"{key[:32]}.json.gz"
        if path.is_file():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                feats = Features.from_json(json.load(f))
            feats.status["cache"] = SignalStatus(True, info={"features": "из кэша"})
            return feats
        duration = float(src["duration"])
        feats = Features(window=a["window"], duration=duration)
        visual_share = 0.3 if a.get("visual") else 0.0
        if src["has_audio"]:
            rms, speech, vad_status = audio_pass(src["path"], duration, ctx.runner, a["window"], bool(a.get("vad")),
                                                 lambda x, m: ctx.progress(0.05 + x * (0.6 - visual_share), m))
            feats.rms_db, feats.speech_segments = rms, speech
            feats.status["audio"] = SignalStatus(True)
            feats.status["vad"] = vad_status
            if len(rms) and float(rms.max()) < -60:
                feats.status["audio"].info["silence"] = True
        else:
            feats.status["audio"] = SignalStatus(False, "в файле нет звуковой дорожки")
            feats.status["vad"] = SignalStatus(False, "в файле нет звуковой дорожки")
        if a.get("visual") and src["has_video"]:
            feats.scene = visual_pass(src["path"], duration, ctx.runner, a["window"], float(a.get("visual_fps", 2)),
                                      lambda x, m: ctx.progress(0.65 - visual_share + x * visual_share, m))
            feats.status["visual"] = SignalStatus(True, info={"fps": a.get("visual_fps")})
        else:
            feats.status["visual"] = SignalStatus(False, "анализ изображения отключён в настройках")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(feats.to_json(), f)
        os.replace(tmp, path)
        return feats

    def _transcript(self, ctx: JobContext, src, sp: dict) -> tuple[list[Segment] | None, SignalStatus]:
        if not sp.get("enabled"):
            return None, SignalStatus(False, "распознавание речи отключено")
        if not src["has_audio"]:
            return None, SignalStatus(False, "в файле нет звуковой дорожки")
        key = _hash("transcript", src["fingerprint"], sp["model"], sp.get("language") or "auto", ENGINE_VERSION,
                    sp.get("chunk_seconds"))
        row = self.db.one("SELECT * FROM transcripts WHERE cache_key=? AND status='done'", (key,))
        if row is None:
            ctx.progress(0.7, "Распознавание речи…")
            t = transcribe(src["path"], float(src["duration"]), sp, self.models_dir, ctx.runner,
                           lambda x, m: ctx.progress(0.7 + 0.25 * x, m))
            if not t.available:
                return None, SignalStatus(False, "распознавание недоступно: " + t.reason, {"notes": t.notes})
            with self.db.transaction():
                self.db.execute("DELETE FROM transcripts WHERE cache_key=?", (key,))
                tid = self.db.insert("transcripts", {
                    "source_id": src["id"], "fingerprint": src["fingerprint"], "cache_key": key, "model": t.model, "device": t.device,
                    "compute_type": t.compute_type, "language": t.language,
                    "language_probability": t.language_probability, "status": "done",
                    "message": "; ".join(t.notes), "created_at": now()})
                for s in t.segments:
                    self.db.insert("transcript_segments", {"transcript_id": tid, "start": s.start, "end": s.end,
                                                           "text": s.text, "avg_logprob": s.avg_logprob,
                                                           "no_speech_prob": s.no_speech_prob})
            row = self.db.one("SELECT * FROM transcripts WHERE id=?", (tid,))
        segs = [Segment(r["start"], r["end"], r["text"], r["avg_logprob"], r["no_speech_prob"])
                for r in self.db.all("SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY start", (row["id"],))]
        return segs, SignalStatus(True, info={"model": row["model"], "device": row["device"],
                                              "compute_type": row["compute_type"], "language": row["language"],
                                              "language_probability": row["language_probability"],
                                              "segments": len(segs), "notes": row["message"]})

    def transcript_segments(self, source_id: int) -> list[Segment]:
        src = self.source(source_id)
        if src is None:
            return []
        row = self.db.one("SELECT id FROM transcripts WHERE source_id=? AND fingerprint=? AND status='done' "
                          "ORDER BY id DESC LIMIT 1", (source_id, src["fingerprint"]))
        if row is None or src is None:
            return []
        return [Segment(r["start"], r["end"], r["text"]) for r in
                self.db.all("SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY start", (row["id"],))]

    def _hud_identity(self, hud: dict):
        if not hud.get("enabled") or not hud.get("plugin"):
            return None
        return [hud["plugin"], getattr(self._hud_plugin(hud["plugin"]), "version", "0")]

    @staticmethod
    def _hud_plugin(spec: str):
        module, _, attr = spec.partition(":")
        try:
            return getattr(importlib.import_module(module), attr or "detect")
        except (ImportError, AttributeError) as e:
            raise MediaError(f"Не удалось загрузить HUD-детектор «{spec}»: {e}") from None

    def _run_analysis(self, ctx: JobContext, payload: dict) -> dict:
        started = time.monotonic()
        params = payload["params"]
        a, sp = params["analysis"], params["speech"]
        ctx.progress(0.01, "Проверка файла")
        src = self._refresh_source(payload["source_id"])
        key = self.cache_key(src["fingerprint"], params, src["game"])
        existing = self.db.one("SELECT * FROM analyses WHERE cache_key=?", (key,))
        if existing:
            n = self.db.one("SELECT COUNT(*) c FROM candidates WHERE analysis_id=?", (existing["id"],))["c"]
            ctx.set(signals=existing["signals"])
            return {"analysis_id": existing["id"], "candidates": n, "cached": True,
                    "message": f"Использован сохранённый анализ: кандидатов {n}"}
        feats = self._features(ctx, src, a)
        segments, speech_status = self._transcript(ctx, src, sp)
        cues = find_cues(segments, self.cfg.cues_for(src["game"])) if segments is not None else None
        hud_events, hud_status = None, SignalStatus(False, "HUD-детектор отключён (по умолчанию)")
        if params["hud"].get("enabled") and params["hud"].get("plugin"):
            fn = self._hud_plugin(params["hud"]["plugin"])
            ctx.progress(0.96, "HUD-детектор")
            hud_events = [HudEvent(*e) if not isinstance(e, HudEvent) else e
                          for e in fn(src["path"], float(src["duration"]), ctx.runner, params["hud"])]
            hud_status = SignalStatus(True, info={"events": len(hud_events)})
        statuses = {k: v.as_dict() for k, v in feats.status.items()}
        statuses["speech"] = speech_status.as_dict()
        statuses["cues"] = (SignalStatus(True, info={"matches": len(cues)}) if cues is not None
                            else SignalStatus(False, "нужна расшифровка речи")).as_dict()
        statuses["hud"] = hud_status.as_dict()
        unavailable = {
            "loud_global": feats.status.get("audio", SignalStatus(False)).reason or "нет звука",
            "loud_rolling": feats.status.get("audio", SignalStatus(False)).reason or "нет звука",
            "onset": feats.status.get("audio", SignalStatus(False)).reason or "нет звука",
            "speech": feats.status.get("vad", SignalStatus(False)).reason or speech_status.reason,
            "cue": speech_status.reason or "нет расшифровки",
            "visual": feats.status.get("visual", SignalStatus(False)).reason,
            "hud": hud_status.reason,
        }
        ctx.progress(0.97, "Поиск кандидатов")
        cands = detect(feats, {**self.cfg, "analysis": a, "weights": params["weights"]}, segments, cues,
                       hud_events, unavailable)
        ctx.runner.check()
        with self.db.transaction():
            aid = self.db.insert("analyses", {
                "source_id": src["id"], "fingerprint": src["fingerprint"], "cache_key": key,
                "detector_version": DETECTOR_VERSION, "params": dumps(params), "signals": dumps(statuses),
                "job_id": ctx.job_id, "created_at": now()})
            for rank, c in enumerate(cands, 1):
                self.db.insert("candidates", {
                    "analysis_id": aid, "source_id": src["id"], "rank": rank,
                    "start": round(c.rng.start, 3), "end": round(c.rng.end, 3), "anchor": round(c.rng.anchor, 3),
                    "auto_start": round(c.rng.start, 3), "auto_end": round(c.rng.end, 3),
                    "score": round(c.rng.score, 4), "contributions": dumps(c.contributions),
                    "explanation": c.explanation, "status": "pending"})
        enabled = [k for k, v in statuses.items() if v.get("available")]
        ctx.set(signals=dumps(statuses))
        silence = statuses.get("audio", {}).get("info", {}).get("silence")
        if not cands:
            msg = ("Кандидатов нет: запись тихая или без звука — это нормальный результат" if silence or not src["has_audio"]
                   else "Кандидатов нет: в записи не найдено выделяющихся моментов — это нормальный результат")
        else:
            msg = f"Найдено кандидатов: {len(cands)}"
        return {"analysis_id": aid, "candidates": len(cands), "cached": False, "message": msg,
                "signals_enabled": enabled, "analysis_seconds": round(time.monotonic() - started, 2)}

    # ------------------------------------------------------------- candidates
    def candidate(self, cid: int):
        return self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))

    def candidates(self, analysis_id: int):
        return self.db.all("SELECT * FROM candidates WHERE analysis_id=? ORDER BY rank", (analysis_id,))

    def edit_candidate(self, cid: int, start: float, end: float) -> list[str]:
        c = self.candidate(cid)
        src = self.source(c["source_id"])
        a = loads(self.db.one("SELECT params FROM analyses WHERE id=?", (c["analysis_id"],))["params"])["analysis"]
        s, e, flags = manual_edit(float(start), float(end), c["anchor"], float(src["duration"]), float(a["max_len"]))
        self.db.update("candidates", cid, {"start": round(s, 3), "end": round(e, 3), "edited": 1,
                                           "flags": dumps(flags) if flags else None})
        return flags

    def reset_candidate(self, cid: int) -> None:
        c = self.candidate(cid)
        self.db.update("candidates", cid, {"start": c["auto_start"], "end": c["auto_end"], "edited": 0, "flags": None})

    def review(self, cid: int, status: str, reason: str = "") -> None:
        if status not in ("accepted", "rejected", "pending"):
            raise ValueError(status)
        self.db.update("candidates", cid, {"status": status, "reason": reason.strip() or None,
                                           "reviewed_at": now() if status != "pending" else None})

    def set_layout(self, cid: int, layout: str, crop: dict | None, burn: bool | None) -> None:
        if layout not in LAYOUTS:
            raise ValueError(layout)
        self.db.update("candidates", cid, {"layout": layout, "crop": dumps(normalize_crop(crop)),
                                           "burn_subtitles": None if burn is None else int(bool(burn))})

    def clip_subtitles(self, cid: int) -> list[SubCue]:
        c = self.candidate(cid)
        segs = self.transcript_segments(c["source_id"])
        return clip_cues([SubCue(s.start, s.end, s.text) for s in segs], c["start"], c["end"])

    # ---------------------------------------------------------------- exports
    def export_settings(self, c) -> dict:
        e = self.cfg["export"]
        layout = c["layout"] or e["layout"]
        burn = bool(e["burn_subtitles"]) if c["burn_subtitles"] is None else bool(c["burn_subtitles"])
        return {"layout": layout, "crop": normalize_crop(loads(c["crop"])), "burn_subtitles": burn,
                "write_srt": bool(e["write_srt"]), "width": e["width"], "height": e["height"], "crf": e["crf"],
                "preset": e["preset"], "loudness_lufs": e["loudness_lufs"], "true_peak_db": e["true_peak_db"],
                "font": e["font"], "font_size": e["font_size"]}

    def export_key(self, c, src, settings: dict, srt_text: str) -> str:
        return _hash("export", src["fingerprint"], round(c["start"], 3), round(c["end"], 3), settings,
                     hashlib.sha256(srt_text.encode()).hexdigest() if settings["burn_subtitles"] or settings["write_srt"] else "",
                     RENDER_VERSION)

    def start_export(self, candidate_ids: list[int], force: bool = False) -> tuple[int, bool]:
        limit = int(self.cfg["export"]["max_clips_per_run"])
        ids = list(dict.fromkeys(int(i) for i in candidate_ids))
        if not ids:
            raise MediaError("Не выбрано ни одного клипа для экспорта")
        if len(ids) > limit:
            raise MediaError(f"За один запуск можно экспортировать не больше {limit} клипов (выбрано {len(ids)})")
        first = self.candidate(ids[0])
        states = [(i, self.candidate(i)["start"], self.candidate(i)["end"]) for i in ids]
        key = _hash("job-export", states, force, dict(self.cfg["export"]))
        return self.jobs.submit("export", key, {"candidate_ids": ids, "force": force},
                                source_id=first["source_id"], candidate_id=ids[0] if len(ids) == 1 else None)

    def _unique_path(self, folder: Path, base: str) -> Path:
        p = folder / f"{base}.mp4"
        n = 2
        while p.exists() or p.with_suffix(".srt").exists() or p.with_name(p.stem + ".part.mp4").exists():
            p = folder / f"{base}_v{n}.mp4"
            n += 1
        return p

    def export_candidate(self, cid: int, runner: Runner, force: bool = False) -> dict:
        c = self.candidate(cid)
        src = self._refresh_source(c["source_id"])
        settings = self.export_settings(c)
        cues = self.clip_subtitles(cid)
        srt_text = to_srt(cues) if cues else ""
        key = self.export_key(c, src, settings, srt_text)
        if not force:
            prev = self.db.one("SELECT * FROM exports WHERE export_key=? AND status='done' ORDER BY id DESC LIMIT 1", (key,))
            if prev and Path(prev["path"]).is_file():
                return {"export_id": prev["id"], "path": prev["path"], "skipped": True}
        out_dir = self.cfg.output_dir
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise friendly_os_error(e, str(out_dir)) from None
        base = f"{_safe_stem(src['filename'])}_{fmt_t(c['start']).replace(':', '-')}_{key[:8]}"
        out = self._unique_path(out_dir, base)
        eid = self.db.insert("exports", {"candidate_id": cid, "export_key": key, "path": str(out), "status": "running",
                                         "forced": int(force), "settings": dumps(settings), "start": c["start"],
                                         "end": c["end"], "created_at": now()})
        try:
            res = render_clip(src["path"], c["start"], c["end"], out, runner, has_audio=bool(src["has_audio"]),
                              export_cfg=settings, layout=settings["layout"], crop=settings["crop"],
                              srt_text=srt_text if settings["burn_subtitles"] and srt_text else None,
                              src_fps=src["fps"])
            srt_path = None
            if settings["write_srt"] and srt_text:
                srt_path = out.with_suffix(".srt")
                srt_path.write_text(srt_text, encoding="utf-8")
        except BaseException as e:
            self.db.update("exports", eid, {"status": "cancelled" if type(e).__name__ == "Cancelled" else "failed",
                                            "error": str(e)[:2000]})
            raise
        self.db.update("exports", eid, {"status": "done", "duration": res.duration, "width": res.width,
                                        "height": res.height, "vcodec": res.vcodec, "acodec": res.acodec,
                                        "srt_path": str(srt_path) if srt_path else None})
        return {"export_id": eid, "path": str(out), "srt": str(srt_path) if srt_path else None, "skipped": False}

    def _run_export(self, ctx: JobContext, payload: dict) -> dict:
        ids = payload["candidate_ids"]
        results = []
        for n, cid in enumerate(ids):
            ctx.progress(n / len(ids), f"Экспорт клипа {n + 1} из {len(ids)}")
            results.append(self.export_candidate(cid, ctx.runner, bool(payload.get("force"))))
        skipped = sum(r["skipped"] for r in results)
        msg = f"Экспортировано: {len(results) - skipped}" + (f", пропущено (уже есть): {skipped}" if skipped else "")
        return {"exports": results, "message": msg}

    def exports(self, source_id: int | None = None):
        if source_id is None:
            return self.db.all("SELECT e.*, c.source_id FROM exports e JOIN candidates c ON c.id=e.candidate_id "
                               "ORDER BY e.id DESC LIMIT 200")
        return self.db.all("SELECT e.*, c.source_id FROM exports e JOIN candidates c ON c.id=e.candidate_id "
                           "WHERE c.source_id=? ORDER BY e.id DESC", (source_id,))

    # -------------------------------------------------------------- evaluation
    def mark_interval(self, source_id: int, start: float, end: float, note: str = "") -> int:
        src = self.source(source_id)
        if not (0 <= start < end <= float(src["duration"]) + 1e-6):
            raise ValueError("Интервал должен быть внутри записи, начало раньше конца")
        return self.db.insert("marked_intervals", {"source_id": source_id, "start": start, "end": end,
                                                   "note": note or None, "created_at": now()})

    def evaluation(self, source_id: int, save: bool = False) -> dict:
        from .evaluation import evaluate
        an = self.latest_analysis(source_id)
        cands = [dict(r) for r in self.candidates(an["id"])] if an else []
        marks = [dict(r) for r in self.db.all("SELECT * FROM marked_intervals WHERE source_id=? ORDER BY start", (source_id,))]
        jobs = [dict(r) for r in self.db.all("SELECT * FROM jobs WHERE source_id=? AND kind='analyze' AND status='done' "
                                             "ORDER BY id DESC LIMIT 5", (source_id,))]
        report = evaluate(cands, marks, jobs)
        report["analysis_id"] = an["id"] if an else None
        if save:
            self.db.insert("evaluations", {"source_id": source_id, "analysis_id": report["analysis_id"],
                                           "report": dumps(report), "created_at": now()})
        return report


def version() -> str:
    return __version__
