"""Tests for the Remote Script's pure logic.

Everything here runs without Ableton: `_Framework` is stubbed out so the module
imports, and only the functions that do not touch Live's object model are
exercised. That covers the parts most likely to be wrong — envelope step
generation, note validation and the wire framing.

Run with: python3 remote_script/test_remote_script.py
"""

import os
import sys
import types
import unittest

# Stub the Live-only import so the script can be loaded outside Ableton.
framework = types.ModuleType("_Framework")
control_surface = types.ModuleType("_Framework.ControlSurface")


class _ControlSurface(object):
    def __init__(self, *args, **kwargs):
        pass


control_surface.ControlSurface = _ControlSurface
framework.ControlSurface = control_surface
sys.modules.setdefault("_Framework", framework)
sys.modules.setdefault("_Framework.ControlSurface", control_surface)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Crableton"))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "abletonmcp",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Crableton", "__init__.py"),
)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

AbletonMCP = mcp.AbletonMCP


class FakeParameter(object):
    def __init__(self, minimum=0.0, maximum=1.0):
        self.min = minimum
        self.max = maximum


class TakeCommandTests(unittest.TestCase):
    """The socket framing: newline-delimited, but tolerant of bare JSON."""

    def test_reads_one_command_and_keeps_the_rest(self):
        command, rest = AbletonMCP._take_command('{"type":"a"}\n{"type":"b"}\n')
        self.assertEqual(command["type"], "a")
        command, rest = AbletonMCP._take_command(rest)
        self.assertEqual(command["type"], "b")
        self.assertEqual(AbletonMCP._take_command(rest), (None, ""))

    def test_waits_for_an_incomplete_command(self):
        partial = '{"type":"set_tem'
        command, rest = AbletonMCP._take_command(partial)
        self.assertIsNone(command)
        self.assertEqual(rest, partial)

    def test_accepts_undelimited_commands(self):
        command, rest = AbletonMCP._take_command('{"type":"a"}{"type":"b"}')
        self.assertEqual(command["type"], "a")
        command, _ = AbletonMCP._take_command(rest)
        self.assertEqual(command["type"], "b")


class EnumMappingTests(unittest.TestCase):
    def test_maps_friendly_names_to_live_indices(self):
        self.assertEqual(mcp._index_of(mcp.LAUNCH_QUANTIZATION, "1 bar", "q"), 4)
        self.assertEqual(mcp._index_of(mcp.LAUNCH_QUANTIZATION, "NONE", "q"), 0)
        self.assertEqual(mcp._name_of(mcp.LAUNCH_QUANTIZATION, 4), "1 bar")

    def test_rejects_an_unknown_name_with_the_valid_ones(self):
        with self.assertRaises(ValueError) as caught:
            mcp._index_of(mcp.MONITORING_STATE, "sometimes", "monitoring state")
        self.assertIn("in, auto, off", str(caught.exception))

    def test_name_of_tolerates_an_out_of_range_index(self):
        self.assertIsNone(mcp._name_of(mcp.LAUNCH_MODE, 99))


class NoteValidationTests(unittest.TestCase):
    def test_fills_in_defaults(self):
        notes = AbletonMCP._validate_notes([{"pitch": 60, "start_time": 0, "duration": 1}])
        self.assertEqual(notes[0]["velocity"], 100)
        self.assertFalse(notes[0]["mute"])
        self.assertIsNone(notes[0]["probability"])

    def test_rejects_notes_live_would_refuse(self):
        for bad, expected in (
            ({"pitch": 128, "start_time": 0, "duration": 1}, "0-127"),
            ({"pitch": 60, "start_time": 0, "duration": 0}, "duration"),
            ({"pitch": 60, "start_time": -1, "duration": 1}, "before the clip"),
            ({"pitch": 60, "duration": 1}, "start_time"),
        ):
            with self.assertRaises(ValueError) as caught:
                AbletonMCP._validate_notes([bad])
            self.assertIn(expected, str(caught.exception))

    def test_clamps_velocity_and_probability(self):
        notes = AbletonMCP._validate_notes([
            {"pitch": 60, "start_time": 0, "duration": 1,
             "velocity": 900, "probability": 5.0},
        ])
        self.assertEqual(notes[0]["velocity"], 127.0)
        self.assertEqual(notes[0]["probability"], 1.0)


class EnvelopePointTests(unittest.TestCase):
    def test_clamps_values_to_the_parameter_range_and_sorts(self):
        parameter = FakeParameter(0.0, 1.0)
        points = AbletonMCP._validated_points([[4, 2.0], [0, -1.0]], parameter)
        self.assertEqual(points, [[0.0, 0.0], [4.0, 1.0]])

    def test_rejects_empty_and_malformed_points(self):
        parameter = FakeParameter()
        with self.assertRaises(ValueError):
            AbletonMCP._validated_points([], parameter)
        with self.assertRaises(ValueError):
            AbletonMCP._validated_points([[0]], parameter)
        with self.assertRaises(ValueError):
            AbletonMCP._validated_points([[-1, 0.5]], parameter)


class EnvelopeStepTests(unittest.TestCase):
    """Live only writes steps, so ramps are approximated. Check the shape."""

    def test_a_single_point_holds_across_the_clip(self):
        steps = AbletonMCP._envelope_steps([[0.0, 0.5]], "linear", 0.25, 16.0)
        self.assertEqual(steps, [(0.0, 16.0, 0.5)])

    def test_step_interpolation_holds_each_value(self):
        steps = AbletonMCP._envelope_steps(
            [[0.0, 0.0], [4.0, 1.0]], "step", 0.25, 4.0)
        self.assertEqual(steps, [(0.0, 4.0, 0.0)])

    def test_linear_interpolation_covers_the_span_without_gaps(self):
        steps = AbletonMCP._envelope_steps(
            [[0.0, 0.0], [4.0, 1.0]], "linear", 0.5, 4.0)
        self.assertEqual(len(steps), 8)

        # Contiguous: each step starts where the previous one ended.
        for previous, following in zip(steps, steps[1:]):
            self.assertAlmostEqual(previous[0] + previous[1], following[0], places=9)
        self.assertAlmostEqual(steps[0][0], 0.0)
        self.assertAlmostEqual(steps[-1][0] + steps[-1][1], 4.0, places=9)

        # Monotonic, and straddling the ideal line rather than lagging it.
        values = [value for _, _, value in steps]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[0], 0.0)
        self.assertLess(values[-1], 1.0)
        self.assertAlmostEqual(values[0], 1.0 - values[-1], places=9)

    def test_equal_endpoints_collapse_to_one_step(self):
        steps = AbletonMCP._envelope_steps(
            [[0.0, 0.7], [8.0, 0.7]], "linear", 0.0625, 8.0)
        self.assertEqual(steps, [(0.0, 8.0, 0.7)])

    def test_the_last_value_is_held_to_the_end_of_the_clip(self):
        steps = AbletonMCP._envelope_steps(
            [[0.0, 0.0], [4.0, 1.0]], "linear", 1.0, 16.0)
        self.assertAlmostEqual(steps[-1][0], 4.0)
        self.assertAlmostEqual(steps[-1][1], 12.0)
        self.assertAlmostEqual(steps[-1][2], 1.0)

    def test_multi_segment_shapes_keep_their_corners(self):
        # The filter sweep shape: rise, hold, rise, fall.
        points = [[0.0, 0.25], [16.0, 0.60], [32.0, 0.60], [48.0, 0.75], [56.0, 0.30]]
        steps = AbletonMCP._envelope_steps(points, "linear", 1.0, 56.0)

        self.assertAlmostEqual(steps[0][0], 0.0)
        self.assertAlmostEqual(steps[-1][0] + steps[-1][1], 56.0, places=9)
        for previous, following in zip(steps, steps[1:]):
            self.assertAlmostEqual(previous[0] + previous[1], following[0], places=9)

        # The hold between beats 16 and 32 is one flat step, not 16 of them.
        holds = [s for s in steps if abs(s[0] - 16.0) < 1e-9]
        self.assertEqual(len(holds), 1)
        self.assertAlmostEqual(holds[0][1], 16.0)
        self.assertAlmostEqual(holds[0][2], 0.60)

    def test_zero_length_segments_are_skipped(self):
        steps = AbletonMCP._envelope_steps(
            [[4.0, 0.1], [4.0, 0.9], [8.0, 0.9]], "linear", 1.0, 8.0)
        self.assertTrue(all(length > 0 for _, length, _ in steps))


class NoteWindowTests(unittest.TestCase):
    def test_defaults_to_the_whole_clip(self):
        from_pitch, pitch_span, from_time, time_span = AbletonMCP._note_window({})
        self.assertEqual((from_pitch, pitch_span, from_time), (0, 128, 0.0))
        self.assertGreater(time_span, 100000)

    def test_a_pitch_window_spans_to_the_top_by_default(self):
        from_pitch, pitch_span, _, _ = AbletonMCP._note_window({"from_pitch": 60})
        self.assertEqual((from_pitch, pitch_span), (60, 68))

    def test_explicit_windows_are_passed_through(self):
        window = AbletonMCP._note_window(
            {"from_time": 4, "time_span": 4, "from_pitch": 36, "pitch_span": 1})
        self.assertEqual(window, (36, 1, 4.0, 4.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
