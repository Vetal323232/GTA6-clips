"""Speech plumbing with a stand-in model, cue matching, evaluation and burned-in subtitles.

Actual speech recognition quality is NOT tested here (no model download is possible in CI).
"""

import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from helpers import HAVE_FFMPEG, TempDir, make_app
from fixtures import make_bursts

from gta6clips import speech
from gta6clips.evaluation import covered, evaluate
from gta6clips.media import Runner
from gta6clips.scoring import find_cues
from gta6clips.speech import Segment


class FakeInfo:
    language = "ru"
    language_probability = 0.9


class FakeModel:
    instances = []

    def __init__(self, name, device, compute_type, **kw):
        if device == "cuda" and FakeModel.fail_cuda:
            raise RuntimeError("CUDA driver missing")
        self.device, self.compute_type = device, compute_type
        FakeModel.instances.append(self)

    def transcribe(self, audio, language=None, **kw):
        seg = types.SimpleNamespace(start=1.0, end=2.5, text=" вот это да ", avg_logprob=-0.3, no_speech_prob=0.1)
        return iter([seg]), FakeInfo()


FakeModel.fail_cuda = True


def fake_module():
    m = types.ModuleType("faster_whisper")
    m.WhisperModel = FakeModel
    return m


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class SpeechPlumbingTests(unittest.TestCase):
    def test_chunk_offsets_language_and_cpu_default(self):
        with TempDir() as tmp:
            src = tmp / "речь.mp4"
            make_bursts(str(src), 25, [5])
            with mock.patch.dict(sys.modules, {"faster_whisper": fake_module()}):
                t = speech.transcribe(str(src), 25.0, {"model": "small", "device": "cpu", "compute_type": "int8",
                                                       "chunk_seconds": 10}, tmp / "models", Runner())
        self.assertTrue(t.available)
        self.assertEqual((t.device, t.compute_type), ("cpu", "int8"))
        self.assertEqual([s.start for s in t.segments], [1.0, 11.0, 21.0])  # offsets per 10 s chunk
        self.assertEqual(t.language, "ru")
        self.assertTrue(any("кусками" in n for n in t.notes))

    def test_gpu_failure_falls_back_to_cpu(self):
        notes = []
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_module()}), \
                mock.patch.object(speech, "cuda_device_count", return_value=1):
            model, device, ctype = speech.load_model({"model": "small", "device": "auto", "compute_type": "int8"},
                                                     Path("/tmp"), notes)
        self.assertEqual(device, "cpu")
        self.assertTrue(any("GPU не инициализировался" in n for n in notes))

    def test_no_cuda_device_uses_cpu(self):
        notes = []
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_module()}), \
                mock.patch.object(speech, "cuda_device_count", return_value=0):
            _, device, _ = speech.load_model({"model": "small", "device": "cuda"}, Path("/tmp"), notes)
        self.assertEqual(device, "cpu")
        self.assertTrue(notes)

    def test_missing_model_is_reported_not_fatal(self):
        class Broken(FakeModel):
            def __init__(self, *a, **k):
                raise OSError("download blocked")
        m = types.ModuleType("faster_whisper")
        m.WhisperModel = Broken
        with mock.patch.dict(sys.modules, {"faster_whisper": m}):
            t = speech.transcribe("/nonexistent.mp4", 10, {"model": "small"}, Path("/tmp"), Runner())
        self.assertFalse(t.available)
        self.assertIn("не загрузилась", t.reason)


class CueAndEvaluationTests(unittest.TestCase):
    def test_cues_ru_en_and_yo(self):
        segs = [Segment(10, 12, "Ну ничего себе!"), Segment(20, 21, "Oh my God, no way"), Segment(30, 31, "всё ок"),
                Segment(40, 41, "погонЯ началась")]
        cues = find_cues(segs, {"ru": ["ничего себе", "погоня", "все ок"], "en": ["no way", "way"]})
        found = sorted((c.time, c.phrase) for c in cues)
        self.assertIn((10, "ничего себе"), found)
        self.assertIn((20, "no way"), found)
        self.assertIn((30, "все ок"), found)  # ё/е insensitive
        self.assertIn((40, "погоня"), found)

    def test_game_override(self):
        from gta6clips.config import load_config
        cfg = load_config(overrides={"cues": {"game_overrides": {"GTA VI": {"ru": ["розыск"]}}}})
        self.assertEqual(cfg.cues_for("GTA VI")["ru"], ["розыск"])
        self.assertTrue(cfg.cues_for("Другая игра")["ru"])

    def test_evaluation_counts_and_missed_marks(self):
        cands = [
            {"rank": 1, "start": 10, "end": 30, "status": "accepted", "auto_start": 10, "auto_end": 30},
            {"rank": 2, "start": 50, "end": 70, "status": "rejected", "reason": "скучно", "auto_start": 50, "auto_end": 70},
            {"rank": 3, "start": 90, "end": 100, "status": "pending", "auto_start": 90, "auto_end": 100},
        ]
        marks = [{"start": 12, "end": 20}, {"start": 200, "end": 210, "note": "погоня"}, {"start": 29, "end": 40}]
        r = evaluate(cands, marks, [])
        self.assertEqual((r["reviewed"], r["accepted"], r["rejected"], r["pending"]), (2, 1, 1, 1))
        self.assertEqual(r["top_k"][1], {"k": 3, "candidates": 3, "reviewed": 2, "accepted": 1})
        self.assertEqual([m["start"] for m in r["marks_missed"]], [200, 29])
        self.assertTrue(covered({"start": 30.5, "end": 31}, cands[0]))  # within 1 s tolerance


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class BurnedSubtitleTests(unittest.TestCase):
    def test_srt_written_and_subtitles_burned(self):
        with TempDir() as tmp:
            src = tmp / "голос и взрыв.mp4"
            make_bursts(str(src), 20, [8])
            app = make_app(tmp)
            try:
                sid = app.import_path(str(src))[0]["id"]
                jid, _ = app.start_analysis(sid)
                app.jobs.wait(jid, 120)
                # explicit timestamped cues stand in for a transcript
                tid = app.db.insert("transcripts", {"source_id": sid, "fingerprint": app.source(sid)["fingerprint"],
                                                    "cache_key": "test", "model": "fixture", "status": "done",
                                                    "created_at": 0})
                for s, e, text in [(5.0, 7.5, "Привет, это тест"), (7.8, 9.0, "Hello Ёлка"), (12.0, 14.0, "вне клипа")]:
                    app.db.insert("transcript_segments", {"transcript_id": tid, "start": s, "end": e, "text": text})
                c = app.candidates(app.latest_analysis(sid)["id"])[0]
                app.edit_candidate(c["id"], 6.0, 10.0)
                cues = app.clip_subtitles(c["id"])
                self.assertEqual([(x.start, x.end, x.text) for x in cues],
                                 [(0.0, 1.5, "Привет, это тест"), (1.8, 3.0, "Hello Ёлка")])
                outs = {}
                for burn in (False, True):
                    app.set_layout(c["id"], "fit_blur", None, burn)
                    jid, _ = app.start_export([c["id"]])
                    self.assertEqual(app.jobs.wait(jid, 300), "done",
                                     app.db.one("SELECT error FROM jobs WHERE id=?", (jid,))["error"])
                    res = json.loads(app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])["exports"][0]
                    outs[burn] = res
                srt = Path(outs[True]["srt"]).read_text(encoding="utf-8")
                self.assertIn("00:00:00,000 --> 00:00:01,500\nПривет, это тест", srt)
                self.assertNotIn("вне клипа", srt)
                frames = []
                for burn in (False, True):
                    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.7", "-i", outs[burn]["path"], "-frames:v", "1",
                                          "-vf", "crop=1080:500:0:1420,scale=108:50,format=gray", "-f", "rawvideo", "-"],
                                         capture_output=True, check=True).stdout
                    frames.append(raw)
                diff = sum(abs(a - b) for a, b in zip(*frames)) / len(frames[0])
                self.assertGreater(diff, 2.0, "burned subtitles not visible in the lower area")
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
