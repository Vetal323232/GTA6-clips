import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gta6clips.app import App  # noqa: E402
from gta6clips.config import load_config  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_app(tmp: Path, **overrides) -> App:
    ov = {"general": {"data_dir": str(tmp / "data"), "output_dir": str(tmp / "экспорт клипов")},
          "speech": {"enabled": False},
          "export": {"preset": "ultrafast", "crf": 30}}
    for section, values in overrides.items():
        ov.setdefault(section, {}).update(values)
    return App(load_config(overrides=ov))


class TempDir:
    def __enter__(self) -> Path:
        self._td = tempfile.TemporaryDirectory(prefix="gta6 тест ")
        return Path(self._td.name)

    def __exit__(self, *exc):
        self._td.cleanup()
