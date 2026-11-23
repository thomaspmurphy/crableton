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


class _EnvelopeEvent(object):
    def __init__(self, time, value):
        self.time = time
        self.value = value


live = types.ModuleType("Live")
live_clip = types.ModuleType("Live.Clip")
live_clip.MidiNoteSpecification = _MidiNoteSpecification
live_clip.Clip = type("Clip", (object,), {"automation_envelope": lambda self, p: None})
live_envelope = types.ModuleType("Live.Envelope")
live_envelope.EnvelopeEvent = _EnvelopeEvent
live.Clip = live_clip
live.Envelope = live_envelope
sys.modules.setdefault("Live", live)
sys.modules.setdefault("Live.Clip", live_clip)
sys.modules.setdefault("Live.Envelope", live_envelope)

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



# ── A small fake Live set, for handlers that resolve their own clips ─────────


class FakeEnvelope(object):
    def __init__(self, parameter, events=()):
        self.parameter = parameter
        self.events = list(events)

    def events_in_range(self, start, end):
        return [types.SimpleNamespace(time=t, value=v)
                for t, v in self.events if start <= t <= end]

    def delete_events_in_range(self, start, end):
        self.events = [(t, v) for t, v in self.events if not start <= t <= end]

    def value_at_time(self, time_value):
        """Linear between events, in parameter space; at exactly 0 Live
        reports the parameter's static value instead."""
        if time_value == 0:
            return -99.0
        events = sorted(self.events)
        if time_value <= events[0][0]:
            return events[0][1]
        for (t0, v0), (t1, v1) in zip(events, events[1:]):
            if t0 <= time_value <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (time_value - t0) / (t1 - t0)
        return events[-1][1]

    def create_event(self, event):
        """Live 12's signature: one EnvelopeEvent, never (time, value)."""
        if not isinstance(event, _EnvelopeEvent):
            raise TypeError("did not match C++ signature: create_event(TEnvelopeEvent)")
        self.events.append((event.time, event.value))


class GainEnvelope(FakeEnvelope):
    """As Live reports volume: events carry linear gain, not the parameter value."""

    def events_in_range(self, start, end):
        return [types.SimpleNamespace(time=t, value=10 ** ((v - 0.85) * 4))
                for t, v in self.events if start <= t <= end]


class NoValueAtTimeEnvelope(GainEnvelope):
    def value_at_time(self, time_value):
        raise RuntimeError("unavailable")


class OldEnvelope(FakeEnvelope):
    def create_event(self, time_value, value):
        self.events.append((time_value, value))


class StepEnvelope(FakeEnvelope):
    """create_event refuses every form; only insert_step works."""

    def create_event(self, *args):
        raise TypeError("did not match C++ signature")

    def insert_step(self, start, length, value):
        self.events.append((start, value))


class UnwritableEnvelope(FakeEnvelope):
    def create_event(self, *args):
        raise TypeError("did not match C++ signature")


class FakeLiveClip(ExtendedClip):
    """A MIDI clip with Live 11+ notes and per-clip automation.

    Arrangement copies behave as Live does: `automation_envelope` will not
    resolve their envelopes, and new ones cannot be created on them.
    """

    envelope_type = FakeEnvelope

    def __init__(self, name="Clip", notes=(), start_time=None, length=4.0):
        ExtendedClip.__init__(self, notes)
        self.name = name
        self.color = 0
        self.muted = False
        self.length = length
        self.end_marker = length
        self.loop_end = length
        self.is_audio_clip = False
        self.is_arrangement_clip = start_time is not None
        if start_time is not None:
            self.start_time = start_time
            self.end_time = start_time + length
        self.automation_envelopes = []
        self.launch_mode = 0

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        self.notes = [n for n in self.notes
                      if not (from_pitch <= n.pitch < from_pitch + pitch_span
                              and from_time <= n.start_time < from_time + time_span)]

    def automation_envelope(self, parameter):
        if self.is_arrangement_clip:
            return None
        return next((e for e in self.automation_envelopes if e.parameter is parameter), None)

    def create_automation_envelope(self, parameter):
        if self.is_arrangement_clip:
            raise RuntimeError("Not a session clip or parameter belongs to another track")
        envelope = self.envelope_type(parameter)
        self.automation_envelopes.append(envelope)
        return envelope

    def clear_envelope(self, parameter):
        self.automation_envelopes = [e for e in self.automation_envelopes
                                     if e.parameter is not parameter]


class FakeAudioClip(FakeLiveClip):
    def __init__(self, path, start_time):
        FakeLiveClip.__init__(self, path.rsplit("/", 1)[-1], (), start_time, 8.0)
        self.is_midi_clip = False
        self.is_audio_clip = True


class FakeSlot(object):
    def __init__(self, clip=None):
        self.clip = clip

    @property
    def has_clip(self):
        return self.clip is not None


class ClipLane(object):
    """What a track's main Arrangement lane and a take lane have in common."""

    def __init__(self):
        self._clips = []

    @property
    def arrangement_clips(self):
        return sorted(self._clips, key=lambda c: c.start_time)

    def create_midi_clip(self, start_time, length):
        return self._place(FakeLiveClip("", (), start_time, length))

    def create_audio_clip(self, path, start_time):
        return self._place(FakeAudioClip(path, start_time))

    def _place(self, clip):
        clip.canonical_parent = self
        self._clips.append(clip)
        return clip


class FakeTakeLane(ClipLane):
    def __init__(self, name):
        ClipLane.__init__(self)
        self.name = name

    def delete_clip(self, clip):
        self._clips.remove(clip)


class UndeletableTakeLane(ClipLane):
    """A lane with no delete of its own, whose clips the track will not delete."""

    def __init__(self, name):
        ClipLane.__init__(self)
        self.name = name


class FakeTrack(ClipLane):
    def __init__(self, name, midi=True, slots=2):
        ClipLane.__init__(self)
        self.name = name
        self.has_midi_input = midi
        self.has_audio_input = not midi
        self.can_be_armed = True
        self.arm = False
        self.clip_slots = [FakeSlot() for _ in range(slots)]
        self.take_lanes = []
        self.volume = FakeParameter()
        self.volume.name = "Track Volume"
        self.panning = FakeParameter(-1.0, 1.0)
        self.panning.name = "Track Panning"
        self.mixer_device = types.SimpleNamespace(
            volume=self.volume, panning=self.panning, track_activator=FakeParameter(),
            sends=[])
        self.devices = []

    lane_type = FakeTakeLane

    dropped_on_copy = ()

    def duplicate_clip_to_arrangement(self, clip, at):
        """As Live 12.4.6 did for a volume envelope: the copy carries the
        clip's envelopes. `dropped_on_copy` simulates parameters whose envelope
        does not survive."""
        copy = FakeLiveClip(clip.name, list(clip.notes), at, clip.length)
        copy.automation_envelopes = [
            FakeEnvelope(e.parameter, e.events) for e in clip.automation_envelopes
            if e.parameter not in self.dropped_on_copy]
        self._place(copy)

    def delete_clip(self, clip):
        # As Live 12.4 does: a take lane's clip is silently left alone.
        if clip in self._clips:
            self._clips.remove(clip)

    def create_take_lane(self):
        lane = self.lane_type("Take %d" % (len(self.take_lanes) + 1))
        self.take_lanes.append(lane)
        return lane


class FakeSong(object):
    """With `lag`, transport and record changes land on the next tick, as in Live."""

    def __init__(self, tracks):
        self.tracks = tracks
        self.return_tracks = []
        self.master_track = None
        self._record_mode = False
        self.arrangement_overdub = False
        self.is_playing = False
        self.current_song_time = 0.0
        self.started_from = None
        self.lag = False
        self.queued = []

    def _later(self, change):
        if self.lag:
            self.queued.append(change)
        else:
            change()

    def apply_queued(self):
        queued, self.queued = self.queued, []
        for change in queued:
            change()

    @property
    def record_mode(self):
        return self._record_mode

    @record_mode.setter
    def record_mode(self, value):
        self._later(lambda: setattr(self, "_record_mode", value))

    def start_playing(self):
        def start():
            self.is_playing = True
            self.started_from = self.current_song_time
        self._later(start)

    def stop_playing(self):
        self._later(lambda: setattr(self, "is_playing", False))


class LiveSetScript(object):
    """An AbletonMCP over a fake set, with deferred work run on demand."""

    def __init__(self, tracks):
        self.song = FakeSong(tracks)
        self.script = AbletonMCP.__new__(AbletonMCP)
        self.script._song_ref = self.song
        self.script.log_message = lambda message: None
        self.pending = []
        self.script._defer = lambda ticks, fn: self.pending.append(fn)

    def __getattr__(self, name):
        handler = getattr(self.script, "_" + name)
        return lambda params, respond=None: handler(params, respond)

    def tick(self):
        self.song.apply_queued()
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()

    def settle(self, result, responses, ticks=10):
        """Run ticks until a deferred handler answers; return its result."""
        if result is not mcp.DEFERRED:
            return result
        for _ in range(ticks):
            if responses:
                break
            self.tick()
        return responses[0]["ok"]


def a_set():
    """Track 0: MIDI, a Session clip in slot 0 and two Arrangement clips.
    Track 1: audio, empty."""
    keys = FakeTrack("Keys")
    keys.clip_slots[0].clip = FakeLiveClip("Session riff", [FakeNote(1, 60, 0.0)])
    keys._clips = [
        FakeLiveClip("Verse", [FakeNote(1, 60, 0.0), FakeNote(2, 64, 1.0)], 16.0),
        FakeLiveClip("Intro", [FakeNote(1, 48, 0.0)], 0.0),
    ]
    return LiveSetScript([keys, FakeTrack("Vox", midi=False)])


SESSION = {"track_index": 0, "clip_index": 0}
ARRANGEMENT = {"track_index": 0, "clip_index": 1, "view": "arrangement"}


class ArrangementClipResolutionTests(unittest.TestCase):
    def test_arrangement_clips_are_counted_in_time_order(self):
        live = a_set()
        self.assertEqual(live.script._resolve_clip(ARRANGEMENT).name, "Verse")
        self.assertEqual(live.script._resolve_clip(SESSION).name, "Session riff")

    def test_take_lane_clips_are_reached_by_lane(self):
        live = a_set()
        lane = live.song.tracks[0].create_take_lane()
        lane.create_midi_clip(8.0, 4.0).name = "Take"
        clip = live.script._resolve_clip(dict(ARRANGEMENT, clip_index=0, take_lane_index=0))
        self.assertEqual(clip.name, "Take")
        with self.assertRaises(IndexError) as caught:
            live.script._resolve_clip(dict(ARRANGEMENT, take_lane_index=0))
        self.assertIn("take lane 0", str(caught.exception))

    def test_a_take_lane_is_meaningless_in_the_session(self):
        with self.assertRaises(ValueError) as caught:
            a_set().script._resolve_clip(dict(SESSION, take_lane_index=0))
        self.assertIn("arrangement", str(caught.exception))


class CreateArrangementClipTests(unittest.TestCase):
    def test_creates_a_midi_clip_and_reports_its_index(self):
        live = a_set()
        result = live.create_arrangement_clip({"track_index": 0, "start_time": 4.0, "length": 8.0})
        self.assertEqual(result["clip_index"], 1)
        self.assertEqual(result["view"], "arrangement")
        self.assertEqual((result["clip"]["start_time"], result["clip"]["end_time"]), (4.0, 12.0))
        # The reported index is the one the note tools then accept.
        live.add_notes_to_clip(dict(ARRANGEMENT, notes=[{"pitch": 72, "start_time": 0, "duration": 1}]))
        self.assertEqual([n.pitch for n in live.song.tracks[0].arrangement_clips[1].notes], [72])

    def test_defaults_to_one_bar(self):
        result = a_set().create_arrangement_clip({"track_index": 0, "start_time": 32.0})
        self.assertEqual(result["clip"]["length"], 4.0)

    def test_refuses_to_overlap_an_existing_clip(self):
        live = a_set()
        with self.assertRaises(ValueError) as caught:
            live.create_arrangement_clip({"track_index": 0, "start_time": 14.0, "length": 4.0})
        self.assertIn("'Verse' already occupies beats 16.0 to 20.0", str(caught.exception))
        self.assertEqual(len(live.song.tracks[0].arrangement_clips), 2)
        # Touching end to end is not an overlap.
        live.create_arrangement_clip({"track_index": 0, "start_time": 12.0, "length": 4.0})

    def test_rejects_bad_tracks_lengths_and_old_live(self):
        live = a_set()
        for params, expected in (
            ({"track_index": 1, "start_time": 0}, "not a MIDI track"),
            ({"track_index": 0, "start_time": 40, "length": 0}, "greater than"),
            ({"track_index": 0, "start_time": -1}, "at least"),
        ):
            with self.assertRaises(ValueError) as caught:
                live.create_arrangement_clip(params)
            self.assertIn(expected, str(caught.exception))

        old = LiveSetScript([types.SimpleNamespace(name="Old", has_midi_input=True,
                                                   arrangement_clips=[])])
        with self.assertRaises(RuntimeError) as caught:
            old.create_arrangement_clip({"track_index": 0, "start_time": 0})
        self.assertIn("duplicate_clip_to_arrangement", str(caught.exception))

    def test_creates_on_a_take_lane(self):
        live = a_set()
        live.create_take_lane({"track_index": 0})
        result = live.create_arrangement_clip(
            {"track_index": 0, "start_time": 16.0, "take_lane_index": 0})
        self.assertEqual((result["take_lane_index"], result["clip_index"]), (0, 0))
        self.assertEqual(len(live.song.tracks[0].take_lanes[0].arrangement_clips), 1)
        self.assertEqual(len(live.song.tracks[0].arrangement_clips), 2)

    def test_imports_audio_at_a_position(self):
        live = a_set()
        result = live.create_arrangement_audio_clip(
            {"track_index": 1, "path": "/samples/vox.wav", "start_time": 8.0})
        self.assertEqual(result["clip"]["name"], "vox.wav")
        self.assertTrue(result["clip"]["is_audio"])
        with self.assertRaises(ValueError) as caught:
            live.create_arrangement_audio_clip(
                {"track_index": 1, "path": "/samples/b.wav", "start_time": 10.0})
        self.assertIn("already occupies", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            live.create_arrangement_audio_clip(
                {"track_index": 0, "path": "/samples/b.wav", "start_time": 0.0})
        self.assertIn("not an audio track", str(caught.exception))


class NoteToolsOnBothViewsTests(unittest.TestCase):
    """Every note tool goes through _resolve_clip, so each must work on both."""

    def check(self, run):
        for where in (SESSION, ARRANGEMENT):
            live = a_set()
            clip = live.script._resolve_clip(where)
            run(live, dict(where), clip)

    def test_read_add_replace_clear(self):
        def run(live, where, clip):
            self.assertEqual(live.get_clip_notes(where)["note_count"], len(clip.notes))
            live.add_notes_to_clip(dict(where, notes=[{"pitch": 70, "start_time": 2, "duration": 1}]))
            self.assertIn(70, [n.pitch for n in clip.notes])
            live.replace_clip_notes(dict(where, notes=[{"pitch": 40, "start_time": 0, "duration": 1}]))
            self.assertEqual([n.pitch for n in clip.notes], [40])
            live.clear_notes_from_clip(where)
            self.assertEqual(clip.notes, [])
        self.check(run)

    def test_transpose_modify_and_remove_by_id(self):
        def run(live, where, clip):
            live.transpose_notes(dict(where, semitones=12))
            self.assertEqual(clip.notes[0].pitch, 72)
            live.modify_notes(dict(where, changes=[{"note_id": 1, "velocity": 50}]))
            self.assertEqual(clip.notes[0].velocity, 50.0)
            live.remove_notes_by_id(dict(where, note_ids=[1]))
            self.assertNotIn(1, [n.note_id for n in clip.notes])
        self.check(run)

    def test_duplicate_extends_session_clips_but_never_arrangement_clips(self):
        live = a_set()
        result = live.duplicate_notes(dict(SESSION, time_offset=4))
        self.assertEqual(result["extended"], "loop_end")

        live = a_set()
        clip = live.script._resolve_clip(ARRANGEMENT)
        result = live.duplicate_notes(dict(ARRANGEMENT, time_offset=4))
        self.assertTrue(result["past_clip_end"])
        self.assertEqual((clip.loop_end, clip.end_time), (4.0, 20.0))
        self.assertEqual(result["copied"], 2)


class ArrangementClipPropertiesTests(unittest.TestCase):
    def test_renames_and_mutes_clips_in_either_view(self):
        for where in (SESSION, ARRANGEMENT):
            live = a_set()
            result = live.set_clip_properties(dict(where, name="Hook", muted=True))
            self.assertEqual(sorted(result["changed"]), ["muted", "name"])
            clip = live.script._resolve_clip(where)
            self.assertEqual((clip.name, clip.muted), ("Hook", True))

    def test_launch_settings_are_refused_on_arrangement_clips_only(self):
        live = a_set()
        live.set_clip_properties(dict(SESSION, launch_mode="gate"))
        with self.assertRaises(ValueError) as caught:
            live.set_clip_properties(dict(ARRANGEMENT, name="Hook", launch_mode="gate"))
        self.assertIn("launch_mode only affects Session clips", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            live.set_clip_properties(dict(ARRANGEMENT, legato=True, launch_mode="gate"))
        self.assertIn("launch_mode, legato only affect Session clips", str(caught.exception))
        self.assertEqual(live.script._resolve_clip(ARRANGEMENT).name, "Verse")


class ArrangementEnvelopeTests(unittest.TestCase):
    MIXER_VOLUME = {"device_index": -1, "parameter_index": 0}

    def live_with_placed_envelope(self):
        """An Arrangement clip listing a volume envelope in its undocumented
        `automation_envelopes` collection, under a parameter object other
        than the track's."""
        live = a_set()
        copy_of_volume = FakeParameter()
        copy_of_volume.name = "Track Volume"
        clip = live.script._resolve_clip(ARRANGEMENT)
        clip.automation_envelopes.append(FakeEnvelope(copy_of_volume, [(0.0, 0.2), (4.0, 0.8)]))
        return live, clip

    def test_reads_and_rewrites_a_placed_envelope_in_place(self):
        live, clip = self.live_with_placed_envelope()
        where = dict(ARRANGEMENT, **self.MIXER_VOLUME)
        self.assertEqual(live.get_clip_envelope(where)["points"], [[0.0, 0.2], [4.0, 0.8]])
        live.set_clip_envelope(dict(where, points=[[0, 1.0], [2, 0.0]]))
        self.assertEqual(sorted(clip.automation_envelopes[0].events), [(0, 1.0), (2, 0.0), (4.0, 0.0)])

    def test_creates_on_session_clips_and_explains_the_arrangement_refusal(self):
        live = a_set()
        live.set_clip_envelope(dict(SESSION, points=[[0, 0.5]], **self.MIXER_VOLUME))
        self.assertTrue(live.get_clip_envelope(dict(SESSION, **self.MIXER_VOLUME))["has_envelope"])
        with self.assertRaises(RuntimeError) as caught:
            live.set_clip_envelope(dict(ARRANGEMENT, points=[[0, 0.5]], **self.MIXER_VOLUME))
        self.assertIn("duplicate_clip_to_arrangement", str(caught.exception))

    def test_writes_breakpoints_as_envelope_events(self):
        live = a_set()
        result = live.set_clip_envelope(
            dict(SESSION, points=[[0, 0.2], [2, 0.9]], **self.MIXER_VOLUME))
        self.assertEqual((result["wrote_as"], result["via"]), ("breakpoints", "EnvelopeEvent"))
        clip = live.script._resolve_clip(SESSION)
        self.assertEqual(clip.automation_envelopes[0].events, [(0.0, 0.2), (2.0, 0.9), (4.0, 0.9)])

    def test_reads_back_what_was_written_in_parameter_units(self):
        pan = {"device_index": -1, "parameter_index": 1}
        for envelope_type in (FakeEnvelope, GainEnvelope):
            live = a_set()
            live.script._resolve_clip(SESSION).envelope_type = envelope_type
            written = [[0.0, -0.5], [2.0, 0.75], [4.0, 0.25]]
            live.set_clip_envelope(dict(SESSION, points=written, **pan))
            read = live.get_clip_envelope(dict(SESSION, **pan))
            self.assertEqual(read["points"], written, envelope_type.__name__)
            self.assertNotIn("values", read)

    def test_volume_reads_in_parameter_space_not_gain(self):
        live = a_set()
        live.script._resolve_clip(SESSION).envelope_type = GainEnvelope
        live.set_clip_envelope(dict(SESSION, points=[[0, 0.3], [2, 0.9], [4, 0.5]],
                                    **self.MIXER_VOLUME))
        read = live.get_clip_envelope(dict(SESSION, **self.MIXER_VOLUME))
        self.assertEqual(read["points"], [[0.0, 0.3], [2.0, 0.9], [4.0, 0.5]])

    def test_without_value_at_time_raw_values_are_labelled(self):
        live = a_set()
        live.script._resolve_clip(SESSION).envelope_type = NoValueAtTimeEnvelope
        live.set_clip_envelope(dict(SESSION, points=[[0, 0.85]], **self.MIXER_VOLUME))
        read = live.get_clip_envelope(dict(SESSION, **self.MIXER_VOLUME))
        self.assertEqual(read["values"], "internal")
        self.assertEqual(read["points"][0], [0.0, 1.0])

    def test_reports_clamped_values(self):
        live = a_set()
        result = live.set_clip_envelope(
            dict(SESSION, points=[[0, 5], [2, 0.5], [3, -1]], **self.MIXER_VOLUME))
        self.assertEqual(result["clamped"], 2)
        result = live.set_clip_envelope(dict(SESSION, points=[[0, 0.5]], **self.MIXER_VOLUME))
        self.assertNotIn("clamped", result)

    def test_falls_back_to_the_old_signature_then_to_steps(self):
        for envelope_type, via, wrote_as in ((OldEnvelope, "create_event(time, value)", "breakpoints"),
                                             (StepEnvelope, "insert_step", "steps")):
            live = a_set()
            live.script._resolve_clip(SESSION).envelope_type = envelope_type
            result = live.set_clip_envelope(
                dict(SESSION, points=[[0, 0.0], [4, 1.0]], resolution=1.0, **self.MIXER_VOLUME))
            self.assertEqual((result["via"], result["wrote_as"]), (via, wrote_as))
            if wrote_as == "steps":
                self.assertIn("stepped approximation", result["note"])
                self.assertEqual(result["written"], 4)

    def test_a_failed_write_leaves_no_empty_envelope(self):
        live = a_set()
        clip = live.script._resolve_clip(SESSION)
        clip.envelope_type = UnwritableEnvelope
        with self.assertRaises(RuntimeError):
            live.set_clip_envelope(dict(SESSION, points=[[0, 0.5]], **self.MIXER_VOLUME))
        self.assertEqual(clip.automation_envelopes, [])
        self.assertFalse(live.get_clip_envelope(dict(SESSION, **self.MIXER_VOLUME))["has_envelope"])

    def test_invalid_points_create_nothing(self):
        live = a_set()
        with self.assertRaises(ValueError):
            live.set_clip_envelope(dict(SESSION, points=[], **self.MIXER_VOLUME))
        self.assertEqual(live.script._resolve_clip(SESSION).automation_envelopes, [])

    def test_clears_a_placed_envelope_by_its_own_parameter(self):
        live, clip = self.live_with_placed_envelope()
        result = live.clear_clip_envelope(dict(ARRANGEMENT, **self.MIXER_VOLUME))
        self.assertTrue(result["cleared"])
        self.assertIn("cannot be written again", result["note"])
        self.assertEqual(clip.automation_envelopes, [])

    def test_clearing_nothing_says_so(self):
        for where in (SESSION, ARRANGEMENT):
            result = a_set().clear_clip_envelope(dict(where, **self.MIXER_VOLUME))
            self.assertFalse(result["cleared"])


class DuplicateClipToArrangementTests(unittest.TestCase):
    def test_an_occupied_span_is_refused_and_nothing_is_placed(self):
        live = a_set()
        with self.assertRaises(ValueError) as caught:
            live.duplicate_clip_to_arrangement(
                {"track_index": 0, "clip_index": 0, "destination_time": 14.0})
        self.assertIn("'Verse' already occupies", str(caught.exception))
        self.assertEqual(len(live.song.tracks[0].arrangement_clips), 2)

    def test_copies_report_their_arrangement_clip_index(self):
        live = a_set()
        result = live.duplicate_clip_to_arrangement(
            {"track_index": 0, "clip_index": 0, "destination_time": 4.0, "repeats": 2})
        self.assertEqual(result["copies"], [{"clip_index": 1, "start_time": 4.0},
                                            {"clip_index": 2, "start_time": 8.0}])
        self.assertEqual(result["envelopes"], [])
        self.assertNotIn("note", result)

    def test_carried_envelopes_are_confirmed_on_every_copy(self):
        live = a_set()
        live.set_clip_envelope(dict(SESSION, points=[[0, 0.2], [4, 0.8]],
                                    device_index=-1, parameter_index=0))
        result = live.duplicate_clip_to_arrangement(
            {"track_index": 0, "clip_index": 0, "destination_time": 4.0, "repeats": 2})
        self.assertEqual(result["envelopes"], [
            {"device": "Mixer", "parameter": "Track Volume",
             "carried": True, "missing_on": []}])
        self.assertNotIn("note", result)
        read = live.get_clip_envelope(dict(ARRANGEMENT, clip_index=2,
                                           device_index=-1, parameter_index=0))
        self.assertTrue(read["has_envelope"])

    def test_a_dropped_envelope_is_named(self):
        live = a_set()
        track = live.song.tracks[0]
        live.set_clip_envelope(dict(SESSION, points=[[0, 0.2], [4, 0.8]],
                                    device_index=-1, parameter_index=0))
        track.dropped_on_copy = (track.volume,)
        result = live.duplicate_clip_to_arrangement(
            {"track_index": 0, "clip_index": 0, "destination_time": 4.0})
        self.assertEqual(result["envelopes"], [
            {"device": "Mixer", "parameter": "Track Volume",
             "carried": False, "missing_on": [1]}])
        self.assertIn("Mixer Track Volume", result["note"])


class DeleteArrangementClipTests(unittest.TestCase):
    def test_deletes_from_the_main_lane_or_a_take_lane(self):
        live = a_set()
        live.create_take_lane({"track_index": 0})
        live.create_arrangement_clip({"track_index": 0, "start_time": 0, "take_lane_index": 0})
        result = live.delete_clip(dict(ARRANGEMENT, clip_index=0, take_lane_index=0))
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(len(live.song.tracks[0].arrangement_clips), 2)

        result = live.delete_arrangement_clip({"track_index": 0, "clip_index": 0})
        self.assertEqual((result["deleted"], result["remaining"]), ("Intro", 1))

    def test_a_take_lane_clip_the_track_will_not_delete_is_an_error(self):
        live = a_set()
        live.song.tracks[0].lane_type = UndeletableTakeLane
        live.create_take_lane({"track_index": 0})
        live.create_arrangement_clip({"track_index": 0, "start_time": 0, "take_lane_index": 0})
        with self.assertRaises(RuntimeError) as caught:
            live.delete_clip(dict(ARRANGEMENT, clip_index=0, take_lane_index=0))
        message = str(caught.exception)
        self.assertIn("no way to delete a take lane clip", message)
        self.assertIn("the take lane (UndeletableTakeLane) has no delete_clip", message)
        self.assertIn("the track (FakeTrack).delete_clip left it in place", message)
        self.assertIn("Delete it in Live itself", message)
        self.assertEqual(len(live.song.tracks[0].take_lanes[0].arrangement_clips), 1)
        self.assertEqual(len(live.song.tracks[0].arrangement_clips), 2)

    def test_duplicate_clip_refuses_arrangement_clips(self):
        with self.assertRaises(ValueError) as caught:
            a_set().duplicate_clip(dict(ARRANGEMENT, target_clip_index=1))
        self.assertIn("duplicate_clip_to_arrangement", str(caught.exception))


class TakeLaneTests(unittest.TestCase):
    def test_lists_lanes_with_their_clips(self):
        live = a_set()
        created = live.create_take_lane({"track_index": 0, "name": "Alt chorus"})
        self.assertEqual((created["index"], created["name"], created["count"]), (0, "Alt chorus", 1))
        live.create_take_lane({"track_index": 0})
        live.create_arrangement_clip({"track_index": 0, "start_time": 4, "take_lane_index": 1})

        listing = live.get_take_lanes({"track_index": 0})
        self.assertEqual(listing["count"], 2)
        self.assertEqual([l["clip_count"] for l in listing["take_lanes"]], [0, 1])
        self.assertEqual(listing["take_lanes"][1]["clips"][0]["start_time"], 4)

        clips = live.get_arrangement_clips({"track_index": 0, "take_lane_index": 1})
        self.assertEqual((clips["count"], clips["take_lane_index"]), (1, 1))
        self.assertEqual(live.get_arrangement_clips({"track_index": 0})["count"], 2)

    def test_unknown_lanes_and_old_live_are_explained(self):
        live = a_set()
        with self.assertRaises(IndexError):
            live.get_arrangement_clips({"track_index": 0, "take_lane_index": 0})
        old = LiveSetScript([types.SimpleNamespace(name="Old")])
        with self.assertRaises(RuntimeError) as caught:
            old.get_take_lanes({"track_index": 0})
        self.assertIn("take lanes", str(caught.exception))


class ArrangementRecordingTests(unittest.TestCase):
    def test_arms_presses_record_and_starts_the_transport(self):
        live = a_set()
        state = live.start_arrangement_recording({"arm_tracks": [0], "overdub": True})
        self.assertTrue(state["recording"] and state["is_playing"])
        self.assertTrue(live.song.arrangement_overdub)
        self.assertEqual([t["name"] for t in state["armed_tracks"]], ["Keys"])
        self.assertNotIn("warning", state)

    def test_waits_for_the_playhead_before_recording_from_a_position(self):
        live = a_set()
        responses = []
        result = live.start_arrangement_recording({"arm_tracks": [1], "from_time": 32.0},
                                                  responses.append)
        self.assertIs(result, mcp.DEFERRED)
        self.assertFalse(live.song.record_mode)
        state = live.settle(result, responses)
        self.assertEqual(live.song.started_from, 32.0)
        self.assertTrue(state["recording"])

    def test_reports_the_state_once_live_has_applied_it(self):
        live = a_set()
        live.song.lag = True
        responses = []
        state = live.settle(
            live.start_arrangement_recording({"arm_tracks": [0]}, responses.append), responses)
        self.assertTrue(state["recording"] and state["is_playing"])

        live.song.tracks[0].create_midi_clip(64.0, 8.0)  # the take, once stopped
        responses = []
        state = live.settle(
            live.stop_arrangement_recording({"disarm": True}, responses.append), responses)
        self.assertFalse(state["recording"] or state["is_playing"])
        self.assertEqual(state["armed_tracks"][0]["arrangement_clip_count"], 3)
        self.assertEqual(state["disarmed"], ["Keys"])

    def test_inside_a_batch_reports_what_was_requested(self):
        live = a_set()
        live.song.lag = True
        state = live.start_arrangement_recording({"arm_tracks": [0]})
        self.assertTrue(state["recording"] and state["is_playing"])
        self.assertIn("as requested", state["reported"])
        state = live.stop_arrangement_recording({})
        self.assertFalse(state["recording"] or state["is_playing"])

    def test_bad_input_changes_nothing(self):
        live = a_set()
        with self.assertRaises(IndexError):
            live.start_arrangement_recording({"arm_tracks": [0, 9]})
        with self.assertRaises(RuntimeError) as caught:
            live.start_arrangement_recording({"arm_tracks": [0], "from_time": 8})
        self.assertIn("batch", str(caught.exception))
        self.assertFalse(live.song.tracks[0].arm or live.song.record_mode)

    def test_warns_when_nothing_is_armed(self):
        state = a_set().start_arrangement_recording({})
        self.assertIn("arm_tracks", state["warning"])

    def test_stops_and_optionally_disarms(self):
        live = a_set()
        live.start_arrangement_recording({"arm_tracks": [0, 1]})
        state = live.stop_arrangement_recording({"stop_playback": False, "disarm": True})
        self.assertTrue(state["was_recording"])
        self.assertFalse(live.song.record_mode)
        self.assertTrue(live.song.is_playing)
        self.assertEqual(state["disarmed"], ["Keys", "Vox"])
        self.assertFalse(any(t.arm for t in live.song.tracks))

        state = live.stop_arrangement_recording({})
        self.assertFalse(state["was_recording"] or live.song.is_playing)

if __name__ == "__main__":
    unittest.main(verbosity=2)
