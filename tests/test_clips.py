import shutil
import subprocess
import sys
import tempfile
import unittest
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
