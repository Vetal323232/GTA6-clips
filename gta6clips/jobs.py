"""Background job queue: one worker thread, cancellable, deduplicated.

A job is identified by a dedupe key (kind + source + parameters). Submitting
a job whose key already has a queued or running job returns the existing job,
so a browser refresh or double click can't start it twice. On start-up any
job left 'queued' or 'running' by a previous process is marked 'interrupted'.
"""

from __future__ import annotations

import os
import platform
import queue
import threading
import time
import traceback
from typing import Callable

from .db import Database, dumps, now
from .media import Cancelled, MediaError, Runner

ACTIVE = ("queued", "running")


def hardware_info() -> dict:
    info = {"os": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "machine": platform.machine()}
    try:
        import psutil
        info["ram_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:
        pass
    try:
        from .speech import cuda_device_count
        info["cuda_devices"] = cuda_device_count()
    except Exception:
        info["cuda_devices"] = 0
    return info


class JobContext:
    def __init__(self, manager: "JobManager", job_id: int, runner: Runner):
        self.manager, self.job_id, self.runner = manager, job_id, runner
        self._last = 0.0

    def progress(self, fraction: float, message: str) -> None:
        self.runner.check()
        t = time.monotonic()
        if t - self._last >= 0.5 or fraction >= 1.0:
            self._last = t
            self.manager.db.update("jobs", self.job_id, {"progress": round(max(0.0, min(1.0, fraction)), 4),
                                                         "message": message})

    def set(self, **values) -> None:
        self.manager.db.update("jobs", self.job_id, values)


class JobManager:
    def __init__(self, db: Database, handlers: dict[str, Callable[[JobContext, dict], dict]]):
        self.db = db
        self.handlers = handlers
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._runners: dict[int, Runner] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="gta6clips-jobs", daemon=True)
        self._thread.start()

    def recover(self) -> int:
        cur = self.db.execute(
            "UPDATE jobs SET status='interrupted', message='Прервано: программа была закрыта во время работы', "
            "finished_at=? WHERE status IN ('queued','running')", (now(),))
        return cur.rowcount

    def submit(self, kind: str, dedupe_key: str, params: dict, source_id: int | None = None,
               candidate_id: int | None = None) -> tuple[int, bool]:
        """Return (job_id, created). An active job with the same key is reused."""
        with self._lock:
            row = self.db.one("SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('queued','running') "
                              "ORDER BY id DESC LIMIT 1", (dedupe_key,))
            if row:
                return int(row["id"]), False
            job_id = self.db.insert("jobs", {
                "kind": kind, "source_id": source_id, "candidate_id": candidate_id, "dedupe_key": dedupe_key,
                "params": dumps(params), "status": "queued", "progress": 0, "message": "В очереди",
                "created_at": now(),
            })
            self._queue.put(job_id)
            return job_id, True

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            row = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if not row or row["status"] not in ACTIVE:
                return False
            runner = self._runners.get(job_id)
            if runner:
                runner.cancel()
            else:  # still queued
                self.db.update("jobs", job_id, {"status": "cancelled", "message": "Отменено", "finished_at": now()})
            return True

    def wait(self, job_id: int, timeout: float = 600) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            row = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row and row["status"] not in ACTIVE:
                return row["status"]
            time.sleep(0.1)
        raise TimeoutError(job_id)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for r in self._runners.values():
                r.cancel()
        self._queue.put(-1)
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id < 0:
                break
            row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not row or row["status"] != "queued":
                continue
            runner = Runner()
            with self._lock:
                self._runners[job_id] = runner
            started = time.monotonic()
            sampler = _PeakMemory(runner)
            self.db.update("jobs", job_id, {"status": "running", "started_at": now(), "message": "Запуск",
                                            "hardware": dumps(hardware_info())})
            ctx = JobContext(self, job_id, runner)
            status, error, result = "done", None, None
            try:
                from .db import loads
                result = self.handlers[row["kind"]](ctx, loads(row["params"], {}))
            except Cancelled:
                status = "cancelled"
            except MediaError as e:
                status, error = "failed", str(e)
            except Exception as e:  # unexpected: keep the trace for troubleshooting
                status, error = "failed", f"Внутренняя ошибка: {type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            finally:
                sampler.stop()
                with self._lock:
                    self._runners.pop(job_id, None)
            msg = {"done": "Готово", "cancelled": "Отменено пользователем", "failed": "Ошибка"}[status]
            if status == "done" and isinstance(result, dict) and result.get("message"):
                msg = result["message"]
            self.db.update("jobs", job_id, {
                "status": status, "error": error, "message": msg, "finished_at": now(),
                "elapsed": round(time.monotonic() - started, 3), "result": dumps(result) if result else None,
                "peak_rss_mb": sampler.peak_mb, "progress": 1.0 if status == "done" else None,
            } if status == "done" else {
                "status": status, "error": error, "message": msg, "finished_at": now(),
                "elapsed": round(time.monotonic() - started, 3), "peak_rss_mb": sampler.peak_mb,
            })


class _PeakMemory:
    """Samples RSS of this process plus the job's ffmpeg children (needs psutil)."""

    def __init__(self, runner: Runner, interval: float = 0.25):
        self.peak_mb: float | None = None
        self._stop = threading.Event()
        try:
            import psutil
        except ImportError:
            return
        self._psutil = psutil
        self._runner = runner
        self._thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    def _loop(self, interval: float) -> None:
        me = self._psutil.Process()
        peak = 0
        while not self._stop.is_set():
            total = 0
            try:
                total += me.memory_info().rss
                for p in self._runner.children():
                    try:
                        total += self._psutil.Process(p.pid).memory_info().rss
                    except (self._psutil.Error, ProcessLookupError):
                        pass
            except self._psutil.Error:
                pass
            peak = max(peak, total)
            self.peak_mb = round(peak / 2**20, 1)
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
