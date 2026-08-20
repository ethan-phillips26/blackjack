"""
Timing helpers behind the animations.

anim.py has no pygame in it, so all of this runs on a bare Python install
alongside the rules and money tests -- and a tween that overruns, sticks, or
lands short is caught here rather than by squinting at a CRT.
"""

import sys
import unittest

import anim
from anim import RollingCounter, Tween


class TestEasing(unittest.TestCase):
    CURVES = (
        anim.linear,
        anim.ease_in_quad,
        anim.ease_out_quad,
        anim.ease_in_cubic,
        anim.ease_out_cubic,
        anim.ease_in_out_cubic,
        anim.ease_out_back,
        anim.ease_out_bounce,
    )

    def test_every_curve_starts_at_zero_and_lands_on_one(self):
        for curve in self.CURVES:
            with self.subTest(curve=curve.__name__):
                self.assertAlmostEqual(curve(0.0), 0.0, places=6)
                self.assertAlmostEqual(curve(1.0), 1.0, places=6)

    def test_ease_out_back_overshoots_and_comes_home(self):
        """The spring in the result banner. If it stops overshooting the pop
        is gone; if it never returns to 1.0 the text ends up the wrong size."""
        self.assertGreater(max(anim.ease_out_back(t / 100) for t in range(101)), 1.0)
        self.assertAlmostEqual(anim.ease_out_back(1.0), 1.0, places=6)

    def test_clamp_and_lerp(self):
        self.assertEqual(anim.clamp01(-3.0), 0.0)
        self.assertEqual(anim.clamp01(7.0), 1.0)
        self.assertEqual(anim.lerp(10.0, 20.0, 0.25), 12.5)


class TestTween(unittest.TestCase):
    def test_start_in_the_future_is_the_delay(self):
        """This is how the deal stagger works -- no queue, no callbacks."""
        t = Tween(start_ms=1000, duration_ms=200, ease=anim.linear)
        self.assertEqual(t.value(0), 0.0)
        self.assertEqual(t.value(999), 0.0)
        self.assertFalse(t.started(999))
        self.assertEqual(t.value(1100), 0.5)
        self.assertTrue(t.done(1200))

    def test_value_never_leaves_the_ramp(self):
        t = Tween(0, 100, anim.linear)
        self.assertEqual(t.value(-500), 0.0)
        self.assertEqual(t.value(5000), 1.0)

    def test_zero_duration_snaps(self):
        """config.ANIMATIONS_ENABLED = False collapses every duration to 0;
        the machine must then behave exactly as it did before there was any
        animation at all."""
        t = Tween(500, 0)
        self.assertEqual(t.value(499), 0.0)
        self.assertEqual(t.value(500), 1.0)
        self.assertTrue(t.done(500))

    def test_at_interpolates_between_endpoints(self):
        t = Tween(0, 100, anim.linear)
        self.assertEqual(t.at(50, 100.0, 200.0), 150.0)
        self.assertEqual(t.at(100, 100.0, 200.0), 200.0)


class TestCyclicHelpers(unittest.TestCase):
    def test_pulse_stays_in_range(self):
        for ms in range(0, 3000, 17):
            self.assertGreaterEqual(anim.pulse(ms, 900), 0.0)
            self.assertLessEqual(anim.pulse(ms, 900), 1.0)

    def test_blink_duty_cycle(self):
        self.assertTrue(anim.blink(0, 1000, 0.5))
        self.assertTrue(anim.blink(499, 1000, 0.5))
        self.assertFalse(anim.blink(500, 1000, 0.5))
        self.assertTrue(anim.blink(1000, 1000, 0.5))

    def test_zero_period_never_divides_by_zero(self):
        self.assertEqual(anim.wave(1234, 0), 0.0)
        self.assertTrue(anim.blink(1234, 0))

    def test_shake_decays_to_nothing(self):
        self.assertEqual(anim.decaying_shake(0, 0, 400, 10.0), 0.0)
        self.assertNotEqual(anim.decaying_shake(120, 0, 400, 10.0), 0.0)
        self.assertEqual(anim.decaying_shake(400, 0, 400, 10.0), 0.0)
        self.assertEqual(anim.decaying_shake(9999, 0, 400, 10.0), 0.0)

    def test_shake_stays_within_amplitude(self):
        for ms in range(0, 400, 3):
            self.assertLessEqual(abs(anim.decaying_shake(ms, 0, 400, 10.0)), 10.0)

    def test_mix_color_endpoints(self):
        black, white = (0, 0, 0), (240, 240, 240)
        self.assertEqual(anim.mix_color(black, white, 0.0), black)
        self.assertEqual(anim.mix_color(black, white, 1.0), white)
        self.assertEqual(anim.mix_color(black, white, 0.5), (120, 120, 120))


class TestRollingCounter(unittest.TestCase):
    """The CREDITS meter. It is a DISPLAY of bank.py's integer quarters and
    must always come to rest on exactly that number -- a meter that settles a
    quarter short is a machine that looks like it ate someone's money."""

    def test_rolls_and_lands_exactly(self):
        c = RollingCounter(value=0, duration_ms=200)
        c.set(4, now_ms=1000)
        # ease_out_cubic front-loads the movement, so check early: the point is
        # that it passes THROUGH the intermediate values rather than jumping.
        self.assertTrue(c.rolling(1020))
        self.assertLess(c.display(1020), 4)
        self.assertEqual(c.display(1200), 4)
        self.assertFalse(c.rolling(1200))

    def test_retarget_mid_roll_starts_from_where_it_is(self):
        c = RollingCounter(value=0, duration_ms=200)
        c.set(10, now_ms=0)
        midway = c.current(100)
        c.set(2, now_ms=100)
        self.assertAlmostEqual(c.current(100), midway, places=6)
        self.assertEqual(c.display(300), 2)

    def test_snap_skips_the_roll(self):
        """Used at boot: never run a recovered balance up from zero."""
        c = RollingCounter(value=0, duration_ms=200)
        c.snap(9)
        self.assertEqual(c.display(0), 9)
        self.assertFalse(c.rolling(0))

    def test_setting_the_same_value_is_a_no_op(self):
        c = RollingCounter(value=3, duration_ms=200)
        c.set(3, now_ms=500)
        self.assertFalse(c.rolling(500))


class TestNoGuiDependencies(unittest.TestCase):
    def test_anim_is_pygame_free(self):
        """anim.py is imported by ui.py but must not depend on it, so the
        timing can be tested without a display attached."""
        self.assertNotIn("pygame", sys.modules)


if __name__ == "__main__":
    unittest.main()
