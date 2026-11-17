"""Configuration: built-in defaults overridden by an optional TOML file.

Lookup order: the path passed with --config, then GTA6CLIPS_CONFIG, then
./config.toml. Unknown keys are ignored; missing keys use the defaults below.
See config.example.toml for a commented copy.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None

DEFAULT_FONT = "Arial" if os.name == "nt" else "DejaVu Sans"

DEFAULTS: dict[str, Any] = {
    "general": {
        "game": "GTA VI",
        "channels": [],
        "data_dir": "gta6clips-data",
        "output_dir": "exports",
        "host": "127.0.0.1",
        "port": 8765,
    },
    "analysis": {
        "window": 0.5,
        "sensitivity": 2.0,
        "rolling_window": 30.0,
        "rolling_threshold": 3.0,
        "onset_db": 9.0,
        "merge_gap": 3.0,
        "before": 12.0,
        "after": 8.0,
        "target_min": 20.0,
        "max_len": 60.0,
        "candidates_per_source": 10,
        "min_clip": 3.0,
        "visual": True,
        "visual_fps": 2.0,
        "vad": True,
        "speech_boundaries": True,
        "scene_boundaries": True,
    },
    "speech": {
        "enabled": True,
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "language": "",
        "chunk_seconds": 300,
        "model_dir": "",
        "local_files_only": False,
    },
    "cues": {
        "ru": ["вот это да", "офигеть", "ничего себе", "погоня", "взрыв", "красава", "ахаха",
                "не может быть", "что это было", "жесть", "клип", "смотри"],
        "en": ["oh my god", "no way", "what the", "let's go", "insane", "clip that", "holy",
                "wow", "explosion", "chase"],
        "game_overrides": {},
    },
    "weights": {
        "loud_global": 1.0,
        "loud_rolling": 1.0,
        "onset": 0.7,
        "speech": 0.3,
        "cue": 1.5,
        "visual": 0.5,
        "hud": 1.0,
    },
    "hud": {
        "enabled": False,
        "plugin": "",
    },
    "export": {
        "width": 1080,
        "height": 1920,
        "layout": "fit_blur",
        "max_clips_per_run": 5,
        "burn_subtitles": False,
        "write_srt": True,
        "font": DEFAULT_FONT,
        "font_size": 16,
        "crf": 20,
        "preset": "veryfast",
        "loudness_lufs": -14.0,
        "true_peak_db": -1.5,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """Nested dict with a `path` attribute and helper accessors."""

    path: Path | None = None

    @property
    def data_dir(self) -> Path:
        return Path(self["general"]["data_dir"]).expanduser().resolve()

    @property
    def output_dir(self) -> Path:
        return Path(self["general"]["output_dir"]).expanduser().resolve()

    def cues_for(self, game: str | None) -> dict[str, list[str]]:
        cues = {"ru": list(self["cues"].get("ru", [])), "en": list(self["cues"].get("en", []))}
        override = self["cues"].get("game_overrides", {}).get(game or "", {})
        for lang, words in override.items():
            cues[lang] = list(words)
        return cues


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    candidate = path or os.environ.get("GTA6CLIPS_CONFIG") or ("config.toml" if Path("config.toml").is_file() else None)
    data: dict = {}
    if candidate:
        p = Path(candidate)
        if not p.is_file():
            raise FileNotFoundError(f"Файл настроек не найден: {p}")
        if tomllib is None:
            raise RuntimeError("Для чтения config.toml нужен Python 3.11 или новее")
        with open(p, "rb") as f:
            try:
                data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise RuntimeError(f"Ошибка в файле настроек {p}: {e}") from None
    cfg = Config(_merge(DEFAULTS, data))
    if overrides:
        cfg = Config(_merge(cfg, overrides))
    cfg.path = Path(candidate).resolve() if candidate else None
    return cfg
