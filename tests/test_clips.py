import io
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gta6clips.cli import main
from gta6clips.cut import plan_clips, video_duration
from gta6clips.detect import Moment, find_moments, read_loudness
from make_sample import make_sample


class PlanTests(unittest.TestCase):
    def test_find_moments_groups_nearby_peaks(self):
        levels = [-40.0] * 100
        levels[20] = levels[22] = -5.0  # 1 s apart -> one moment
        levels[80] = -6.0
        moments = find_moments(levels, window=0.5, merge_gap=3.0)
        self.assertEqual([(m.start, m.end) for m in moments], [(10.0, 11.5), (40.0, 40.5)])

    def test_plan_clips_pads_clamps_and_merges(self):
        moments = [Moment(1, 2, 1, -5), Moment(8, 9, 8, -4), Moment(50, 51, 50, -3)]
        clips = plan_clips(moments, duration=52, before=5, after=3)
        self.assertEqual([(c.start, c.end) for c in clips], [(0.0, 12.0), (45.0, 52.0)])

    def test_top_keeps_loudest(self):
        moments = [Moment(10, 11, 10, -9), Moment(30, 31, 30, -2), Moment(60, 61, 60, -5)]
        clips = plan_clips(moments, duration=100, top=2)
        self.assertEqual([c.start for c in clips], [25.0, 55.0])

    def test_merged_clip_never_exceeds_max_len(self):
        # Regression: these used to merge into one 17 s clip (15-32 s) despite max_len=10.
        moments = [Moment(20, 21, 20, -5), Moment(28, 29, 28, -4)]
        clips = plan_clips(moments, duration=100, max_len=10)
        self.assertEqual([(c.start, c.end) for c in clips], [(15.0, 24.0), (24.0, 32.0)])
        self.assertTrue(all(c.duration <= 10 for c in clips))

    def test_clips_respect_max_len_and_do_not_overlap(self):
        rng = random.Random(1234)
        for _ in range(500):
            duration = rng.uniform(5, 300)
            max_len = rng.uniform(2, 40)
            moments = []
            for _ in range(rng.randint(0, 25)):
                start = rng.uniform(0, duration)
                end = min(duration, start + rng.uniform(0.5, 30))
                moments.append(Moment(start, end, rng.uniform(start, end), rng.uniform(-30, 0)))
            clips = plan_clips(moments, duration, rng.uniform(0, 10), rng.uniform(0, 10), max_len)
            for c in clips:
                self.assertLessEqual(c.duration, max_len + 1e-9)
                self.assertGreaterEqual(c.start, 0)
                self.assertLessEqual(c.end, duration + 1e-9)
            for a, b in zip(clips, clips[1:]):
                self.assertLessEqual(a.end, b.start + 1e-9)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class BadInputTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_corrupted_file_gives_clear_error(self):
        # Regression: ffprobe failure used to raise an unhandled CalledProcessError.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.mp4"
            bad.write_bytes(bytes(random.Random(7).getrandbits(8) for _ in range(4096)))
            code, _, err = self.run_cli(str(bad), "-o", str(Path(tmp) / "clips"))
        self.assertNotEqual(code, 0)
        self.assertIn("ошибка: не удалось прочитать", err)
        self.assertNotIn("Traceback", err)

    def test_empty_file_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "empty.mp4"
            bad.touch()
            code, _, err = self.run_cli(str(bad))
        self.assertNotEqual(code, 0)
        self.assertTrue(err.startswith("ошибка: "))

    def test_video_duration_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.mkv"
            bad.write_text("not a video")
            with self.assertRaises(RuntimeError):
                video_duration(str(bad))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = str(Path(cls.tmp.name) / "sample.mp4")
        make_sample(cls.video)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detects_bursts(self):
        moments = find_moments(read_loudness(self.video))
        self.assertEqual(len(moments), 2)
        self.assertAlmostEqual(moments[0].start, 15, delta=1)
        self.assertAlmostEqual(moments[1].start, 42, delta=1)

    def test_cli_writes_clips(self):
        out = Path(self.tmp.name) / "clips"
        self.assertEqual(main([self.video, "-o", str(out)]), 0)
        files = sorted(out.glob("*.mp4"))
        self.assertEqual(len(files), 2)
        for f in files:
            self.assertAlmostEqual(video_duration(str(f)), 9.0, delta=0.5)  # 5 + 1 + 3


if __name__ == "__main__":
    unittest.main()
