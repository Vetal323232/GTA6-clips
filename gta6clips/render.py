"""Vertical clip rendering and output validation with ffmpeg.

Default layout "fit_blur": the whole gameplay frame is scaled to fit the
1080x1920 canvas over a blurred, zoomed copy of itself, so nothing (HUD,
minimap) is cropped away. Cropping happens only when the user picks a crop
layout and adjusts the crop rectangle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .media import MediaError, Runner, ffmpeg_error_text, friendly_os_error, probe

RENDER_VERSION = "r1"
LAYOUTS = {
    "fit_blur": "Весь кадр на размытом фоне (по умолчанию)",
    "crop_blur": "Выбранная область на размытом фоне",
    "crop_fill": "Выбранная область на весь экран",
}
FULL_CROP = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def normalize_crop(crop: dict | None) -> dict:
    c = dict(FULL_CROP)
    if crop:
        for k in c:
            try:
                c[k] = float(crop.get(k, c[k]))
            except (TypeError, ValueError):
                pass
    c["w"] = min(1.0, max(0.05, c["w"]))
    c["h"] = min(1.0, max(0.05, c["h"]))
    c["x"] = min(1.0 - c["w"], max(0.0, c["x"]))
    c["y"] = min(1.0 - c["h"], max(0.0, c["y"]))
    return {k: round(v, 4) for k, v in c.items()}


def video_filter(layout: str, crop: dict | None, width: int, height: int, subtitles: str | None = None,
                 font: str = "Arial", font_size: int = 11) -> str:
    c = normalize_crop(crop)
    crop_expr = f"crop=iw*{c['w']}:ih*{c['h']}:iw*{c['x']}:ih*{c['y']}"
    bg = (f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
          f"gblur=sigma=30,eq=brightness=-0.08[bgb]")
    if layout == "crop_fill":
        graph = (f"[0:v]{crop_expr},scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height},setsar=1[base]")
    else:
        fg_crop = crop_expr + "," if layout == "crop_blur" else ""
        graph = (f"[0:v]split=2[bg][fg];{bg};[fg]{fg_crop}scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
                 f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    if subtitles:
        style = (f"FontName={font},FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 f"BorderStyle=1,Outline=2,Shadow=0,MarginV=45,MarginL=20,MarginR=20,Alignment=2")
        graph += f";[base]subtitles={subtitles}:charenc=UTF-8:force_style='{style}'[sub];[sub]format=yuv420p[v]"
    else:
        graph += ";[base]format=yuv420p[v]"
    return graph


@dataclass
class RenderResult:
    path: Path
    duration: float
    width: int
    height: int
    vcodec: str
    acodec: str


def render_clip(src: str, start: float, end: float, out_path: Path, runner: Runner, *, has_audio: bool,
                export_cfg: dict, layout: str = "fit_blur", crop: dict | None = None,
                srt_text: str | None = None, src_fps: float | None = None) -> RenderResult:
    """Render to a temporary file, validate it, then move it to out_path (never overwrites)."""
    if out_path.exists():
        raise MediaError(f"Файл уже существует, перезапись запрещена: {out_path}")
    width, height = int(export_cfg["width"]), int(export_cfg["height"])
    duration = end - start
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="gta6clips-render-"))
    part = out_path.with_name(out_path.stem + ".part.mp4")
    try:
        sub_name = None
        if srt_text:
            (tmp_dir / "subs.srt").write_text(srt_text, encoding="utf-8")
            sub_name = "subs.srt"  # relative to cwd=tmp_dir: avoids filtergraph path quoting (C:\...)
        graph = video_filter(layout, crop, width, height, sub_name, export_cfg.get("font", "Arial"),
                             int(export_cfg.get("font_size", 11)))
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
               "-i", os.path.abspath(src)]
        if has_audio:
            af = (f"loudnorm=I={export_cfg['loudness_lufs']}:TP={export_cfg['true_peak_db']}:LRA=11,"
                  f"aresample=48000")
            cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0", "-af", af]
        else:
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0"]
        cmd += ["-c:v", "libx264", "-preset", str(export_cfg.get("preset", "veryfast")),
                "-crf", str(export_cfg.get("crf", 20)), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-t", f"{duration:.3f}",
                "-movflags", "+faststart", str(part.resolve())]
        proc = runner.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(tmp_dir))
        try:
            _, err = proc.communicate()
        finally:
            runner.release(proc)
        runner.check()
        if proc.returncode != 0:
            raise MediaError("ffmpeg не смог отрисовать клип.\n" + ffmpeg_error_text(err))
        result = validate_output(part, duration, width, height, runner, src_fps)
        if out_path.exists():
            raise MediaError(f"Файл появился во время экспорта, перезапись запрещена: {out_path}")
        os.replace(part, out_path)
        result.path = out_path
        return result
    except OSError as e:
        raise friendly_os_error(e, str(out_path)) from None
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def validate_output(path: Path, expected: float, width: int, height: int, runner: Runner,
                    src_fps: float | None = None) -> RenderResult:
    info = probe(path)
    streams = info.raw.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    problems = []
    if not v or v.get("codec_name") != "h264":
        problems.append(f"видео не H.264 ({v and v.get('codec_name')})")
    if not a or a.get("codec_name") != "aac":
        problems.append(f"звук не AAC ({a and a.get('codec_name')})")
    if v and (v.get("width"), v.get("height")) != (width, height):
        problems.append(f"размер {v.get('width')}x{v.get('height')} вместо {width}x{height}")
    tolerance = 0.2 + (1.0 / src_fps if src_fps else 0.0)
    if abs(info.duration - expected) > tolerance:
        problems.append(f"длительность {info.duration:.3f} с вместо {expected:.3f} с")
    if problems:
        raise MediaError("Экспорт не прошёл проверку: " + "; ".join(problems))
    res = runner.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                      "-f", "null", "-"])
    if res.returncode != 0 or (res.stderr or b"").strip():
        raise MediaError("Экспорт не декодируется полностью: " + ffmpeg_error_text(res.stderr))
    return RenderResult(path, info.duration, width, height, "h264", "aac")


def preview_frame(src: str, t: float, layout: str, crop: dict | None, export_cfg: dict, runner: Runner,
                  scale: float = 1 / 3) -> bytes:
    w, h = int(export_cfg["width"] * scale) // 2 * 2, int(export_cfg["height"] * scale) // 2 * 2
    graph = video_filter(layout, crop, w, h)
    res = runner.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", os.path.abspath(src),
                      "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-f", "image2pipe",
                      "-vcodec", "mjpeg", "-q:v", "4", "-"], timeout=60)
    if res.returncode != 0 or not res.stdout:
        raise MediaError("Не удалось построить превью кадра.\n" + ffmpeg_error_text(res.stderr))
    return res.stdout


def describe(result: RenderResult) -> str:
    return json.dumps({"duration": result.duration, "width": result.width, "height": result.height})
