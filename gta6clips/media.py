"""ffprobe/ffmpeg helpers, source fingerprints and cancellable subprocesses."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv"}
FINGERPRINT_CHUNK = 4 * 1024 * 1024

# Hide console windows for child processes on Windows.
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaError(RuntimeError):
    """A user-facing (Russian) error about a media file or tool."""


class Cancelled(Exception):
    """The job was cancelled by the user."""


def check_tools() -> list[str]:
    """Return the names of missing required tools."""
    return [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]


@dataclass
class ProbeInfo:
    duration: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "N/A"):
        return None
    num, _, den = rate.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: str | os.PathLike) -> ProbeInfo:
    """Read duration and streams with ffprobe. Raises MediaError on bad input."""
    path = str(path)
    if not Path(path).is_file():
        raise MediaError(f"Файл не найден: {path}")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_CREATIONFLAGS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"код {result.returncode}"
        raise MediaError(f"Не удалось прочитать файл {path}. Он повреждён или формат не поддерживается.\n{detail}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise MediaError(f"ffprobe вернул некорректный ответ для {path}") from None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = None
    for value in (data.get("format", {}).get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            duration = float(value)
            break
        except (TypeError, ValueError):
            continue
    if duration is None or duration <= 0:
        raise MediaError(f"Не удалось определить длительность файла {path}")
    if video is None and audio is None:
        raise MediaError(f"В файле {path} нет ни видео, ни звука")
    return ProbeInfo(
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=(video or {}).get("width"),
        height=(video or {}).get("height"),
        fps=_parse_rate((video or {}).get("avg_frame_rate")) or _parse_rate((video or {}).get("r_frame_rate")),
        vcodec=(video or {}).get("codec_name"),
        acodec=(audio or {}).get("codec_name"),
        raw=data,
    )


def fingerprint(path: str | os.PathLike) -> str:
    """Content identity of a media file: size + SHA-256 of head, middle and tail.

    Independent of the file name and location, so a moved or renamed file keeps
    its identity, while any re-encode or edit changes it.
    """
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(f"v1:{size}:".encode())
    with open(p, "rb") as f:
        offsets = [0]
        if size > FINGERPRINT_CHUNK:
            offsets += [max(0, size // 2 - FINGERPRINT_CHUNK // 2), max(0, size - FINGERPRINT_CHUNK)]
        for off in offsets:
            f.seek(off)
            h.update(f.read(FINGERPRINT_CHUNK))
    return h.hexdigest()


def find_videos(folder: str | os.PathLike, recursive: bool = True) -> list[Path]:
    root = Path(folder)
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in root.glob(pattern) if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)


class Runner:
    """Starts child processes for one job and kills them all on cancel()."""

    def __init__(self):
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    def popen(self, cmd: list[str], **kwargs) -> subprocess.Popen:
        self.check()
        kwargs.setdefault("creationflags", _CREATIONFLAGS)
        proc = subprocess.Popen(cmd, **kwargs)
        with self._lock:
            self._procs.append(proc)
        if self.cancelled.is_set():
            self._kill(proc)
            raise Cancelled()
        return proc

    def release(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def run(self, cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run to completion capturing stderr; raise Cancelled if cancelled."""
        proc = self.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=timeout)
        finally:
            self.release(proc)
        self.check()
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def children(self) -> list[subprocess.Popen]:
        with self._lock:
            return list(self._procs)

    def cancel(self) -> None:
        self.cancelled.set()
        for proc in self.children():
            self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass


def friendly_os_error(e: OSError, what: str) -> MediaError:
    if e.errno == errno.ENOSPC:
        return MediaError(f"Недостаточно места на диске: {what}")
    if e.errno in (errno.EACCES, errno.EPERM):
        return MediaError(f"Нет доступа: {what}")
    return MediaError(f"Ошибка файловой системы ({e.strerror}): {what}")


def ffmpeg_error_text(stderr: bytes | str | None) -> str:
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else (stderr or "")
    text = text.strip()
    if "No space left on device" in text:
        return "Недостаточно места на диске.\n" + text[-800:]
    return text[-1500:]
