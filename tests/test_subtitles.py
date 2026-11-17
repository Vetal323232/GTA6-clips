import random
import unittest

import helpers  # noqa: F401
from gta6clips.subtitles import Cue, clip_cues, parse_srt, srt_time, to_srt

TOL = 0.05


class SubtitleTests(unittest.TestCase):
    cues = [Cue(1.0, 4.0, "до клипа"), Cue(8.0, 12.0, "пересекает начало"), Cue(13.0, 15.5, "внутри"),
            Cue(18.0, 25.0, "crosses the end"), Cue(30.0, 32.0, "после клипа")]

    def test_clip_and_rebase(self):
        out = clip_cues(self.cues, 10.0, 20.0)
        self.assertEqual([c.text for c in out], ["пересекает начало", "внутри", "crosses the end"])
        expected = [(0.0, 2.0), (3.0, 5.5), (8.0, 10.0)]
        for c, (s, e) in zip(out, expected):
            self.assertAlmostEqual(c.start, s, delta=TOL)
            self.assertAlmostEqual(c.end, e, delta=TOL)

    def test_cue_spanning_whole_clip(self):
        out = clip_cues([Cue(0, 100, "длинная")], 10, 20)
        self.assertEqual((out[0].start, out[0].end), (0.0, 10.0))

    def test_touching_cues_are_dropped(self):
        self.assertEqual(clip_cues([Cue(5, 10, "a"), Cue(20, 25, "b")], 10, 20), [])

    def test_boundary_edit_regenerates_timing(self):
        a = clip_cues(self.cues, 10, 20)
        b = clip_cues(self.cues, 12, 26)
        self.assertNotEqual([(c.start, c.end) for c in a], [(c.start, c.end) for c in b])
        self.assertAlmostEqual(b[0].start, 1.0, delta=TOL)  # cue 8–12 only touches 12 → dropped
        self.assertAlmostEqual(b[-1].end, 13.0, delta=TOL)

    def test_random_invariants_and_srt_roundtrip(self):
        rng = random.Random(7)
        for _ in range(300):
            cues = []
            for _ in range(rng.randint(0, 20)):
                s = rng.uniform(0, 200)
                cues.append(Cue(s, s + rng.uniform(0.01, 10), rng.choice(["привет", "go go", "вот это да", "x"])))
            start = rng.uniform(0, 150)
            end = start + rng.uniform(0.5, 60)
            out = clip_cues(cues, start, end)
            length = end - start
            for c in out:
                self.assertTrue(0 <= c.start < c.end <= length + 1e-3, (c, length))
            self.assertEqual([c.start for c in out], sorted(c.start for c in out))
            back = parse_srt(to_srt(out))
            self.assertEqual(len(back), len(out))
            for x, y in zip(out, back):
                self.assertAlmostEqual(x.start, y.start, delta=TOL)
                self.assertAlmostEqual(x.end, y.end, delta=TOL)
                self.assertEqual(x.text, y.text)

    def test_srt_syntax(self):
        text = to_srt([Cue(0, 1.5, "один"), Cue(61.25, 3725.0, "два")])
        self.assertEqual(text, "1\n00:00:00,000 --> 00:00:01,500\nодин\n\n2\n00:01:01,250 --> 01:02:05,000\nдва\n")
        self.assertEqual(srt_time(0.0004), "00:00:00,000")


if __name__ == "__main__":
    unittest.main()
