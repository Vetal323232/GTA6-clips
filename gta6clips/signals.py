"""Streaming signal extraction from a source file.

Every signal is computed per analysis window (default 0.5 s). Decoding is
streamed from ffmpeg in fixed-size blocks, so memory use does not depend on
the length of the recording except for the small per-window arrays
(a few floats per window: ~7200 windows per hour).

A signal that could not be computed is recorded as unavailable with a reason;
it is never filled with made-up values.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .media import MediaError, Runner, ffmpeg_error_text

SAMPLE_RATE = 16000
BLOCK_SECONDS = 60.0
SILENCE_DB = -90.0

Progress = Callable[[float, str], None]


@dataclass
class SignalStatus:
    available: bool
    reason: str = ""
    info: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason, **({"info": self.info} if self.info else {})}


@dataclass
class Features:
    window: float
    duration: float
    rms_db: np.ndarray | None = None              # loudness per window, dBFS
    speech_segments: list[tuple[float, float]] | None = None  # VAD, seconds
    scene: np.ndarray | None = None               # max scene-change score per window (0..1)
    status: dict[str, SignalStatus] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return max(1, int(np.ceil(self.duration / self.window)))

    def to_json(self) -> dict:
        return {
            "window": self.window,
            "duration": self.duration,
            "rms_db": None if self.rms_db is None else [round(float(x), 2) for x in self.rms_db],
            "speech_segments": self.speech_segments,
            "scene": None if self.scene is None else [round(float(x), 4) for x in self.scene],
            "status": {k: v.as_dict() for k, v in self.status.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Features":
        f = cls(window=data["window"], duration=data["duration"])
        if data.get("rms_db") is not None:
            f.rms_db = np.asarray(data["rms_db"], dtype=np.float32)
        if data.get("speech_segments") is not None:
            f.speech_segments = [tuple(s) for s in data["speech_segments"]]
        if data.get("scene") is not None:
            f.scene = np.asarray(data["scene"], dtype=np.float32)
        f.status = {k: SignalStatus(v["available"], v.get("reason", ""), v.get("info", {}))
                    for k, v in data.get("status", {}).items()}
        return f


def vad_runtime_available() -> bool:
    return _load_vad()[0] is not None


def _load_vad():
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: F401
    except Exception as e:  # ImportError or onnxruntime problems
        return None, f"VAD недоступен: пакет faster-whisper не установлен или не загрузился ({type(e).__name__})"
    return (get_speech_timestamps, VadOptions), ""


def audio_pass(
    path: str,
    duration: float,
    runner: Runner,
    window: float = 0.5,
    use_vad: bool = True,
    progress: Progress | None = None,
) -> tuple[np.ndarray, list[tuple[float, float]] | None, SignalStatus]:
    """Decode audio once; return per-window dBFS, VAD speech segments and VAD status."""
    vad, vad_reason = _load_vad() if use_vad else (None, "VAD отключён в настройках")
    win_samples = int(round(SAMPLE_RATE * window))
    block_windows = max(1, int(BLOCK_SECONDS / window))
    block_bytes = block_windows * win_samples * 2
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0", "-vn",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    errfile = tempfile.TemporaryFile()
    proc = runner.popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
    levels: list[np.ndarray] = []
    speech: list[tuple[float, float]] | None = [] if vad else None
    offset = 0.0
    stderr_tail = b""
    try:
        assert proc.stdout is not None
        while True:
            data = _read_exact(proc.stdout, block_bytes)
            runner.check()
            if not data:
                break
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
            n_full = len(samples) // win_samples
            pieces = samples[: n_full * win_samples].astype(np.float32).reshape(n_full, win_samples) / 32768.0
            rms = np.sqrt(np.mean(pieces * pieces, axis=1)) if n_full else np.empty(0, np.float32)
            rest = samples[n_full * win_samples:]
            if len(rest):
                r = rest.astype(np.float32) / 32768.0
                rms = np.append(rms, np.sqrt(np.mean(r * r)))
            with np.errstate(divide="ignore"):
                db = np.where(rms > 0, 20 * np.log10(np.maximum(rms, 1e-12)), SILENCE_DB)
            levels.append(db.astype(np.float32))
            if vad is not None and speech is not None:
                get_ts, opts = vad
                audio = samples.astype(np.float32) / 32768.0
                for seg in get_ts(audio, opts(min_silence_duration_ms=300), sampling_rate=SAMPLE_RATE):
                    s, e = offset + seg["start"] / SAMPLE_RATE, offset + seg["end"] / SAMPLE_RATE
                    if speech and s - speech[-1][1] < 0.05:
                        speech[-1] = (speech[-1][0], round(e, 3))
                    else:
                        speech.append((round(s, 3), round(e, 3)))
            offset += len(samples) / SAMPLE_RATE
            if progress:
                progress(min(1.0, offset / duration), f"Звук: {offset / 60:.1f} из {duration / 60:.1f} мин")
        code = proc.wait()
        errfile.seek(0)
        stderr_tail = errfile.read()[-4000:]
    finally:
        errfile.close()
        runner.release(proc)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
    runner.check()
    if code != 0:
        raise MediaError("ffmpeg не смог декодировать звук.\n" + ffmpeg_error_text(stderr_tail))
    rms_db = np.concatenate(levels) if levels else np.empty(0, np.float32)
    if speech is not None:
        status = SignalStatus(True, info={"segments": len(speech)})
    else:  # retryable = requested but the runtime was missing; fixed by installing it
        status = SignalStatus(False, vad_reason, {"retryable": True} if use_vad else {})
    return rms_db, speech, status


SCENE_RE = re.compile(rb"lavfi\.scene_score=([0-9.]+)")
PTS_RE = re.compile(rb"pts_time:([0-9.]+)")


def visual_pass(
    path: str,
    duration: float,
    runner: Runner,
    window: float = 0.5,
    fps: float = 2.0,
    progress: Progress | None = None,
) -> np.ndarray:
    """Scene-change score per window from a low-frame-rate, low-resolution decode."""
    n = max(1, int(np.ceil(duration / window)))
    scores = np.zeros(n, dtype=np.float32)
    vf = f"fps={fps},scale=160:-2,select='gte(scene,0)',metadata=mode=print:file=-"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-an", "-sn",
           "-vf", vf, "-f", "null", "-"]
    errfile = tempfile.TemporaryFile()
    proc = runner.popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
    t = 0.0
    last_report = 0.0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = PTS_RE.search(line)
            if m:
                t = float(m.group(1))
                continue
            m = SCENE_RE.search(line)
            if m:
                i = min(n - 1, int(t / window))
                scores[i] = max(scores[i], float(m.group(1)))
                if progress and t - last_report >= 30:
                    last_report = t
                    runner.check()
                    progress(min(1.0, t / duration), f"Изображение: {t / 60:.1f} из {duration / 60:.1f} мин")
        code = proc.wait()
        errfile.seek(0)
        err = errfile.read()[-4000:]
    finally:
        errfile.close()
        runner.release(proc)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
    runner.check()
    if code != 0:
        raise MediaError("ffmpeg не смог проанализировать изображение.\n" + ffmpeg_error_text(err))
    return scores


def _read_exact(stream, size: int) -> bytes:
    chunks, got = [], 0
    while got < size:
        chunk = stream.read(size - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)
