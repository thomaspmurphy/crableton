"""Tests for the Remote Script's pure logic.

Everything here runs without Ableton: `_Framework` is stubbed out so the module
imports, and only the functions that do not touch Live's object model are
exercised, plus the note-editing handlers against fake clips that mimic
Live 11's extended note API and the older tuple API.

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


class _MidiNoteSpecification(object):
    def __init__(self, **fields):
        self.__dict__.update(fields)


live = types.ModuleType("Live")
live_clip = types.ModuleType("Live.Clip")
live_clip.MidiNoteSpecification = _MidiNoteSpecification
live.Clip = live_clip
sys.modules.setdefault("Live", live)
sys.modules.setdefault("Live.Clip", live_clip)

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


class FakeNote(object):
    def __init__(self, note_id, pitch, start_time, duration=1.0, velocity=100.0):
        self.note_id = note_id
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = False
        self.probability = 1.0
        self.velocity_deviation = 0.0
        self.release_velocity = 64.0


class ExtendedClip(object):
    """Live 11+: notes carry ids and are edited through a mutable vector."""

    name = "Riff"
    is_midi_clip = True
    is_arrangement_clip = False
    looping = True
    loop_start = 0.0
    loop_end = 4.0
    end_marker = 4.0
    signature_numerator = 4
    signature_denominator = 4

    def __init__(self, notes):
        self.notes = list(notes)
        self.applied = None
        self.next_id = 100

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n for n in self.notes
                if from_pitch <= n.pitch < from_pitch + pitch_span
                and from_time <= n.start_time < from_time + time_span]

    def apply_note_modifications(self, vector):
        self.applied = list(vector)

    def add_new_notes(self, specifications):
        for spec in specifications:
            self.notes.append(FakeNote(self.next_id, spec.pitch, spec.start_time,
                                       spec.duration, spec.velocity))
            self.next_id += 1

    def remove_notes_by_id(self, ids):
        self.notes = [n for n in self.notes if n.note_id not in ids]


class LegacyClip(object):
    """Live 10: (pitch, start, duration, velocity, mute) tuples, no ids."""

    name = "Old"
    is_midi_clip = True

    def __init__(self, notes):
        self.notes = list(notes)

    def get_notes(self, from_time, from_pitch, time_span, pitch_span):
        return tuple(n for n in self.notes
                     if from_pitch <= n[0] < from_pitch + pitch_span
                     and from_time <= n[1] < from_time + time_span)

    def set_notes(self, notes):
        self.notes = list(notes)


def script_for(clip):
    script = AbletonMCP.__new__(AbletonMCP)
    script._resolve_clip = lambda params: clip
    return script


class TransposeTests(unittest.TestCase):
    def test_shifts_only_the_window_and_keeps_ids(self):
        clip = ExtendedClip([FakeNote(1, 36, 0.0), FakeNote(2, 60, 0.0), FakeNote(3, 36, 4.0)])
        result = script_for(clip)._transpose_notes(
            {"semitones": 12, "from_pitch": 0, "pitch_span": 48, "time_span": 4}, None)
        self.assertEqual(result["transposed"], 1)
        self.assertEqual([n.pitch for n in clip.notes], [48, 60, 36])
        self.assertEqual([n.note_id for n in clip.applied], [1])

    def test_refuses_to_push_notes_out_of_range_and_names_them(self):
        clip = ExtendedClip([FakeNote(1, 120, 0.0), FakeNote(2, 60, 1.0)])
        with self.assertRaises(ValueError) as caught:
            script_for(clip)._transpose_notes({"semitones": 12}, None)
        self.assertIn("pitch 120 at beat 0.0 (id 1)", str(caught.exception))
        self.assertIsNone(clip.applied)
        self.assertEqual(clip.notes[0].pitch, 120)

    def test_legacy_clips_are_rewritten_with_the_window_shifted(self):
        clip = LegacyClip([(60, 0.0, 1.0, 100, False), (40, 0.0, 1.0, 100, False)])
        result = script_for(clip)._transpose_notes({"semitones": -2, "from_pitch": 50}, None)
        self.assertEqual(result["via"], "set_notes")
        self.assertEqual(sorted(n[0] for n in clip.notes), [40, 58])


class DuplicateTests(unittest.TestCase):
    def test_copies_a_bar_forward_and_extends_the_loop_to_the_bar(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0), FakeNote(2, 64, 2.5, duration=1.0)])
        result = script_for(clip)._duplicate_notes(
            {"time_offset": 4, "time_span": 4}, None)
        self.assertEqual(result["copied"], 2)
        self.assertEqual(sorted((n.pitch, n.start_time) for n in clip.notes[2:]),
                         [(60, 4.0), (64, 6.5)])
        self.assertEqual(clip.loop_end, 8.0)
        self.assertEqual(result["extended"], "loop_end")

    def test_can_leave_the_clip_length_alone(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0)])
        result = script_for(clip)._duplicate_notes(
            {"time_offset": 4, "extend_clip": False}, None)
        self.assertEqual(clip.loop_end, 4.0)
        self.assertTrue(result["past_clip_end"])

    def test_transposed_copies_inside_the_clip_leave_its_length(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0)])
        result = script_for(clip)._duplicate_notes(
            {"time_offset": 0, "pitch_offset": 7}, None)
        self.assertEqual(clip.notes[1].pitch, 67)
        self.assertNotIn("extended", result)

    def test_rejects_copies_onto_the_originals_out_of_range_or_before_zero(self):
        clip = ExtendedClip([FakeNote(1, 125, 1.0)])
        for params, expected in (
            ({"time_offset": 0}, "both 0"),
            ({"time_offset": 4, "pitch_offset": 5}, "outside 0-127"),
            ({"time_offset": -2}, "before the clip start"),
        ):
            with self.assertRaises(ValueError) as caught:
                script_for(clip)._duplicate_notes(params, None)
            self.assertIn(expected, str(caught.exception))
        self.assertEqual(len(clip.notes), 1)

    def test_legacy_clips_append_the_copies(self):
        clip = LegacyClip([(60, 0.0, 1.0, 100, False)])
        clip.looping, clip.loop_end = True, 8.0
        script_for(clip)._duplicate_notes({"time_offset": 2}, None)
        self.assertEqual(sorted(n[1] for n in clip.notes), [0.0, 2.0])


class ModifyNotesTests(unittest.TestCase):
    def test_changes_only_the_given_fields(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0), FakeNote(2, 62, 1.0)])
        result = script_for(clip)._modify_notes(
            {"changes": [{"note_id": 2, "velocity": 300, "start_time": 1.5}]}, None)
        note = clip.notes[1]
        self.assertEqual((note.pitch, note.start_time, note.velocity), (62, 1.5, 127.0))
        self.assertEqual(clip.notes[0].velocity, 100.0)
        self.assertIsNotNone(clip.applied)
        self.assertEqual(result["notes"][0]["note_id"], 2)

    def test_unknown_ids_fail_before_anything_changes(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0)])
        with self.assertRaises(ValueError) as caught:
            script_for(clip)._modify_notes(
                {"changes": [{"note_id": 1, "pitch": 61}, {"note_id": 9, "pitch": 1}]}, None)
        self.assertIn("9", str(caught.exception))
        self.assertEqual(clip.notes[0].pitch, 60)
        self.assertIsNone(clip.applied)

    def test_rejects_bad_edits(self):
        for changes, expected in (
            ([], "non-empty"),
            ([{"pitch": 60}], "note_id"),
            ([{"note_id": 1}], "changes nothing"),
            ([{"note_id": 1, "pitch": 128}], "0-127"),
            ([{"note_id": 1, "duration": 0}], "positive"),
            ([{"note_id": 1, "colour": 3}], "unknown field"),
            ([{"note_id": 1, "mute": True}, {"note_id": 1, "pitch": 3}], "twice"),
        ):
            with self.assertRaises(ValueError) as caught:
                AbletonMCP._validated_note_edits(changes)
            self.assertIn(expected, str(caught.exception))

    def test_needs_the_extended_api(self):
        with self.assertRaises(RuntimeError) as caught:
            script_for(LegacyClip([]))._modify_notes(
                {"changes": [{"note_id": 1, "pitch": 60}]}, None)
        self.assertIn("Live 11", str(caught.exception))


class RemoveNotesByIdTests(unittest.TestCase):
    def test_removes_just_those_notes(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0), FakeNote(2, 62, 1.0), FakeNote(3, 64, 2.0)])
        result = script_for(clip)._remove_notes_by_id({"note_ids": [3, 1, 1]}, None)
        self.assertEqual(result["removed"], 2)
        self.assertEqual([n.note_id for n in clip.notes], [2])

    def test_unknown_ids_fail_before_anything_is_deleted(self):
        clip = ExtendedClip([FakeNote(1, 60, 0.0)])
        with self.assertRaises(ValueError):
            script_for(clip)._remove_notes_by_id({"note_ids": [1, 7]}, None)
        self.assertEqual(len(clip.notes), 1)

    def test_needs_the_extended_api(self):
        with self.assertRaises(RuntimeError) as caught:
            script_for(LegacyClip([]))._remove_notes_by_id({"note_ids": [1]}, None)
        self.assertIn("Live 11", str(caught.exception))


class ClipNotesTests(unittest.TestCase):
    def test_get_clip_notes_reports_note_ids_on_the_extended_api(self):
        clip = ExtendedClip([FakeNote(7, 60, 0.0)])
        clip.length = 4.0
        result = script_for(clip)._get_clip_notes({}, None)
        self.assertEqual(result["notes"][0]["note_id"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
