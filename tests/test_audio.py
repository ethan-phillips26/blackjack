"""
Procedural sound effects.

audio.py has no pygame in it, so the waveforms can be checked on a machine with
no sound card at all -- which is the point, since the cabinet this runs on may
not have working audio either.

The properties worth asserting are the ones that are audible when they break:
a sound that starts or stops mid-cycle pops, a sound that exceeds full scale
clips into a buzz, and a name that no builder knows about is silence where the
machine expected a noise.
"""

import os
import re
import sys
import unittest

import audio


def samples_of(pcm: bytes) -> list[int]:
    """PCM bytes back into signed 16-bit ints."""
    import array

    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":  # pragma: no cover
        values.byteswap()
    return list(values)


class TestWaveforms(unittest.TestCase):
    def test_tone_is_the_length_asked_for(self):
        for ms in (10, 45, 250):
            with self.subTest(ms=ms):
                expected = int(audio.SAMPLE_RATE * ms / 1000)
                self.assertEqual(len(audio.tone(440, ms)), expected)

    def test_tone_stays_within_full_scale(self):
        for shape in ("square", "saw", "triangle", "sine"):
            with self.subTest(shape=shape):
                peak = max(abs(v) for v in audio.tone(440, 60, shape=shape, volume=1.0))
                self.assertLessEqual(peak, 1.0)

    def test_notes_open_and_close_on_silence(self):
        """The envelope exists to stop square waves clicking. A note that
        begins at full amplitude pops, and on a small speaker the pop is
        louder than the note."""
        for shape in ("square", "sine"):
            with self.subTest(shape=shape):
                got = audio.tone(440, 80, shape=shape, volume=1.0)
                self.assertAlmostEqual(got[0], 0.0, places=6)
                self.assertLess(abs(got[-1]), 0.05)

    def test_a_bend_does_not_go_silent(self):
        """Phase is accumulated, not recomputed from absolute time; getting
        that wrong makes a sliding note buzz instead of slide."""
        bent = audio.tone(400, 80, volume=1.0, bend=2.0)
        self.assertGreater(max(abs(v) for v in bent), 0.5)

    def test_noise_is_deterministic(self):
        self.assertEqual(audio.noise(30), audio.noise(30))

    def test_silence_is_silent(self):
        self.assertTrue(all(v == 0.0 for v in audio.silence(20)))


class TestComposition(unittest.TestCase):
    def test_sequence_concatenates(self):
        a, b = audio.tone(440, 20), audio.tone(660, 30)
        self.assertEqual(len(audio.sequence(a, b)), len(a) + len(b))

    def test_layer_is_as_long_as_its_longest_part(self):
        short, long = audio.tone(440, 20), audio.tone(660, 90)
        self.assertEqual(len(audio.layer(short, long)), len(long))

    def test_layer_of_nothing_is_nothing(self):
        self.assertEqual(audio.layer(), [])

    def test_to_pcm_clips_instead_of_wrapping(self):
        """Wrapping would turn a loud sound into a violently distorted one."""
        loud = audio.to_pcm([4.0, -4.0, 0.0])
        self.assertEqual(samples_of(loud), [32767, -32767, 0])

    def test_to_pcm_is_sixteen_bit(self):
        self.assertEqual(len(audio.to_pcm([0.0] * 10)), 20)

    def test_master_volume_scales_everything(self):
        quiet = samples_of(audio.to_pcm([1.0], volume=0.5))
        self.assertEqual(quiet, [16383])


class TestLibrary(unittest.TestCase):
    def test_every_sound_renders(self):
        library = audio.build_library()
        self.assertEqual(set(library), set(audio.BUILDERS))
        for name, pcm in library.items():
            with self.subTest(name=name):
                self.assertGreater(len(pcm), 0, f"{name} rendered to nothing")
                self.assertEqual(len(pcm) % 2, 0, "not whole 16-bit samples")

    def test_no_sound_is_silent_and_none_is_pinned_to_the_rails(self):
        for name, pcm in audio.build_library().items():
            with self.subTest(name=name):
                values = samples_of(pcm)
                peak = max(abs(v) for v in values)
                self.assertGreater(peak, 1000, f"{name} is inaudibly quiet")
                clipped = sum(1 for v in values if abs(v) >= 32767)
                self.assertLess(
                    clipped / len(values), 0.02, f"{name} is clipping badly"
                )

    def test_sounds_are_short_enough_to_keep_up_with_the_game(self):
        """Cards land 125ms apart and coins are dispensed ~3/sec; a sound
        longer than its own repeat interval turns into a drone."""
        limits = {"card": 120, "dispense": 200, "coin": 250, "bet": 120}
        library = audio.build_library()
        for name, limit_ms in limits.items():
            with self.subTest(name=name):
                ms = len(library[name]) / 2 / audio.SAMPLE_RATE * 1000
                self.assertLessEqual(ms, limit_ms)


class TestEveryCallSiteHasASound(unittest.TestCase):
    """A play_sound() typo is silent, which is the hardest kind of bug to
    notice. Check the names the code actually asks for against the ones
    audio.py can actually make."""

    def test_all_requested_names_exist(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        requested = set()
        for filename in ("main.py", "ui.py"):
            with open(os.path.join(root, filename), encoding="utf-8") as handle:
                requested |= set(re.findall(r'play_sound\(\s*"([a-z_]+)"', handle.read()))
        self.assertTrue(requested, "found no play_sound call sites to check")
        self.assertEqual(requested - set(audio.BUILDERS), set())

    def test_the_result_sounds_are_real_too(self):
        """_result_sound() picks a name at runtime, so the literal scan above
        cannot see it -- and a typo there would be silence on every win."""
        from game import Outcome
        from main import App

        for outcome in Outcome:
            with self.subTest(outcome=outcome.name):
                self.assertIn(App._result_sound(outcome), audio.BUILDERS)

    def test_every_outcome_gets_a_distinct_enough_sound(self):
        from game import Outcome
        from main import App

        self.assertEqual(App._result_sound(Outcome.PLAYER_BLACKJACK), "blackjack")
        self.assertEqual(App._result_sound(Outcome.PUSH), "push")
        self.assertEqual(App._result_sound(Outcome.DEALER_BUST), "win")
        self.assertEqual(App._result_sound(Outcome.PLAYER_BUST), "lose")


class TestNoGuiDependencies(unittest.TestCase):
    def test_audio_is_pygame_free(self):
        """ui.py owns the mixer; this module only makes numbers."""
        self.assertNotIn("pygame", sys.modules)


if __name__ == "__main__":
    unittest.main()
