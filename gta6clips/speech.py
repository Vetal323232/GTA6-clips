"""Optional local speech recognition with faster-whisper (CTranslate2).

Default: multilingual "small" model on CPU with int8 weights. A GPU is used
only when device = "auto" or "cuda" *and* CTranslate2 reports a CUDA device
*and* the model actually loads there; otherwise we fall back to CPU and say so.

Models are downloaded by faster-whisper from Hugging Face on first use into
`<data_dir>/models` (or speech.model_dir). Nothing here calls a paid or cloud
inference API.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .media import MediaError, Runner, ffmpeg_error_text

SAMPLE_RATE = 16000
ENGINE_VERSION = "fw1"

# Approximate download sizes of the CTranslate2 conversions used by faster-whisper.
# These are estimates (rounded), not measured in this project.
ESTIMATED_MODEL_SIZE = {
    "tiny": "≈75 МБ (оценка)",
    "base": "≈145 МБ (оценка)",
    "small": "≈480 МБ (оценка)",
    "medium": "≈1,5 ГБ (оценка)",
}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass
class Transcript:
    available: bool
    reason: str = ""
    model: str = ""
    device: str = ""
    compute_type: str = ""
    language: str | None = None
    language_probability: float | None = None
    segments: list[Segment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def runtime_available() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except Exception as e:
        return False, f"пакет faster-whisper не установлен ({type(e).__name__}). Установите requirements-speech.txt"
    return True, ""


def cuda_device_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def load_model(cfg: dict, models_dir: Path, notes: list[str]):
    """Load WhisperModel honoring the device policy; returns (model, device, compute_type)."""
    from faster_whisper import WhisperModel

    name = cfg.get("model", "small")
    device_pref = (cfg.get("device") or "cpu").lower()
    compute_type = cfg.get("compute_type") or "int8"
    root = Path(cfg.get("model_dir") or models_dir)
    root.mkdir(parents=True, exist_ok=True)
    kwargs = dict(download_root=str(root), local_files_only=bool(cfg.get("local_files_only")))

    if device_pref in ("auto", "cuda"):
        count = cuda_device_count()
        if count > 0:
            gpu_type = compute_type if compute_type not in ("int8",) else "int8_float16"
            try:
                model = WhisperModel(name, device="cuda", compute_type=gpu_type, **kwargs)
                return model, "cuda", gpu_type
            except Exception as e:
                notes.append(f"GPU не инициализировался ({type(e).__name__}: {e}); используется CPU")
        else:
            notes.append("CUDA-устройство не найдено CTranslate2; используется CPU")
    cpu_type = compute_type if compute_type in ("int8", "int8_float32", "float32", "int16") else "int8"
    model = WhisperModel(name, device="cpu", compute_type=cpu_type, **kwargs)
    return model, "cpu", cpu_type


def transcribe(
    path: str,
    duration: float,
    cfg: dict,
    models_dir: Path,
    runner: Runner,
    progress: Callable[[float, str], None] | None = None,
) -> Transcript:
    """Transcribe in fixed-size chunks streamed from ffmpeg (bounded memory)."""
    ok, reason = runtime_available()
    if not ok:
        return Transcript(False, reason)
    notes: list[str] = []
    try:
        model, device, compute_type = load_model(cfg, models_dir, notes)
    except Exception as e:
        size = ESTIMATED_MODEL_SIZE.get(cfg.get("model", ""), "размер неизвестен")
        return Transcript(False, f"модель «{cfg.get('model')}» не загрузилась ({type(e).__name__}: {str(e)[:300]}). "
                                 f"Нужна однократная загрузка {size} из Hugging Face", notes=notes)
    result = Transcript(True, model=cfg.get("model", ""), device=device, compute_type=compute_type, notes=notes)
    language = (cfg.get("language") or "").strip() or None
    chunk = int(float(cfg.get("chunk_seconds", 300)) * SAMPLE_RATE)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0", "-vn",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    errfile = tempfile.TemporaryFile()
    proc = runner.popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
    offset = 0.0
    lang_votes: dict[str, float] = {}
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(chunk * 4)
            runner.check()
            if not data:
                break
            audio = np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4")
            segments, info = model.transcribe(audio, language=language, vad_filter=True, beam_size=5)
            for seg in segments:
                runner.check()
                text = seg.text.strip()
                if text:
                    result.segments.append(Segment(round(offset + seg.start, 3), round(offset + seg.end, 3), text,
                                                   seg.avg_logprob, seg.no_speech_prob))
            if info.language:
                lang_votes[info.language] = lang_votes.get(info.language, 0) + info.language_probability * len(audio)
            offset += len(audio) / SAMPLE_RATE
            if progress:
                progress(min(1.0, offset / duration), f"Речь: {offset / 60:.1f} из {duration / 60:.1f} мин")
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
    if code != 0:
        raise MediaError("ffmpeg не смог декодировать звук для распознавания.\n" + ffmpeg_error_text(err))
    if language:
        result.language, result.language_probability = language, None
    elif lang_votes:
        best = max(lang_votes, key=lang_votes.get)
        result.language = best
        result.language_probability = round(lang_votes[best] / max(1, offset * SAMPLE_RATE), 3)
    if chunk and offset > cfg.get("chunk_seconds", 300):
        result.notes.append(f"Звук распознавался кусками по {cfg.get('chunk_seconds', 300)} с; "
                            "слово на стыке кусков может потеряться")
    return result
