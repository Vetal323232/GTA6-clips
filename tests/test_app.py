"""Technical end-to-end checks on synthetic media (not evidence of highlight quality)."""

import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path

from helpers import HAVE_FFMPEG, TempDir, make_app
from fixtures import make_bursts, make_silent, make_video_only

from gta6clips.db import SCHEMA_VERSION, Database
from gta6clips.media import MediaError, probe


def ffprobe_streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = TempDir()
        cls.tmp = cls.td.__enter__()
        cls.media = cls.tmp / "Записи GTA VI"  # spaces + Cyrillic
        cls.media.mkdir()
        cls.bursts = cls.media / "сессия с взрывами.mp4"
        make_bursts(str(cls.bursts), 40, [10, 28], scene_cut=27.0)

    @classmethod
    def tearDownClass(cls):
        cls.td.__exit__(None, None, None)

    def setUp(self):
        self.case = Path(self.tmp / f"case_{self._testMethodName}")
        self.case.mkdir()
        self.app = make_app(self.case)

    def tearDown(self):
        self.app.close()

    def analyze(self, sid, **ov):
        jid, _ = self.app.start_analysis(sid, ov)
        status = self.app.jobs.wait(jid, 120)
        job = self.app.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        return status, job

    # --- import and identity -------------------------------------------------
    def test_import_dedupe_and_cyrillic_paths(self):
        r1 = self.app.import_path(str(self.bursts))
        r2 = self.app.import_path(str(self.bursts))
        self.assertEqual(r1[0]["status"], "new")
        self.assertEqual(r2[0]["status"], "existing")
        self.assertEqual(self.app.db.one("SELECT COUNT(*) c FROM sources")["c"], 1)
        # folder import: same file again plus a corrupted one, which is reported but doesn't stop the import
        (self.media / "битый файл.mp4").write_bytes(b"\x00garbage" * 500)
        try:
            res = self.app.import_path(str(self.media))
        finally:
            (self.media / "битый файл.mp4").unlink()
        statuses = sorted(r["status"] for r in res)
        self.assertEqual(statuses, ["error", "existing"])
        err = next(r for r in res if r["status"] == "error")["error"]
        self.assertIn("повреждён", err)

    def test_moved_copy_keeps_identity(self):
        self.app.import_path(str(self.bursts))
        moved = self.case / "другое имя.mp4"
        shutil.copy(self.bursts, moved)
        r = self.app.import_path(str(moved))
        self.assertEqual(r[0]["status"], "existing")

    def test_corrupted_file_is_a_clear_error(self):
        bad = self.case / "повреждённый.mkv"
        bad.write_bytes(os.urandom(5000))
        with self.assertRaises(MediaError) as cm:
            self.app.import_file(str(bad))
        self.assertIn("повреждён", str(cm.exception))

    # --- analysis, cache, invalidation ---------------------------------------
    def test_analysis_candidates_respect_invariants_and_cache(self):
        sid = self.app.import_path(str(self.bursts))[0]["id"]
        status, job = self.analyze(sid, max_len=12, before=6, after=4)
        self.assertEqual(status, "done", job["error"])
        an = self.app.latest_analysis(sid)
        cands = self.app.candidates(an["id"])
        self.assertEqual(len(cands), 2)
        anchors = sorted(c["anchor"] for c in cands)
        self.assertAlmostEqual(anchors[0], 10.5, delta=1.0)
        self.assertAlmostEqual(anchors[1], 28.5, delta=1.0)
        for c in cands:
            self.assertTrue(0 <= c["start"] < c["end"] <= 40.0 + 1e-6)
            self.assertLessEqual(c["end"] - c["start"], 12 + 1e-6)
            self.assertTrue(c["start"] <= c["anchor"] <= c["end"])
            contrib = json.loads(c["contributions"])
            self.assertTrue(contrib["loud_global"]["available"])
            self.assertFalse(contrib["cue"]["available"])  # speech disabled → unavailable, not zero
            self.assertIn("Ключевой момент", c["explanation"])
        signals = json.loads(an["signals"])
        self.assertTrue(signals["visual"]["available"])
        self.assertFalse(signals["speech"]["available"])
        # same settings again → cached analysis, no duplicate candidates
        status, job = self.analyze(sid, max_len=12, before=6, after=4)
        self.assertIn("сохранённый", job["message"])
        self.assertEqual(self.app.db.one("SELECT COUNT(*) c FROM analyses")["c"], 1)
        # different scoring parameter → new analysis, but decoded features come from cache
        status, job = self.analyze(sid, max_len=12, before=6, after=5)
        self.assertEqual(self.app.db.one("SELECT COUNT(*) c FROM analyses")["c"], 2)
        self.assertIn("cache", json.loads(job["signals"]))

    def test_modified_media_invalidates_analysis(self):
        src = self.case / "изменяемый.mp4"
        make_bursts(str(src), 20, [8])
        sid = self.app.import_path(str(src))[0]["id"]
        self.analyze(sid)
        fp1 = self.app.source(sid)["fingerprint"]
        make_bursts(str(src), 20, [4, 14])  # overwrite in place with different content
        os.utime(src, (time.time() + 5, time.time() + 5))
        status, job = self.analyze(sid)
        self.assertEqual(status, "done")
        self.assertNotIn("сохранённый", job["message"])
        fp2 = self.app.source(sid)["fingerprint"]
        self.assertNotEqual(fp1, fp2)
        self.assertEqual(self.app.latest_analysis(sid)["fingerprint"], fp2)
        cands = self.app.candidates(self.app.latest_analysis(sid)["id"])
        # new bursts at 4 s and 14 s (old one was at 8 s) are both covered by the new candidates
        for t in (4.5, 14.5):
            self.assertTrue(any(c["start"] <= t <= c["end"] for c in cands), (t, [dict(c) for c in cands]))

    def test_silence_gives_no_candidates_without_error(self):
        src = self.case / "тишина.mp4"
        make_silent(str(src))
        sid = self.app.import_path(str(src))[0]["id"]
        status, job = self.analyze(sid)
        self.assertEqual(status, "done", job["error"])
        self.assertIn("нормальный результат", job["message"])

    def test_missing_source_reported(self):
        src = self.case / "исчезнет.mp4"
        make_silent(str(src), 5)
        sid = self.app.import_path(str(src))[0]["id"]
        src.unlink()
        status, job = self.analyze(sid)
        self.assertEqual(status, "failed")
        self.assertIn("недоступен", job["error"])

    # --- export ----------------------------------------------------------------
    def test_export_validation_skip_and_forced(self):
        sid = self.app.import_path(str(self.bursts))[0]["id"]
        self.analyze(sid, max_len=12, before=6, after=4)
        c = self.app.candidates(self.app.latest_analysis(sid)["id"])[0]
        self.app.edit_candidate(c["id"], 8.3, 12.1)  # 3.8 s
        jid, _ = self.app.start_export([c["id"]])
        self.assertEqual(self.app.jobs.wait(jid, 300), "done",
                         self.app.db.one("SELECT error FROM jobs WHERE id=?", (jid,))["error"])
        res = json.loads(self.app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])
        out = Path(res["exports"][0]["path"])
        self.assertTrue(out.is_file())
        self.assertIn("экспорт клипов", str(out))
        info = ffprobe_streams(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["codec_name"], v["width"], v["height"]), ("h264", 1080, 1920))
        self.assertEqual(a["codec_name"], "aac")
        self.assertAlmostEqual(float(info["format"]["duration"]), 3.8, delta=0.2)
        decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:v:0", "-map", "0:a:0?",
                                 "-f", "null", "-"], capture_output=True, text=True)
        self.assertEqual((decode.returncode, decode.stderr.strip()), (0, ""))
        self.assertFalse(list(out.parent.glob("*.part.mp4")))
        mtime = out.stat().st_mtime
        # identical settings → skipped
        jid, _ = self.app.start_export([c["id"]])
        self.app.jobs.wait(jid, 300)
        res2 = json.loads(self.app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])
        self.assertTrue(res2["exports"][0]["skipped"])
        # forced → new file name, old file untouched
        jid, _ = self.app.start_export([c["id"]], force=True)
        self.assertEqual(self.app.jobs.wait(jid, 300), "done")
        res3 = json.loads(self.app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])
        self.assertNotEqual(res3["exports"][0]["path"], str(out))
        self.assertEqual(out.stat().st_mtime, mtime)
        # changed layout → different export identity
        self.app.set_layout(c["id"], "crop_blur", {"x": 0.25, "y": 0, "w": 0.5, "h": 1}, False)
        key_before = res["exports"][0]["path"]
        jid, _ = self.app.start_export([c["id"]])
        self.app.jobs.wait(jid, 300)
        res4 = json.loads(self.app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])
        self.assertFalse(res4["exports"][0]["skipped"])
        self.assertNotEqual(res4["exports"][0]["path"], key_before)

    def test_video_only_export_gets_silent_aac(self):
        src = self.case / "без звука.mp4"
        make_video_only(str(src), 12)
        sid = self.app.import_path(str(src))[0]["id"]
        status, job = self.analyze(sid)
        self.assertEqual(status, "done", job["error"])
        self.assertIn("нормальный результат", job["message"])
        signals = json.loads(job["signals"])
        self.assertFalse(signals["audio"]["available"])
        # no automatic candidates: create a manual one through the DB API used by the UI
        an = self.app.latest_analysis(sid)
        cid = self.app.db.insert("candidates", {"analysis_id": an["id"], "source_id": sid, "rank": 1, "start": 2.0,
                                                "end": 5.0, "anchor": 3.0, "auto_start": 2.0, "auto_end": 5.0,
                                                "score": 0, "contributions": "{}", "explanation": "вручную"})
        jid, _ = self.app.start_export([cid])
        self.assertEqual(self.app.jobs.wait(jid, 300), "done",
                         self.app.db.one("SELECT error FROM jobs WHERE id=?", (jid,))["error"])
        path = json.loads(self.app.db.one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])["exports"][0]["path"]
        info = probe(path)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.acodec, "aac")
        self.assertAlmostEqual(info.duration, 3.0, delta=0.2)

    def test_export_limit_per_run(self):
        with self.assertRaises(MediaError):
            self.app.start_export(list(range(1, 8)))

    # --- jobs ------------------------------------------------------------------
    def test_cancel_and_rerun(self):
        src = self.case / "длинная.mp4"
        make_bursts(str(src), 300, [50, 200], size="320x180", rate=25)
        sid = self.app.import_path(str(src))[0]["id"]
        jid, created = self.app.start_analysis(sid, {"visual_fps": 25.0})
        jid2, created2 = self.app.start_analysis(sid, {"visual_fps": 25.0})
        self.assertEqual((jid, created2), (jid2, False))  # refresh/double submit doesn't duplicate
        for _ in range(200):
            if self.app.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "running":
                break
            time.sleep(0.05)
        time.sleep(0.5)
        self.assertTrue(self.app.jobs.cancel(jid))
        self.assertEqual(self.app.jobs.wait(jid, 60), "cancelled")
        time.sleep(0.5)
        ffmpeg_left = subprocess.run(["pgrep", "-f", str(src)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(ffmpeg_left, "", "ffmpeg child still running after cancel")
        self.assertEqual(self.app.db.one("SELECT COUNT(*) c FROM analyses")["c"], 0)  # nothing half-written
        jid3, created3 = self.app.start_analysis(sid, {"visual_fps": 25.0})
        self.assertTrue(created3)
        self.assertEqual(self.app.jobs.wait(jid3, 300), "done")

    def test_interrupted_jobs_marked_on_restart(self):
        db = self.app.db
        jid = db.insert("jobs", {"kind": "analyze", "dedupe_key": "x", "params": "{}", "status": "running",
                                 "created_at": time.time()})
        self.app.close()
        self.app = make_app(self.case)
        self.assertEqual(self.app.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"], "interrupted")

    def test_hud_extension_point_adds_evidence(self):
        src = self.case / "hud.mp4"
        make_silent(str(src), 30)
        sid = self.app.import_path(str(src))[0]["id"]
        self.app.cfg["hud"].update({"enabled": True, "plugin": "hud_dummy:detect"})
        status, job = self.analyze(sid)
        self.assertEqual(status, "done", job["error"])
        cands = self.app.candidates(self.app.latest_analysis(sid)["id"])
        self.assertEqual(len(cands), 1)
        self.assertIn("HUD", cands[0]["explanation"])

    def test_schema_version(self):
        self.assertEqual(Database(self.case / "x.sqlite3").schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
