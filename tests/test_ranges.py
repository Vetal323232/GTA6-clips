import random
import unittest

import helpers  # noqa: F401
from gta6clips.ranges import (Range, adjust_boundaries, check_range, fit_around, initial_range, manual_edit,
                              resolve_overlaps)


class RangeInvariantTests(unittest.TestCase):
    def test_reported_overlap_case_stays_within_max_len(self):
        # Same situation as the reported plan_clips regression, through the anchor-aware planner.
        a = Range(*initial_range(20.5, (20, 21), 100, 5, 3, 10), anchor=20.5, score=1.0)
        b = Range(*initial_range(28.5, (28, 29), 100, 5, 3, 10), anchor=28.5, score=2.0)
        out = resolve_overlaps([a, b], 100, 10, 1.0)
        for r in out:
            self.assertLessEqual(r.duration, 10)
            self.assertTrue(check_range(r, 100, 10))
        self.assertEqual(len(out), 2)
        self.assertLessEqual(out[0].end, out[1].start)

    def test_target_min_takes_real_context_only(self):
        self.assertEqual(initial_range(50, (50, 51), 200, 3, 2, 60, 20), (38.6, 58.6))
        s, e = initial_range(2, (2, 3), 200, 3, 2, 60, 20)
        self.assertEqual((s, e), (0.0, 20.0))
        s, e = initial_range(5, (5, 6), 8, 3, 2, 60, 20)  # source is only 8 s: no stretching
        self.assertEqual((s, e), (0.0, 8.0))

    def test_fit_around_keeps_anchor(self):
        s, e = fit_around(50, 0, 100, 100, 20)
        self.assertLessEqual(e - s, 20)
        self.assertTrue(s <= 50 <= e)

    def test_random_plans_satisfy_invariants(self):
        rng = random.Random(42)
        for _ in range(1500):
            duration = rng.uniform(1, 600)
            max_len = rng.uniform(1, 70)
            ranges = []
            for _ in range(rng.randint(0, 30)):
                anchor = rng.uniform(0, duration)
                ext = (max(0, anchor - rng.uniform(0, 20)), min(duration, anchor + rng.uniform(0, 20)))
                s, e = initial_range(anchor, ext, duration, rng.uniform(0, 15), rng.uniform(0, 15), max_len,
                                     rng.choice([0, 20, 80]))
                if e <= s:
                    continue
                r = Range(s, e, anchor, rng.random(), ext)
                speech = sorted((x, x + rng.uniform(0.2, 8)) for x in (rng.uniform(0, duration) for _ in range(5)))
                cuts = [rng.uniform(0, duration) for _ in range(5)]
                r = adjust_boundaries(r, duration, max_len, speech, cuts)
                self.assertTrue(check_range(r, duration, max_len), (r, duration, max_len))
                ranges.append(r)
            out = resolve_overlaps(ranges, duration, max_len, min(1.0, max_len / 2))
            for r in out:
                self.assertTrue(check_range(r, duration, max_len), (r, duration, max_len))
                self.assertGreaterEqual(r.start, 0)
                self.assertLessEqual(r.end, duration + 1e-6)
                self.assertLess(r.start, r.end)
            for x, y in zip(out, out[1:]):
                self.assertLessEqual(x.end, y.start + 1e-6)

    def test_speech_boundary_preferred_over_scene_cut(self):
        r = Range(10, 30, 20, 1)
        speech = [(8, 12)]  # start falls inside speech
        out = adjust_boundaries(r, 100, 60, speech, [9.5])
        self.assertEqual(out.start, 8)  # moved to phrase start, not to the scene cut inside the phrase

    def test_boundary_adjustment_never_exceeds_max_len(self):
        r = Range(10, 20, 15, 1)
        out = adjust_boundaries(r, 100, 10, [(5, 12)], None)
        self.assertLessEqual(out.duration, 10)


class ManualEditTests(unittest.TestCase):
    def test_edit_excluding_anchor_is_kept_but_flagged(self):
        s, e, flags = manual_edit(30, 40, anchor=20, duration=100, max_len=60)
        self.assertEqual((s, e), (30, 40))
        self.assertTrue(any("ключевой момент" in f for f in flags))

    def test_edit_longer_than_max_is_flagged_not_shortened(self):
        s, e, flags = manual_edit(0, 90, anchor=20, duration=100, max_len=60)
        self.assertEqual((s, e), (0, 90))
        self.assertTrue(flags)

    def test_edit_outside_source_rejected(self):
        with self.assertRaises(ValueError):
            manual_edit(50, 120, 60, 100, 60)
        with self.assertRaises(ValueError):
            manual_edit(40, 40, 40, 100, 60)


if __name__ == "__main__":
    unittest.main()
