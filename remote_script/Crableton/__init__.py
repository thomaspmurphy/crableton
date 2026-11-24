# Crableton, the Live-side half of the crableton MCP server.
#
# Ableton Live only loads Python control surfaces, so this script runs inside
# Live, exposes its object model over a TCP socket, and is driven by the
# crableton MCP server.
#
# Two rules shape everything here:
#
#   1. Live's API may only be touched from its main thread. Socket work happens
#      on client threads, so anything that reads or writes the model is handed
#      to the main thread via schedule_message and answered through a queue.
#   2. Some of Live's writes do not take effect within the tick that made them
#      (moving the playhead, most notably). Handlers that depend on a previous
#      write landing must defer to a later tick rather than assume.
#
# Install with `crableton install`, then pick "Crableton" as a Control Surface
# in Live's settings.

from __future__ import absolute_import, print_function, unicode_literals

import json
import math
import re
import socket
import threading
import time
import traceback

from _Framework.ControlSurface import ControlSurface

try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# 9878, not 9877: the upstream ableton-mcp script uses 9877, and both can be
# selected as Control Surfaces at the same time. Different ports is all it takes
# for them to coexist.
DEFAULT_PORT = 9878
HOST = "127.0.0.1"

SCRIPT_VERSION = "2.6.0"
PROTOCOL_VERSION = 2

# Sentinel returned by handlers that will answer on a later tick.
DEFERRED = object()

# How many main-thread ticks a deferred handler may wait before giving up.
MAX_DEFER_TICKS = 40

# Locators within this many beats of each other are the same locator.
CUE_TOLERANCE = 1e-3

# Ticks the playhead must hold still before a move is judged to have landed.
PLAYHEAD_SETTLE_TICKS = 3


# ── Value maps ───────────────────────────────────────────────────────────────
# The MCP layer speaks in musician's terms; Live speaks in enum indices.

LAUNCH_QUANTIZATION = [
    "none", "8 bars", "4 bars", "2 bars", "1 bar", "1/2", "1/2T", "1/4",
    "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32",
]

RECORDING_QUANTIZATION = [
    "none", "1/4", "1/8", "1/8T", "1/8 + 1/8T", "1/16", "1/16T",
    "1/16 + 1/16T", "1/32",
]

# Clip.quantize() grid indices.
QUANTIZE_GRID = ["1/4", "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32", "1/32T"]

LAUNCH_MODE = ["trigger", "gate", "toggle", "repeat"]

WARP_MODE = {
    "beats": 0, "tones": 1, "texture": 2, "repitch": 3,
    "complex": 4, "complex pro": 6,
}

FOLLOW_ACTION = [
    "none", "stop", "play again", "previous", "next",
    "first", "last", "any", "other", "jump",
]

MONITORING_STATE = ["in", "auto", "off"]

# Launch settings: Arrangement clips carry them but never use them.
SESSION_ONLY_CLIP_FIELDS = (
    "launch_mode", "launch_quantization", "legato", "velocity_amount",
    "follow_action_enabled", "follow_action_time", "follow_action_a",
    "follow_action_b", "follow_action_chance_a", "follow_action_chance_b",
)
CROSSFADE_ASSIGN = ["a", "none", "b"]

DEVICE_TYPE = {0: "undefined", 1: "instrument", 2: "audio_effect", 4: "midi_effect"}

BROWSER_CATEGORIES = [
    "instruments", "sounds", "drums", "audio_effects", "midi_effects",
    "plugins", "max_for_live", "samples", "packs", "user_library",
]


def _index_of(table, value, what):
    """Map a friendly name onto Live's enum index."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if key in table:
        return table.index(key)
    raise ValueError("unknown %s %r; expected one of: %s" % (what, value, ", ".join(table)))


def _name_of(table, index, default=None):
    try:
        return table[int(index)]
    except (IndexError, TypeError, ValueError):
        return default


# ── Displayed values ─────────────────────────────────────────────────────────
# Live formats every parameter for display ("1.50 kHz", "-12.0 dB", "4.00 : 1")
# and the raw value behind it is often non-linear. Parsing both sides to a
# number in a canonical unit lets a target like "220 Hz" be searched for.

_UNITS = {
    "": ("", 1.0),
    "k": ("", 1000.0),
    "hz": ("hz", 1.0),
    "khz": ("hz", 1000.0),
    "s": ("s", 1.0),
    "sec": ("s", 1.0),
    "ms": ("s", 0.001),
    "db": ("db", 1.0),
    "%": ("%", 1.0),
    "st": ("st", 1.0),
    "semitones": ("st", 1.0),
    "ct": ("ct", 1.0),
    "cent": ("ct", 1.0),
    "cents": ("ct", 1.0),
}
_NUMBER = r"[+-]?(?:inf|\d+(?:\.\d*)?|\.\d+)"
_NUMBER_WITH_UNIT = re.compile(r"^(%s)\s*(.*)$" % _NUMBER)
_RATIO = re.compile(r"^(%s)\s*:\s*1$" % _NUMBER)
_FRACTION = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_PAN = re.compile(r"^(\d+(?:\.\d*)?)\s*([lr])$")

# How many points the monotonicity check and the fallback scan sample.
DISPLAY_GRID = 32
DISPLAY_SCAN = 1000
DISPLAY_BISECTIONS = 48


def parse_display(text):
    """Read a displayed value as (number, unit): "1.5 kHz" is (1500.0, "hz").

    Units are canonical (hz, s, db, %, st, ct, ratio, pan, fraction); an unknown
    unit is kept as written and only matches itself. None when there is no
    leading number at all.
    """
    if text is None:
        return None
    t = str(text).strip().lower().replace(u"−", "-")
    if t in ("c", "center", "centre"):
        return (0.0, "pan")
    m = _PAN.match(t)
    if m:
        amount = float(m.group(1))
        return (-amount if m.group(2) == "l" else amount, "pan")
    m = _RATIO.match(t)
    if m:
        return (float(m.group(1)), "ratio")
    m = _FRACTION.match(t)
    if m and int(m.group(2)):
        return (float(m.group(1)) / float(m.group(2)), "fraction")
    m = _NUMBER_WITH_UNIT.match(t)
    if not m:
        return None
    number, unit = float(m.group(1)), m.group(2).strip()
    if unit in _UNITS:
        canonical, scale = _UNITS[unit]
        return (number * scale, canonical)
    return (number, unit)


def _units_compatible(wanted, shown):
    return not wanted or not shown or wanted == shown


def _display_error(achieved, goal):
    if achieved == goal:
        return 0.0
    error = achieved - goal
    if math.isnan(error) or math.isinf(error):
        return None
    return round(error, 6)


# ── Meters ───────────────────────────────────────────────────────────────────

# schedule_message counts in Live's display ticks, about 100 ms each, which is
# the finest interval the meters can be sampled at without blocking Live.
METER_TICK_SECONDS = 0.1
MAX_METER_SECONDS = 30.0
MAX_PROBE_SECONDS = 60.0
# probe_entry thresholds, on Live's 0..1 meter scale.
ENTRY_SILENT = 0.1
ENTRY_LOUD = 0.3
ENTRY_RISE = 0.15
# A change must last this many beats to count, which rejects a single hit.
ENTRY_PERSIST_BEATS = 1.0
# A fall this long (in per-beat minimums) that recovers is a dropout.
ENTRY_DIP_BEATS = 1.0
# A dip is a fall of this fraction below the track's usual per-beat minimum. Live's
# meters fall so slowly that 2-3 beat volume dropouts only took a ~0.8 track to
# 0.40 and 0.61 (issue #3), never near silence; 0.35 would miss the second.
ENTRY_DIP_DEPTH = 0.2
# The meters lag, so a dip may show up to this many beats either side of the beat.
ENTRY_LAG_BEATS = 2.0
# Roughly how long Live's output meters take to fall from ~0.8 to ~0.1 after a
# sound stops: issue #3's dropout traces fell about 0.3 per second.
METER_DECAY_SECONDS = 2.0


def create_instance(c_instance):
    return AbletonMCP(c_instance)


class AbletonMCP(ControlSurface):
    """Serves Live's object model over TCP."""

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self.log_message("Crableton %s starting" % SCRIPT_VERSION)

        self._song_ref = self.song()
        self.running = False
        self.server = None
        self.server_thread = None
        self.client_threads = []

        self._handlers = self._build_handlers()

        self.start_server()
        self.show_message("Crableton %s: listening on port %d" % (SCRIPT_VERSION, DEFAULT_PORT))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def disconnect(self):
        self.log_message("Crableton disconnecting")
        self.running = False
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
        ControlSurface.disconnect(self)

    def start_server(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(8)
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            self.log_message("Listening on %s:%d" % (HOST, DEFAULT_PORT))
        except Exception as e:
            self.log_message("Could not start server: %s" % e)
            self.show_message("Crableton: port %d unavailable, %s" % (DEFAULT_PORT, e))

    def _server_thread(self):
        try:
            self.server.settimeout(1.0)
            while self.running:
                try:
                    client, address = self.server.accept()
                except socket.timeout:
                    continue
                except Exception:
                    if self.running:
                        self.log_message("Accept failed: %s" % traceback.format_exc())
                    continue

                thread = threading.Thread(target=self._handle_client, args=(client,))
                thread.daemon = True
                thread.start()
                self.client_threads = [t for t in self.client_threads if t.is_alive()]
                self.client_threads.append(thread)
        except Exception:
            self.log_message("Server thread died: %s" % traceback.format_exc())

    def _handle_client(self, client):
        """One command at a time per connection; the MCP server opens several."""
        buffer = ""
        try:
            client.settimeout(None)
            while self.running:
                data = client.recv(65536)
                if not data:
                    break
                try:
                    buffer += data.decode("utf-8")
                except AttributeError:
                    buffer += data

                # Commands are newline-delimited, but tolerate a client that
                # sends bare concatenated JSON.
                while True:
                    command, buffer = self._take_command(buffer)
                    if command is None:
                        break
                    response = self._process_command(command)
                    payload = json.dumps(response) + "\n"
                    try:
                        client.sendall(payload.encode("utf-8"))
                    except AttributeError:
                        client.sendall(payload)
        except Exception:
            self.log_message("Client handler stopped: %s" % traceback.format_exc())
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _take_command(buffer):
        """Pull one complete JSON object off the front of the buffer."""
        stripped = buffer.lstrip()
        if not stripped:
            return None, ""
        decoder = json.JSONDecoder()
        try:
            command, end = decoder.raw_decode(stripped)
        except ValueError:
            return None, buffer  # incomplete; wait for more
        return command, stripped[end:]

    # ── Dispatch ─────────────────────────────────────────────────────────────

    def _process_command(self, command):
        command_id = command.get("id")
        try:
            result = self._run(command.get("type", ""), command.get("params") or {})
            response = {"status": "success", "result": result}
        except Exception as e:
            self.log_message("%s failed: %s" % (command.get("type"), traceback.format_exc()))
            response = {"status": "error", "message": str(e) or e.__class__.__name__}
        if command_id is not None:
            response["id"] = command_id
        return response

    def _run(self, command_type, params):
        if command_type == "get_script_info":
            return self._get_script_info()
        if command_type == "batch":
            return self._on_main_thread(self._batch, params)

        handler = self._handlers.get(command_type)
        if handler is None:
            raise KeyError("unknown command: %s" % command_type)
        return self._on_main_thread(handler, params)

    def _on_main_thread(self, handler, params):
        """Run a handler on Live's main thread and wait for its result.

        A handler may return DEFERRED and call ``respond`` itself later, which
        is how anything needing more than one tick is expressed.
        """
        answer = queue.Queue(1)

        def task():
            try:
                result = handler(params, answer.put)
                if result is not DEFERRED:
                    answer.put({"ok": result})
            except Exception as e:
                self.log_message("main-thread task failed: %s" % traceback.format_exc())
                answer.put({"error": str(e) or e.__class__.__name__})

        try:
            self.schedule_message(0, task)
        except AssertionError:
            task()  # already on the main thread

        try:
            outcome = answer.get(timeout=100.0)
        except queue.Empty:
            raise RuntimeError("Live did not finish the operation in time")

        if "error" in outcome:
            raise RuntimeError(outcome["error"])
        return outcome["ok"]

    def _defer(self, ticks, fn):
        """Run fn after `ticks` main-thread ticks."""
        if ticks <= 0:
            fn()
            return
        self.schedule_message(ticks, fn)

    def _batch(self, params, respond):
        """Run a list of commands in one main-thread pass.

        This is the reason batching is worth having: each command otherwise
        costs a socket round trip plus a scheduling tick, and the user watches
        the set change one step at a time.
        """
        commands = params.get("commands") or []
        stop_on_error = params.get("stop_on_error", True)
        results = []

        for step, command in enumerate(commands):
            command_type = command.get("type", "")
            handler = self._handlers.get(command_type)
            if handler is None:
                outcome = {"step": step, "command": command_type,
                           "status": "error", "message": "unknown command"}
            else:
                try:
                    # Deferring inside a batch would break its ordering
                    # guarantee, so batched handlers must complete inline.
                    value = handler(command.get("params") or {}, None)
                    if value is DEFERRED:
                        raise RuntimeError(
                            "%s needs more than one pass and cannot run inside a batch"
                            % command_type
                        )
                    outcome = {"step": step, "command": command_type,
                               "status": "success", "result": value}
                except Exception as e:
                    outcome = {"step": step, "command": command_type, "status": "error",
                               "message": str(e) or e.__class__.__name__}
            results.append(outcome)
            if outcome["status"] == "error" and stop_on_error:
                break

        failed = [r for r in results if r["status"] == "error"]
        return {
            "ran": len(results),
            "of": len(commands),
            "failed": len(failed),
            "results": results,
        }

    def _get_script_info(self):
        song = self._song_ref
        # Ask the class, not a clip in the set: a set with no Session clips
        # would otherwise report no envelope support at all.
        try:
            import Live
            clip_envelopes = hasattr(Live.Clip.Clip, "automation_envelope")
            envelope_events = hasattr(Live, "Envelope") and hasattr(Live.Envelope, "EnvelopeEvent")
        except (ImportError, AttributeError):
            clip_envelopes = envelope_events = False

        first_track = song.tracks[0] if len(song.tracks) else None
        return {
            "name": "Crableton",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "live_version": self._live_version(),
            "capabilities": sorted(self._handlers.keys()) + ["batch", "get_script_info"],
            "features": {
                "batch": True,
                "clip_envelopes": clip_envelopes,
                "envelope_events": envelope_events,
                "extended_notes": hasattr(song, "view"),
                "arrangement_clip_creation": hasattr(first_track, "create_midi_clip"),
                "take_lanes": hasattr(first_track, "take_lanes"),
                "track_name_guard": True,
                "display_values": True,
                "meters": True,
            },
        }

    def _live_version(self):
        try:
            app = self.application()
            return "%d.%d.%d" % (
                app.get_major_version(),
                app.get_minor_version(),
                app.get_bugfix_version(),
            )
        except Exception:
            return "unknown"

    def _build_handlers(self):
        return {
            # song / transport
            "describe_live_object": self._describe_live_object,
            "get_session_info": self._get_session_info,
            "get_session_snapshot": self._get_session_snapshot,
            "set_tempo": self._set_tempo,
            "tap_tempo": self._tap_tempo,
            "start_playback": self._start_playback,
            "stop_playback": self._stop_playback,
            "continue_playback": self._continue_playback,
            "stop_all_clips": self._stop_all_clips,
            "set_current_song_time": self._set_current_song_time,
            "set_loop_region": self._set_loop_region,
            "set_time_signature": self._set_time_signature,
            "set_transport_options": self._set_transport_options,
            "undo": self._undo,
            "redo": self._redo,
            "capture_midi": self._capture_midi,
            "set_scale": self._set_scale,
            "set_view": self._set_view,
            "set_selection": self._set_selection,
            # scenes
            "get_scenes": self._get_scenes,
            "create_scene": self._create_scene,
            "delete_scene": self._delete_scene,
            "duplicate_scene": self._duplicate_scene,
            "capture_and_insert_scene": self._capture_and_insert_scene,
            "fire_scene": self._fire_scene,
            "set_scene_properties": self._set_scene_properties,
            # tracks
            "get_tracks": self._get_tracks,
            "get_track_info": self._get_track_info,
            "create_midi_track": self._create_midi_track,
            "create_audio_track": self._create_audio_track,
            "create_return_track": self._create_return_track,
            "delete_track": self._delete_track,
            "duplicate_track": self._duplicate_track,
            "set_track_name": self._set_track_name,
            "set_track_mixer": self._set_track_mixer,
            "set_track_send": self._set_track_send,
            "get_track_routing": self._get_track_routing,
            "set_track_routing": self._set_track_routing,
            "get_meters": self._get_meters,
            "probe_entry": self._probe_entry,
            # clips
            "get_clip": self._get_clip,
            "create_clip": self._create_clip,
            "create_audio_clip": self._create_audio_clip,
            "delete_clip": self._delete_clip,
            "duplicate_clip": self._duplicate_clip,
            "fire_clip": self._fire_clip,
            "stop_clip": self._stop_clip,
            "set_clip_properties": self._set_clip_properties,
            "quantize_clip": self._quantize_clip,
            "crop_clip": self._crop_clip,
            "duplicate_clip_loop": self._duplicate_clip_loop,
            "get_clip_envelope": self._get_clip_envelope,
            "set_clip_envelope": self._set_clip_envelope,
            "clear_clip_envelope": self._clear_clip_envelope,
            # notes
            "get_clip_notes": self._get_clip_notes,
            "add_notes_to_clip": self._add_notes_to_clip,
            "replace_clip_notes": self._replace_clip_notes,
            "clear_notes_from_clip": self._clear_notes_from_clip,
            "transpose_notes": self._transpose_notes,
            "duplicate_notes": self._duplicate_notes,
            "modify_notes": self._modify_notes,
            "remove_notes_by_id": self._remove_notes_by_id,
            # devices
            "get_devices": self._get_devices,
            "get_device_parameters": self._get_device_parameters,
            "set_device_parameter": self._set_device_parameter,
            "set_device_parameters": self._set_device_parameters,
            "set_device_enabled": self._set_device_enabled,
            "delete_device": self._delete_device,
            "get_rack_chains": self._get_rack_chains,
            "set_chain_mixer": self._set_chain_mixer,
            "get_simpler_sample": self._get_simpler_sample,
            # browser
            "search_browser": self._search_browser,
            "get_browser_tree": self._get_browser_tree,
            "get_browser_items_at_path": self._get_browser_items_at_path,
            "load_browser_item": self._load_browser_item,
            "load_drum_kit": self._load_drum_kit,
            # arrangement
            "get_arrangement_clips": self._get_arrangement_clips,
            "duplicate_clip_to_arrangement": self._duplicate_clip_to_arrangement,
            "create_arrangement_clip": self._create_arrangement_clip,
            "create_arrangement_audio_clip": self._create_arrangement_audio_clip,
            "delete_arrangement_clip": self._delete_arrangement_clip,
            "clear_arrangement": self._clear_arrangement,
            "get_take_lanes": self._get_take_lanes,
            "create_take_lane": self._create_take_lane,
            "start_arrangement_recording": self._start_arrangement_recording,
            "stop_arrangement_recording": self._stop_arrangement_recording,
            "get_locators": self._get_locators,
            "create_locator": self._create_locator,
            "delete_locator": self._delete_locator,
            "clear_locators": self._clear_locators,
            "jump_to_locator": self._jump_to_locator,
        }

    # ── Resolution helpers ───────────────────────────────────────────────────

    def _resolve_track(self, params):
        song = self._song_ref
        track_type = (params.get("track_type") or "regular").lower()
        expected = params.get("expect_track_name")
        if track_type == "master":
            track = song.master_track
            if expected is not None:
                self._check_track_name(track, expected, "master track", [])
            return track
        collection = song.return_tracks if track_type == "return" else song.tracks
        index = int(params.get("track_index", 0))
        if not 0 <= index < len(collection):
            raise IndexError(
                "%s track %d does not exist (there are %d)"
                % (track_type, index, len(collection))
            )
        track = collection[index]
        if expected is not None:
            self._check_track_name(
                track, expected, "%s track %d" % (track_type, index), collection,
                is_return=track_type == "return")
        return track

    @staticmethod
    def _require_index_for_guard(params):
        if params.get("expect_track_name") is not None and params.get("track_index") is None:
            raise ValueError("expect_track_name was given without track_index; "
                             "pass the index the name was read at")

    @staticmethod
    def _bare_return_name(name):
        # Live reports return tracks with their letter, "A-Reverb", while the
        # user (and the agent) often know them as plain "Reverb".
        head, sep, rest = name.partition("-")
        if sep and 1 <= len(head) <= 2 and head.isalpha() and head.isupper():
            return rest
        return name

    @classmethod
    def _track_name_matches(cls, actual, expected, is_return=False):
        actual = (actual or "").strip()
        expected = (expected or "").strip()
        if actual == expected:
            return True
        if is_return:
            return cls._bare_return_name(actual).strip() == cls._bare_return_name(expected).strip()
        return False

    def _check_track_name(self, track, expected, where, collection, is_return=False):
        """Refuse when the track at an index is no longer the one the caller read.

        Handlers run on Live's main thread, so this check and the write that
        follows it cannot be split by a user edit.
        """
        actual = self._get(track, "name", "")
        if self._track_name_matches(actual, expected, is_return):
            return
        expected = str(expected).strip()
        found = [i for i, t in enumerate(collection)
                 if self._track_name_matches(self._get(t, "name", ""), expected, is_return)]
        if not found:
            moved = "%s is not in the set" % expected
        elif len(found) == 1:
            moved = "%s is now at index %d" % (expected, found[0])
        else:
            moved = "%s is now at indices %s" % (expected, ", ".join(str(i) for i in found))
        raise ValueError(
            '%s is "%s", expected "%s"; the set changed (%s). Re-read with get_tracks.'
            % (where, actual, expected, moved)
        )

    def _resolve_clip(self, params):
        track = self._resolve_track(params)
        index = int(params.get("clip_index", 0))
        view = (params.get("view") or "session").lower()
        lane_index = params.get("take_lane_index")

        if view == "arrangement":
            if lane_index is None:
                owner, where = track, "this track"
            else:
                owner = self._resolve_take_lane(track, int(lane_index))
                where = "take lane %d" % int(lane_index)
            clips = list(owner.arrangement_clips)
            if not 0 <= index < len(clips):
                raise IndexError(
                    "arrangement clip %d does not exist on %s (there are %d)"
                    % (index, where, len(clips))
                )
            return clips[index]

        if lane_index is not None:
            raise ValueError(
                "take_lane_index only applies to Arrangement clips; pass view \"arrangement\""
            )
        slot = self._resolve_slot(track, index)
        if not slot.has_clip:
            raise ValueError("clip slot %d is empty" % index)
        return slot.clip

    @staticmethod
    def _take_lanes(track):
        if not hasattr(track, "take_lanes"):
            raise RuntimeError(
                "this Live version does not expose take lanes to scripts; "
                "Track.take_lanes needs a recent Live 12 release"
            )
        try:
            return list(track.take_lanes)
        except RuntimeError as e:
            raise ValueError("%r has no take lanes: %s" % (track.name, e))

    def _resolve_take_lane(self, track, index):
        lanes = self._take_lanes(track)
        if not 0 <= index < len(lanes):
            raise IndexError(
                "take lane %d does not exist on %r (there are %d)"
                % (index, track.name, len(lanes))
            )
        return lanes[index]

    @staticmethod
    def _resolve_slot(track, index):
        if not 0 <= index < len(track.clip_slots):
            raise IndexError(
                "clip slot %d does not exist (this track has %d)"
                % (index, len(track.clip_slots))
            )
        return track.clip_slots[index]

    def _resolve_device(self, params):
        """Find a device, descending into racks when chain_path is given."""
        track = self._resolve_track(params)
        index = int(params.get("device_index", 0))
        if not 0 <= index < len(track.devices):
            raise IndexError(
                "device %d does not exist on this track (there are %d)"
                % (index, len(track.devices))
            )
        device = track.devices[index]

        path = (params.get("chain_path") or "").strip()
        if not path:
            return device

        steps = [p for p in path.split(".") if p != ""]
        if len(steps) % 2 != 0:
            raise ValueError(
                "chain_path must be pairs of chain and device indices, e.g. \"0.2\""
            )
        for i in range(0, len(steps), 2):
            chain_index, device_index = int(steps[i]), int(steps[i + 1])
            chains = getattr(device, "chains", None)
            if not chains:
                raise ValueError("device %r is not a rack, so chain_path cannot descend"
                                 % device.name)
            if not 0 <= chain_index < len(chains):
                raise IndexError("chain %d does not exist (there are %d)"
                                 % (chain_index, len(chains)))
            chain = chains[chain_index]
            if not 0 <= device_index < len(chain.devices):
                raise IndexError("device %d does not exist in chain %d (there are %d)"
                                 % (device_index, chain_index, len(chain.devices)))
            device = chain.devices[device_index]
        return device

    def _resolve_parameter(self, params):
        """The (device, parameter) a parameter or envelope call refers to.

        device_index of -1 means the track's own mixer, whose parameters are
        laid out as volume, panning, the on/off switch, then the sends.
        """
        if int(params.get("device_index", 0)) == -1:
            track = self._resolve_track(params)
            mixer = track.mixer_device
            ordered = [mixer.volume, mixer.panning, mixer.track_activator]
            ordered.extend(list(mixer.sends))
            index = int(params.get("parameter_index", 0))
            if not 0 <= index < len(ordered):
                raise IndexError(
                    "mixer parameter %d does not exist; 0 volume, 1 panning, "
                    "2 track on/off, 3+ sends (%d available)" % (index, len(ordered))
                )
            return None, ordered[index]

        device = self._resolve_device(params)
        name = params.get("parameter_name")
        if name:
            wanted = str(name).strip().lower()
            for parameter in device.parameters:
                if parameter.name.strip().lower() == wanted:
                    return device, parameter
            raise ValueError(
                "device %r has no parameter named %r; it has: %s"
                % (device.name, name, ", ".join(p.name for p in device.parameters))
            )

        index = int(params.get("parameter_index", 0))
        if not 0 <= index < len(device.parameters):
            raise IndexError(
                "parameter %d does not exist on %r (it has %d)"
                % (index, device.name, len(device.parameters))
            )
        return device, device.parameters[index]

    @staticmethod
    def _apply(target, attribute, value, transform=None):
        """Write one optional property, reporting what it could not set."""
        if value is None:
            return None
        try:
            setattr(target, attribute, transform(value) if transform else value)
            return attribute
        except Exception as e:
            raise ValueError("could not set %s: %s" % (attribute, e))

    @staticmethod
    def _get(obj, attribute, default=None, cast=None):
        try:
            value = getattr(obj, attribute)
        except (AttributeError, RuntimeError):
            return default
        if cast is None:
            return value
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    # ── Introspection ────────────────────────────────────────────────────────

    def _describe_live_object(self, params, respond):
        """Report what a Live object actually offers, in this Live version.

        The Live API is not uniform across versions and editions, and guessing
        at it costs a Live restart each time. This turns that into a question
        that can be asked directly.
        """
        target = (params.get("target") or "song").lower()
        resolvers = {
            "song": lambda: self._song_ref,
            "song_view": lambda: self._song_ref.view,
            "application": lambda: self.application(),
            "track": lambda: self._resolve_track(params),
            "mixer": lambda: self._resolve_track(params).mixer_device,
            "clip_slot": lambda: self._resolve_slot(
                self._resolve_track(params), int(params.get("clip_index", 0))),
            "clip": lambda: self._resolve_clip(params),
            "device": lambda: self._resolve_device(params),
            "parameter": lambda: self._resolve_parameter(params)[1],
            "scene": lambda: self._scene_at(params.get("scene_index", 0)),
            "envelope": lambda: self._envelope_for(params)[2],
        }
        if target == "envelopes":
            return self._list_clip_envelopes(params)
        if target not in resolvers:
            raise ValueError(
                "unknown target %r; expected one of: %s"
                % (target, ", ".join(sorted(resolvers)))
            )

        obj = resolvers[target]()
        if obj is None:
            return {"target": target, "exists": False,
                    "note": "Live returned nothing for this object"}

        needle = (params.get("filter") or "").strip().lower()
        methods, properties = [], []
        for name in sorted(dir(obj)):
            if name.startswith("_"):
                continue
            if needle and needle not in name.lower():
                continue
            try:
                attribute = getattr(obj, name)
            except Exception:
                properties.append({"name": name, "type": "unreadable"})
                continue
            if callable(attribute):
                methods.append(name)
            else:
                properties.append({
                    "name": name,
                    "type": type(attribute).__name__,
                    "value": self._describe_value(attribute),
                })

        return {
            "target": target,
            "class": type(obj).__name__,
            "filter": params.get("filter"),
            "methods": methods,
            "properties": properties,
        }

    def _list_clip_envelopes(self, params):
        """Every automation envelope a clip carries, and what it automates.

        `automation_envelope(parameter)` cannot resolve the envelopes of a
        placed Arrangement clip, so this is the only way to see what one
        actually holds.
        """
        clip = self._resolve_clip(params)
        try:
            envelopes = list(clip.automation_envelopes)
        except (AttributeError, RuntimeError):
            envelopes = []

        listed = []
        for i, envelope in enumerate(envelopes):
            owner = self._get(envelope, "parameter")
            listed.append({
                "index": i,
                "parameter": self._get(owner, "name") if owner is not None else None,
                "parameter_min": self._get(owner, "min"),
                "parameter_max": self._get(owner, "max"),
                "writable": hasattr(envelope, "insert_step"),
                "value_at_start": self._safe_value_at(envelope, 1e-4),
                "members": [n for n in dir(envelope) if not n.startswith("_")],
            })

        return {
            "clip": clip.name,
            "is_arrangement_clip": self._get(clip, "is_arrangement_clip", False),
            "has_envelopes": self._get(clip, "has_envelopes", False),
            "count": len(listed),
            "envelopes": listed,
        }

    @staticmethod
    def _safe_value_at(envelope, time_value):
        try:
            return envelope.value_at_time(time_value)
        except Exception:
            return None

    @staticmethod
    def _describe_value(value):
        """A JSON-safe précis of a Live property's value."""
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, str):
            return value[:120]
        try:
            return "<%s of %d>" % (type(value).__name__, len(value))
        except TypeError:
            return "<%s>" % type(value).__name__

    # ── Song / transport ─────────────────────────────────────────────────────

    def _get_session_info(self, params, respond):
        song = self._song_ref
        return {
            "tempo": song.tempo,
            "signature_numerator": song.signature_numerator,
            "signature_denominator": song.signature_denominator,
            "is_playing": song.is_playing,
            "current_song_time": song.current_song_time,
            "song_length": self._get(song, "song_length"),
            "record_mode": self._get(song, "record_mode"),
            "session_record": self._get(song, "session_record"),
            "metronome": self._get(song, "metronome"),
            "loop": {
                "enabled": self._get(song, "loop"),
                "start": self._get(song, "loop_start"),
                "length": self._get(song, "loop_length"),
            },
            "clip_trigger_quantization": _name_of(
                LAUNCH_QUANTIZATION, self._get(song, "clip_trigger_quantization", 0)),
            "midi_recording_quantization": _name_of(
                RECORDING_QUANTIZATION, self._get(song, "midi_recording_quantization", 0)),
            "groove_amount": self._get(song, "groove_amount"),
            "scale": {
                "root_note": self._get(song, "root_note"),
                "name": self._get(song, "scale_name"),
                "enabled": self._get(song, "scale_mode"),
            },
            "can_undo": self._get(song, "can_undo"),
            "can_redo": self._get(song, "can_redo"),
            "track_count": len(song.tracks),
            "return_track_count": len(song.return_tracks),
            "scene_count": len(song.scenes),
            "locator_count": len(song.cue_points),
            "tracks": [
                {"index": i, "name": t.name, "type": self._track_kind(t),
                 "is_playing_slot": self._get(t, "playing_slot_index", -1)}
                for i, t in enumerate(song.tracks)
            ],
            "return_tracks": [
                {"index": i, "name": t.name} for i, t in enumerate(song.return_tracks)
            ],
            "scenes": [
                {"index": i, "name": s.name, "is_empty": self._get(s, "is_empty")}
                for i, s in enumerate(song.scenes)
            ],
            "live_version": self._live_version(),
        }

    @staticmethod
    def _track_kind(track):
        if getattr(track, "is_foldable", False):
            return "group"
        if getattr(track, "has_midi_input", False):
            return "midi"
        if getattr(track, "has_audio_input", False):
            return "audio"
        return "other"

    def _set_tempo(self, params, respond):
        tempo = float(params["tempo"])
        if not 20.0 <= tempo <= 999.0:
            raise ValueError("tempo must be between 20 and 999 BPM, got %s" % tempo)
        self._song_ref.tempo = tempo
        return {"tempo": self._song_ref.tempo}

    def _tap_tempo(self, params, respond):
        self._song_ref.tap_tempo()
        return {"tempo": self._song_ref.tempo}

    def _start_playback(self, params, respond):
        self._song_ref.start_playing()
        return {"is_playing": self._song_ref.is_playing}

    def _stop_playback(self, params, respond):
        self._song_ref.stop_playing()
        return {"is_playing": self._song_ref.is_playing}

    def _continue_playback(self, params, respond):
        self._song_ref.continue_playing()
        return {"is_playing": self._song_ref.is_playing}

    def _stop_all_clips(self, params, respond):
        self._song_ref.stop_all_clips()
        return {"stopped": True}

    def _set_current_song_time(self, params, respond):
        self._song_ref.current_song_time = max(0.0, float(params["time"]))
        return {"current_song_time": self._song_ref.current_song_time}

    def _set_loop_region(self, params, respond):
        song = self._song_ref
        changed = []
        # Length before start: Live clamps the start against the old length,
        # so setting the shorter one first can silently move the brace.
        if params.get("length") is not None:
            length = float(params["length"])
            if length <= 0:
                raise ValueError("loop length must be greater than 0")
            song.loop_length = length
            changed.append("length")
        if params.get("start") is not None:
            song.loop_start = max(0.0, float(params["start"]))
            changed.append("start")
        if params.get("enabled") is not None:
            song.loop = bool(params["enabled"])
            changed.append("enabled")
        return {"changed": changed, "start": song.loop_start,
                "length": song.loop_length, "enabled": song.loop}

    def _set_time_signature(self, params, respond):
        song = self._song_ref
        numerator = int(params["numerator"])
        denominator = int(params["denominator"])
        if not 1 <= numerator <= 99:
            raise ValueError("numerator must be 1-99, got %d" % numerator)
        if denominator not in (1, 2, 4, 8, 16, 32, 64):
            raise ValueError("denominator must be 1, 2, 4, 8, 16, 32 or 64, got %d" % denominator)
        song.signature_numerator = numerator
        song.signature_denominator = denominator
        return {"signature": "%d/%d" % (numerator, denominator)}

    def _set_transport_options(self, params, respond):
        song = self._song_ref
        changed = []
        for key in ("metronome", "record_mode", "session_record", "arrangement_overdub",
                    "session_automation_record", "punch_in", "punch_out"):
            if params.get(key) is not None:
                changed.append(self._apply(song, key, bool(params[key])))
        if params.get("follow_song") is not None:
            changed.append(self._apply(song.view, "follow_song", bool(params["follow_song"])))
        if params.get("back_to_arranger") is not None:
            changed.append(self._apply(song, "back_to_arranger", bool(params["back_to_arranger"])))
        if params.get("clip_trigger_quantization") is not None:
            song.clip_trigger_quantization = _index_of(
                LAUNCH_QUANTIZATION, params["clip_trigger_quantization"], "quantization")
            changed.append("clip_trigger_quantization")
        if params.get("midi_recording_quantization") is not None:
            song.midi_recording_quantization = _index_of(
                RECORDING_QUANTIZATION, params["midi_recording_quantization"], "quantization")
            changed.append("midi_recording_quantization")
        if params.get("groove_amount") is not None:
            song.groove_amount = max(0.0, min(1.0, float(params["groove_amount"])))
            changed.append("groove_amount")
        return {"changed": [c for c in changed if c]}

    def _undo(self, params, respond):
        if not self._song_ref.can_undo:
            raise RuntimeError("there is nothing to undo")
        self._song_ref.undo()
        return {"undone": True, "can_undo": self._song_ref.can_undo}

    def _redo(self, params, respond):
        if not self._song_ref.can_redo:
            raise RuntimeError("there is nothing to redo")
        self._song_ref.redo()
        return {"redone": True, "can_redo": self._song_ref.can_redo}

    def _capture_midi(self, params, respond):
        self._song_ref.capture_midi()
        return {"captured": True}

    def _set_scale(self, params, respond):
        song = self._song_ref
        changed = []
        if params.get("root_note") is not None:
            root = int(params["root_note"])
            if not 0 <= root <= 11:
                raise ValueError("root_note must be 0-11, got %d" % root)
            changed.append(self._apply(song, "root_note", root))
        if params.get("scale_name") is not None:
            changed.append(self._apply(song, "scale_name", str(params["scale_name"])))
        if params.get("scale_mode") is not None:
            changed.append(self._apply(song, "scale_mode", bool(params["scale_mode"])))
        return {"changed": [c for c in changed if c],
                "root_note": self._get(song, "root_note"),
                "scale_name": self._get(song, "scale_name")}

    def _set_view(self, params, respond):
        views = {
            "session": "Session",
            "arrangement": "Arranger",
            "detail_clip": "Detail/Clip",
            "detail_device": "Detail/DeviceChain",
            "browser": "Browser",
        }
        name = views.get(str(params["view"]).lower())
        if name is None:
            raise ValueError("unknown view %r" % params["view"])
        view = self.application().view
        if name.startswith("Detail/") and not view.is_view_visible("Detail"):
            view.show_view("Detail")
        view.show_view(name)
        return {"view": params["view"]}

    def _set_selection(self, params, respond):
        self._require_index_for_guard(params)
        song = self._song_ref
        view = song.view
        selected = {}

        if params.get("track_index") is not None:
            track = self._resolve_track({
                "track_index": params["track_index"],
                "expect_track_name": params.get("expect_track_name"),
            })
            view.selected_track = track
            selected["track"] = track.name
        if params.get("scene_index") is not None:
            index = int(params["scene_index"])
            if not 0 <= index < len(song.scenes):
                raise IndexError("scene %d does not exist" % index)
            view.selected_scene = song.scenes[index]
            selected["scene"] = index
        if params.get("clip_index") is not None:
            track = view.selected_track
            slot = self._resolve_slot(track, int(params["clip_index"]))
            view.highlighted_clip_slot = slot
            selected["clip_slot"] = int(params["clip_index"])
        if params.get("device_index") is not None:
            track = view.selected_track
            index = int(params["device_index"])
            if not 0 <= index < len(track.devices):
                raise IndexError("device %d does not exist on the selected track" % index)
            view.select_device(track.devices[index])
            selected["device"] = track.devices[index].name

        return {"selected": selected}

    # ── Scenes ───────────────────────────────────────────────────────────────

    def _get_scenes(self, params, respond):
        return {"scenes": [self._serialize_scene(s, i)
                           for i, s in enumerate(self._song_ref.scenes)]}

    def _serialize_scene(self, scene, index):
        return {
            "index": index,
            "name": scene.name,
            "color": self._get(scene, "color"),
            "is_empty": self._get(scene, "is_empty"),
            "is_triggered": self._get(scene, "is_triggered"),
            "tempo": self._get(scene, "tempo"),
            "is_tempo_enabled": self._get(scene, "is_tempo_enabled"),
            "time_signature_numerator": self._get(scene, "time_signature_numerator"),
            "time_signature_denominator": self._get(scene, "time_signature_denominator"),
            "is_time_signature_enabled": self._get(scene, "is_time_signature_enabled"),
        }

    def _scene_at(self, index):
        scenes = self._song_ref.scenes
        index = int(index)
        if not 0 <= index < len(scenes):
            raise IndexError("scene %d does not exist (there are %d)" % (index, len(scenes)))
        return scenes[index]

    def _create_scene(self, params, respond):
        self._song_ref.create_scene(int(params.get("index", -1)))
        return {"scene_count": len(self._song_ref.scenes)}

    def _delete_scene(self, params, respond):
        index = int(params["index"])
        self._scene_at(index)
        self._song_ref.delete_scene(index)
        return {"deleted": index, "scene_count": len(self._song_ref.scenes)}

    def _duplicate_scene(self, params, respond):
        index = int(params["index"])
        self._scene_at(index)
        self._song_ref.duplicate_scene(index)
        return {"duplicated": index, "scene_count": len(self._song_ref.scenes)}

    def _capture_and_insert_scene(self, params, respond):
        self._song_ref.capture_and_insert_scene()
        return {"scene_count": len(self._song_ref.scenes)}

    def _fire_scene(self, params, respond):
        scene = self._scene_at(params["index"])
        scene.fire()
        return {"fired": params["index"], "name": scene.name}

    def _set_scene_properties(self, params, respond):
        scene = self._scene_at(params["index"])
        changed = []
        for key, attribute, cast in (
            ("name", "name", str),
            ("color", "color", int),
            ("tempo", "tempo", float),
            ("is_tempo_enabled", "is_tempo_enabled", bool),
            ("time_signature_numerator", "time_signature_numerator", int),
            ("time_signature_denominator", "time_signature_denominator", int),
            ("is_time_signature_enabled", "is_time_signature_enabled", bool),
        ):
            if params.get(key) is not None:
                changed.append(self._apply(scene, attribute, params[key], cast))
        return {"changed": [c for c in changed if c],
                "scene": self._serialize_scene(scene, int(params["index"]))}

    # ── Tracks ───────────────────────────────────────────────────────────────

    def _get_tracks(self, params, respond):
        include_devices = params.get("include_devices", True)
        include_clips = params.get("include_clips", False)
        song = self._song_ref
        return {
            "tracks": [self._serialize_track(t, i, "regular", include_devices, include_clips)
                       for i, t in enumerate(song.tracks)],
            "return_tracks": [self._serialize_track(t, i, "return", include_devices, False)
                              for i, t in enumerate(song.return_tracks)],
            "master_track": self._serialize_track(
                song.master_track, 0, "master", include_devices, False),
        }

    def _serialize_track(self, track, index, track_type, include_devices=True,
                         include_clips=False):
        mixer = track.mixer_device
        info = {
            "index": index,
            "track_type": track_type,
            "name": track.name,
            "kind": self._track_kind(track),
            "color": self._get(track, "color"),
            "mute": self._get(track, "mute"),
            "solo": self._get(track, "solo"),
            "arm": self._get(track, "arm"),
            "can_be_armed": self._get(track, "can_be_armed", False),
            "volume": self._get(mixer.volume, "value"),
            "volume_display": self._display_value(mixer.volume),
            "panning": self._get(mixer.panning, "value"),
            "is_grouped": self._get(track, "is_grouped", False),
            "is_foldable": self._get(track, "is_foldable", False),
            "fold_state": self._get(track, "fold_state"),
            "playing_slot_index": self._get(track, "playing_slot_index", -1),
            "sends": [
                {"index": i, "value": send.value, "display": self._display_value(send)}
                for i, send in enumerate(mixer.sends)
            ],
        }
        if track_type == "master":
            info["crossfader"] = self._get(getattr(mixer, "crossfader", None), "value")
            info["cue_volume"] = self._get(getattr(mixer, "cue_volume", None), "value")
        else:
            info["crossfade_assign"] = _name_of(
                CROSSFADE_ASSIGN, self._get(mixer, "crossfade_assign", 1))
            info["monitoring_state"] = _name_of(
                MONITORING_STATE, self._get(track, "current_monitoring_state", 1))

        if include_devices:
            info["devices"] = [self._serialize_device(d, i, include_params=False)
                               for i, d in enumerate(track.devices)]
        if include_clips:
            info["clip_slots"] = [
                {"index": i,
                 "has_clip": slot.has_clip,
                 "name": slot.clip.name if slot.has_clip else None,
                 "length": slot.clip.length if slot.has_clip else None,
                 "is_playing": self._get(slot, "is_playing", False)}
                for i, slot in enumerate(track.clip_slots)
            ]
        return info

    @staticmethod
    def _display_value(parameter):
        """How Live shows a parameter, "-6.0 dB" rather than 0.63."""
        try:
            return parameter.str_for_value(parameter.value)
        except Exception:
            return None

    def _get_track_info(self, params, respond):
        track = self._resolve_track(params)
        track_type = (params.get("track_type") or "regular").lower()
        info = self._serialize_track(
            track, int(params.get("track_index", 0)), track_type,
            include_devices=True, include_clips=True)
        if track_type != "master":
            info["arrangement_clips"] = [
                self._serialize_clip(c, i, brief=True)
                for i, c in enumerate(track.arrangement_clips)
            ]
        return info

    def _create_midi_track(self, params, respond):
        index = int(params.get("index", -1))
        self._song_ref.create_midi_track(index)
        created = self._song_ref.tracks[index if index >= 0 else -1]
        return {"name": created.name, "track_count": len(self._song_ref.tracks)}

    def _create_audio_track(self, params, respond):
        index = int(params.get("index", -1))
        self._song_ref.create_audio_track(index)
        created = self._song_ref.tracks[index if index >= 0 else -1]
        return {"name": created.name, "track_count": len(self._song_ref.tracks)}

    def _create_return_track(self, params, respond):
        self._song_ref.create_return_track()
        created = self._song_ref.return_tracks[-1]
        return {"name": created.name,
                "return_track_count": len(self._song_ref.return_tracks)}

    def _delete_track(self, params, respond):
        track_type = (params.get("track_type") or "regular").lower()
        index = int(params.get("track_index", 0))
        track = self._resolve_track(params)
        name = track.name
        if track_type == "master":
            raise ValueError("the master track cannot be deleted")
        if track_type == "return":
            self._song_ref.delete_return_track(index)
        else:
            self._song_ref.delete_track(index)
        return {"deleted": name}

    def _duplicate_track(self, params, respond):
        if (params.get("track_type") or "regular").lower() != "regular":
            raise ValueError("only regular tracks can be duplicated")
        index = int(params.get("track_index", 0))
        self._resolve_track(params)
        self._song_ref.duplicate_track(index)
        return {"duplicated": index, "track_count": len(self._song_ref.tracks)}

    def _set_track_name(self, params, respond):
        track = self._resolve_track(params)
        previous = track.name
        track.name = str(params["name"])
        return {"from": previous, "to": track.name}

    def _set_track_mixer(self, params, respond):
        track = self._resolve_track(params)
        mixer = track.mixer_device
        changed = []

        if params.get("volume") is not None and params.get("volume_db") is not None:
            raise ValueError("give volume or volume_db, not both")
        volume_target = None
        if params.get("volume") is not None:
            mixer.volume.value = self._clamp_parameter(mixer.volume, params["volume"])
            changed.append("volume")
        if params.get("volume_db") is not None:
            volume_target = self._set_by_display(
                mixer.volume, "%s dB" % float(params["volume_db"]))
            changed.append("volume")
        if params.get("panning") is not None:
            mixer.panning.value = self._clamp_parameter(mixer.panning, params["panning"])
            changed.append("panning")
        for key in ("mute", "solo", "arm"):
            if params.get(key) is not None:
                if key == "arm" and not self._get(track, "can_be_armed", False):
                    raise ValueError("this track cannot be armed")
                changed.append(self._apply(track, key, bool(params[key])))
        if params.get("color") is not None:
            changed.append(self._apply(track, "color", int(params["color"])))
        if params.get("folded") is not None:
            if not self._get(track, "is_foldable", False):
                raise ValueError("only group tracks can be folded")
            changed.append(self._apply(track, "fold_state", bool(params["folded"])))
        if params.get("crossfade_assign") is not None:
            mixer.crossfade_assign = _index_of(
                CROSSFADE_ASSIGN, params["crossfade_assign"], "crossfade assignment")
            changed.append("crossfade_assign")
        if params.get("monitoring_state") is not None:
            track.current_monitoring_state = _index_of(
                MONITORING_STATE, params["monitoring_state"], "monitoring state")
            changed.append("monitoring_state")

        result = {
            "changed": [c for c in changed if c],
            "volume": mixer.volume.value,
            "volume_display": self._display_value(mixer.volume),
            "panning": mixer.panning.value,
        }
        if volume_target is not None:
            result["volume_target"] = volume_target["target"]
            result["volume_error_db"] = volume_target.get("error")
        return result

    @staticmethod
    def _clamp_parameter(parameter, value):
        """Keep writes inside the parameter's range, Live raises otherwise."""
        return max(parameter.min, min(parameter.max, float(value)))

    def _value_for_display(self, parameter, wanted):
        """Find the raw value Live displays as `wanted`, in one main-thread pass.

        Quantized parameters match their named settings. Otherwise the display
        is read through str_for_value: if it is monotonic across the range the
        target is bisected, else the range is scanned for the closest value.
        """
        text = str(wanted).strip()
        lo, hi = float(parameter.min), float(parameter.max)
        quantized = bool(self._get(parameter, "is_quantized", False))
        items = []
        if quantized:
            items = [str(i) for i in (self._get(parameter, "value_items") or [])]
            for i, item in enumerate(items):
                if item.strip().lower() == text.lower():
                    return self._display_outcome(parameter, lo + i, text, None, "value_items")

        target = parse_display(text)
        if target is None:
            if items:
                raise ValueError("%r is not one of %s's settings: %s"
                                 % (text, parameter.name, ", ".join(items)))
            raise ValueError("could not read %r as a number with a unit, e.g. \"220 Hz\", "
                             "\"-6 dB\", \"3 ms\", \"2:1\"" % text)
        goal, unit = target

        def measure(x):
            try:
                parsed = parse_display(parameter.str_for_value(x))
            except Exception:
                return None
            if parsed is None or not _units_compatible(unit, parsed[1]):
                return None
            return parsed[0]

        if quantized:
            candidates = [lo + i for i in range(int(round(hi - lo)) + 1)]
            return self._closest(parameter, measure, candidates, goal, text, "scan")

        grid = [lo + (hi - lo) * i / float(DISPLAY_GRID) for i in range(DISPLAY_GRID + 1)]
        points = [(x, measure(x)) for x in grid]
        points = [(x, y) for x, y in points if y is not None and not math.isnan(y)]
        if len(points) < 2:
            raise ValueError("%s is shown as %r, which cannot be compared with %r"
                             % (parameter.name, self._display_value(parameter), text))
        steps = [b[1] - a[1] for a, b in zip(points, points[1:])]
        steps = [d for d in steps if not math.isnan(d)]
        rising = all(d >= 0 for d in steps)
        falling = all(d <= 0 for d in steps)
        if points[0][1] == points[-1][1] or not (rising or falling):
            dense = [lo + (hi - lo) * i / float(DISPLAY_SCAN) for i in range(DISPLAY_SCAN + 1)]
            return self._closest(parameter, measure, dense, goal, text, "scan")
        value = self._bisect_display(measure, points, goal, rising)
        return self._display_outcome(parameter, value, text, (goal, measure(value)), "bisection")

    @staticmethod
    def _bisect_display(measure, points, goal, rising):
        sign = 1.0 if rising else -1.0
        if math.isinf(goal):
            lowest, highest = sorted([points[0], points[-1]], key=lambda p: p[1])
            return lowest[0] if goal < 0 else highest[0]
        if sign * (goal - points[0][1]) <= 0:
            return points[0][0]
        if sign * (goal - points[-1][1]) >= 0:
            return points[-1][0]
        lo, hi = points[0][0], points[-1][0]
        for (xa, ya), (xb, yb) in zip(points, points[1:]):
            if sign * (ya - goal) <= 0 <= sign * (yb - goal):
                lo, hi = xa, xb
                break
        for _ in range(DISPLAY_BISECTIONS):
            mid = (lo + hi) / 2.0
            y = measure(mid)
            if y is None:
                break
            if sign * (y - goal) < 0:
                lo = mid
            else:
                hi = mid

        def miss(x):
            y = measure(x)
            return float("inf") if y is None else abs(y - goal)

        return lo if miss(lo) < miss(hi) else hi

    def _closest(self, parameter, measure, candidates, goal, text, method):
        best, best_miss = None, None
        for x in candidates:
            y = measure(x)
            if y is None:
                continue
            miss = 0.0 if y == goal else abs(y - goal)
            if best_miss is None or miss < best_miss:
                best, best_miss = x, miss
        if best is None:
            raise ValueError("%s is shown as %r, which cannot be compared with %r"
                             % (parameter.name, self._display_value(parameter), text))
        return self._display_outcome(parameter, best, text, (goal, measure(best)), method)

    def _display_outcome(self, parameter, value, text, numbers, method):
        value = self._clamp_parameter(parameter, value)
        try:
            shown = parameter.str_for_value(value)
        except Exception:
            shown = None
        outcome = {"value": value, "target": text, "achieved": shown, "method": method}
        if numbers is not None and numbers[1] is not None:
            outcome["error"] = _display_error(numbers[1], numbers[0])
        return outcome

    def _set_by_display(self, parameter, wanted):
        """Set a parameter to the value Live displays as `wanted`."""
        outcome = self._value_for_display(parameter, wanted)
        parameter.value = outcome["value"]
        outcome["value"] = parameter.value
        return outcome

    def _set_track_send(self, params, respond):
        track = self._resolve_track(params)
        sends = track.mixer_device.sends
        index = int(params["send_index"])
        if not 0 <= index < len(sends):
            raise IndexError(
                "send %d does not exist; this set has %d return track(s)"
                % (index, len(sends))
            )
        if params.get("value_db") is not None:
            outcome = self._set_by_display(sends[index], "%s dB" % float(params["value_db"]))
            return {"send_index": index, "value": sends[index].value,
                    "display": outcome["achieved"], "target": outcome["target"],
                    "error_db": outcome.get("error")}
        if params.get("value") is None:
            raise ValueError("give value or value_db")
        sends[index].value = self._clamp_parameter(sends[index], params["value"])
        return {"send_index": index, "value": sends[index].value,
                "display": self._display_value(sends[index])}

    # ── Meters ───────────────────────────────────────────────────────────────
    # Sampling happens here, on the main thread, one pass over every meter per
    # sample, spread across ticks with _defer so Live keeps running between
    # samples. A loop in the server would pay a socket round trip and a
    # scheduling tick per sample, and its timestamps would be the client's.

    def _now(self):
        return time.time()

    def _meter_sources(self):
        song = self._song_ref
        sources = [("regular", i, t) for i, t in enumerate(song.tracks)]
        sources += [("return", i, t) for i, t in enumerate(song.return_tracks)]
        if song.master_track is not None:
            sources.append(("master", 0, song.master_track))
        return sources

    def _read_meters(self, sources):
        """Every meter in one main-thread pass, as (level, left, right) rows."""
        return [
            (self._get(track, "output_meter_level", 0.0, float) or 0.0,
             self._get(track, "output_meter_left", 0.0, float) or 0.0,
             self._get(track, "output_meter_right", 0.0, float) or 0.0)
            for _, _, track in sources
        ]

    def _sample_meters(self, interval, keep_going, finish, fail):
        """Sample every meter each `interval` seconds until keep_going says stop.

        Samples are (wall time, song time, meter rows).
        """
        sources = self._meter_sources()
        ticks = max(1, int(round(interval / METER_TICK_SECONDS)))
        samples = []

        def sample():
            try:
                samples.append((self._now(), self._song_ref.current_song_time,
                                self._read_meters(sources)))
                if keep_going(samples):
                    self._defer(ticks, sample)
                else:
                    finish(sources, samples)
            except Exception as e:
                fail(e)

        sample()

    @staticmethod
    def _meter_interval(params):
        interval = float(params.get("interval") or METER_TICK_SECONDS)
        if interval <= 0:
            raise ValueError("interval must be positive")
        return max(interval, METER_TICK_SECONDS)

    def _group_of(self, track, sources):
        """(group name, its position in sources) for a grouped track, else None.

        Live 12.4 reads 0 on the meters of tracks inside a group while the
        group track meters their sum (issue #3).
        """
        if not self._get(track, "is_grouped", False):
            return None
        group = self._get(track, "group_track")
        if group is None:
            return None
        position = next((k for k, (_, _, t) in enumerate(sources) if t == group), None)
        return self._get(group, "name", ""), position

    def _grouped_note(self, sources, samples, tracks, entries):
        """Mark grouped tracks and list those whose meter read 0 throughout.

        `entries` are the report rows in sources order. Returns the list for the
        report's `grouped` key, each with its group's level over the samples.
        """
        unread = []
        for k, (_, _, track) in enumerate(sources):
            group = self._group_of(track, sources)
            if group is None:
                continue
            entries[k]["grouped"] = True
            entries[k]["group"] = group[0]
            if any(row[k][0] > 0 for _, _, row in samples):
                continue
            entries[k]["meter"] = "unavailable"
            item = {"name": entries[k]["name"], "group": group[0]}
            if group[1] is not None and samples:
                levels = [row[group[1]][0] for _, _, row in samples]
                item["group_avg"] = round(sum(levels) / len(levels), 3)
                item["group_peak"] = round(max(levels), 3)
            unread.append(item)
        return unread

    GROUPED_NOTE = ("Live reads 0 on the meters of these grouped tracks; their sound is in "
                    "the group track's meter (group_avg, group_peak), so a 0 here is not "
                    "silence and they are not classified.")

    def _meter_stats(self, sources, samples, column):
        stats = []
        for k, (kind, index, track) in enumerate(sources):
            rows = [row[k] for _, _, row in samples]
            levels = [r[0] for r in rows]
            entry = {"type": kind, "index": index, "name": self._get(track, "name", "")}
            if levels:
                entry[column + "min"] = round(min(levels), 3)
                entry[column + "avg"] = round(sum(levels) / len(levels), 3)
                entry[column + "peak"] = round(max(levels), 3)
                if not column:
                    entry["peak_left"] = round(max(r[1] for r in rows), 3)
                    entry["peak_right"] = round(max(r[2] for r in rows), 3)
            if kind == "regular" and self._get(track, "has_audio_output", True) is False:
                entry["has_audio_output"] = False
            stats.append(entry)
        return stats

    def _get_meters(self, params, respond):
        duration = float(params.get("duration") or 2.0)
        if not 0 < duration <= MAX_METER_SECONDS:
            raise ValueError("duration must be between 0 and %d seconds" % MAX_METER_SECONDS)
        interval = self._meter_interval(params)
        if respond is None:
            raise RuntimeError("get_meters samples over time and cannot run inside a batch")
        song = self._song_ref
        started = self._now()

        def keep_going(samples):
            return samples[-1][0] - started < duration

        def finish(sources, samples):
            times = [t for t, _, _ in samples]
            gaps = [b - a for a, b in zip(times, times[1:])]
            tracks = self._meter_stats(sources, samples, "")
            grouped = self._grouped_note(sources, samples, tracks, tracks)
            report = {
                "samples": len(samples),
                "seconds": round(times[-1] - times[0], 3),
                "interval": round(sum(gaps) / len(gaps), 3) if gaps else None,
                "song_time": {"start": samples[0][1], "end": samples[-1][1]},
                "is_playing": bool(song.is_playing),
                "tracks": tracks,
                "scale": "Live's meter scale, 0.0 to 1.0, not dBFS. The meters hold "
                         "peaks briefly, so avg reads high.",
            }
            if grouped:
                report["grouped"] = grouped
                report["note"] = self.GROUPED_NOTE
            if not song.is_playing:
                report["warning"] = ("the transport is stopped, so the meters show only "
                                     "input monitoring and tails")
            respond({"ok": report})

        self._sample_meters(interval, keep_going, finish,
                            lambda e: respond({"error": str(e)}))
        return DEFERRED

    @staticmethod
    def _probe_options(params):
        """Optional probe_entry tuning, with the defaults tuned on issue #3's cases."""
        options = {
            "silent": float(params.get("silent") if params.get("silent") is not None
                            else ENTRY_SILENT),
            "loud": float(params.get("loud") if params.get("loud") is not None
                          else ENTRY_LOUD),
            "rise": float(params.get("rise") if params.get("rise") is not None
                          else ENTRY_RISE),
            "persist": float(params.get("persist") if params.get("persist") is not None
                             else ENTRY_PERSIST_BEATS),
            "dip": float(params.get("dip") if params.get("dip") is not None
                         else ENTRY_DIP_BEATS),
            "dip_depth": float(params.get("dip_depth") if params.get("dip_depth") is not None
                               else ENTRY_DIP_DEPTH),
            "lag": float(params.get("lag") if params.get("lag") is not None
                         else ENTRY_LAG_BEATS),
            "decay": float(params.get("decay") if params.get("decay") is not None
                           else METER_DECAY_SECONDS),
        }
        if not 0 <= options["silent"] < options["loud"] <= 1:
            raise ValueError("need 0 <= silent < loud <= 1")
        if options["rise"] <= 0 or options["persist"] < 0 or options["dip"] <= 0:
            raise ValueError("rise and dip must be positive and persist not negative")
        if not 0 < options["dip_depth"] < 1 or options["lag"] < 0:
            raise ValueError("dip_depth must be between 0 and 1 and lag not negative")
        if not 0 <= options["decay"] <= 5:
            raise ValueError("decay must be between 0 and 5 seconds")
        return options

    def _probe_entry(self, params, respond):
        """Play across `beat` and report which tracks get louder there."""
        song = self._song_ref
        beat = float(params["beat"])
        preroll = float(params.get("preroll") or 5.0)
        after = float(params.get("after") or 8.0)
        if beat < 0 or preroll <= 0 or after <= 0:
            raise ValueError("beat must not be negative; preroll and after must be positive")
        options = self._probe_options(params)
        interval = self._meter_interval(params)
        if respond is None:
            raise RuntimeError("probe_entry plays the set over time and cannot run inside "
                               "a batch")
        start = max(0.0, beat - preroll)
        end = beat + after
        decay_beats = options["decay"] * float(song.tempo) / 60.0
        # The meters still hold what was playing before the jump for about as
        # long as they take to fall after a track stops, so the before window
        # skips that much (but never more than half the preroll).
        settle = min(decay_beats, (beat - start) / 2.0)
        budget = min(MAX_PROBE_SECONDS, (end - start) * 60.0 / float(song.tempo) * 2.0 + 3.0)
        original = song.current_song_time
        was_playing = bool(song.is_playing)
        state = {"ticks": 0}

        def restore():
            if not was_playing:
                song.stop_playing()
            song.current_song_time = original

        def fail(e):
            try:
                restore()
            finally:
                respond({"error": str(e)})

        def finish(sources, samples):
            restore()
            report = self._probe_report(sources, samples, beat, start, start + settle, end,
                                        decay_beats, options)
            report["restored_to"] = original
            if state.get("warning"):
                report["warning"] = state["warning"]
            respond({"ok": report})

        def positioned():
            state["ticks"] += 1
            now = song.current_song_time
            if not start - 1e-3 <= now < start + max(1.0, settle):
                if state["ticks"] > MAX_DEFER_TICKS:
                    fail(RuntimeError("Live would not move the playhead to beat %s" % start))
                    return
                self._defer(1, positioned)
                return
            if not song.is_playing:
                song.continue_playing()
            started = self._now()

            def keep_going(samples):
                now_wall, song_time, _ = samples[-1]
                if song_time >= end:
                    return False
                if now_wall - started > budget:
                    state["warning"] = "stopped after %.1f s before reaching beat %s" % (
                        budget, end)
                    return False
                if len(samples) > 3 and not song.is_playing:
                    state["warning"] = "playback stopped during the probe"
                    return False
                return True

            self._sample_meters(interval, keep_going, finish, fail)

        song.current_song_time = start
        self._defer(1, positioned)
        return DEFERRED

    @staticmethod
    def _longest_run(points, test, step):
        """Longest stretch, in beats, of consecutive (song_time, level) points passing test."""
        best, first, last = 0.0, None, None
        for song_time, level in points:
            if test(level):
                if first is None:
                    first = song_time
                last = song_time
                best = max(best, last - first + step)
            else:
                first = None
        return best

    @staticmethod
    def _relative_drop(trace_beats, mins, beat, options):
        """Find a dropout in a track's per-beat minimum levels, relative to its own level.

        Live's meters fall slowly, so a short dropout never reaches silence: it
        shows as a fall in the per-beat minimum (issue #3). The reference is the
        median of the minimums in the beats before `beat`: before_min would be
        the dip itself when meter lag puts it a beat early, and before_avg is
        pulled up by held peaks. A fall of `dip_depth` below it, starting no
        more than `lag` beats before the beat and lasting `dip` beats, is
        "dipped" if it recovers in the window and "left" if it does not.

        Returns None, or a dict with kind, at, beats, reference, low and fall.
        """
        depth, dip, lag = options["dip_depth"], options["dip"], options["lag"]
        points = [(b, m) for b, m in zip(trace_beats, mins) if m is not None]
        before = sorted(m for b, m in points if b + 1 <= beat)
        if len(before) < 2:
            return None
        middle = len(before) // 2
        reference = (before[middle] if len(before) % 2
                     else (before[middle - 1] + before[middle]) / 2.0)
        if reference < options["silent"]:
            return None
        floor = reference * (1.0 - depth)
        lead = [m for b, m in points if b + 1 <= beat - lag]
        # A meter still falling from before the jump is a track that already stopped.
        if (len(lead) >= 2 and lead[-1] <= lead[0] * (1.0 - depth)
                and all(y <= x + 0.01 for x, y in zip(lead, lead[1:]))):
            return None
        # A track that already fell this far well before the beat (past the first
        # beat, which can still be settling after the jump) is a pattern, not a dropout.
        if any(m < floor for m in lead[1:]):
            return None
        runs, run = [], []
        for b, m in points:
            if m < floor:
                run.append((b, m))
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        last = points[-1][0]
        found = None
        for run in runs:
            if run[0][0] < beat - lag or len(run) < dip:
                continue
            low = min(m for _, m in run)
            drop = {"kind": "left" if run[-1][0] == last else "dipped", "at": run[0][0],
                    "beats": len(run), "reference": round(reference, 3), "low": low,
                    "fall": round(1.0 - low / reference, 3)}
            if drop["kind"] == "left":
                return drop
            if found is None or drop["fall"] > found["fall"]:
                found = drop
        return found

    def _probe_report(self, sources, samples, beat, start, before_from, end, decay_beats,
                      options):
        """Classify each track's change at `beat` from its meter samples.

        Live's meters hold peaks and fall slowly, so averages blur short events
        (issue #3). An entry or rise only counts when it holds for `persist`
        beats beyond the meter's own `decay`, since one sparse hit keeps the
        meter up for about that long. Dips and exits are judged relative to the
        track's own level by _relative_drop.
        """
        silent, loud, rise = options["silent"], options["loud"], options["rise"]
        hold = options["persist"] + decay_beats
        before = [s for s in samples if before_from <= s[1] < beat]
        after = [s for s in samples if beat <= s[1] <= end + 0.5]
        song_times = [s[1] for s in samples]
        gaps = [b - a for a, b in zip(song_times, song_times[1:]) if b > a]
        step = sum(gaps) / len(gaps) if gaps else 0.0
        first_beat = int(math.floor(start))
        trace_beats = list(range(first_beat, int(math.ceil(end))))

        def levels(window, k):
            return [(s[1], s[2][k][0]) for s in window]

        def summary(points):
            values = [level for _, level in points]
            if not values:
                return {}
            return {"min": round(min(values), 3), "avg": round(sum(values) / len(values), 3),
                    "peak": round(max(values), 3)}

        entries = []
        for k, (kind, index, track) in enumerate(sources):
            entry = {"type": kind, "index": index, "name": self._get(track, "name", "")}
            for prefix, points in (("before_", levels(before, k)), ("after_", levels(after, k))):
                for key, value in summary(points).items():
                    entry[prefix + key] = value
            all_points = levels(samples, k)
            peaks, mins = [], []
            for b in trace_beats:
                bucket = [level for t, level in all_points if b <= t < b + 1]
                peaks.append(round(max(bucket), 3) if bucket else None)
                mins.append(round(min(bucket), 3) if bucket else None)
            entry["trace_peak"], entry["trace_min"] = peaks, mins
            entries.append(entry)
        grouped = self._grouped_note(sources, samples, entries, entries)

        entered, rose, dipped, left = [], [], [], []
        for k, (kind, index, track) in enumerate(sources):
            entry = entries[k]
            label = entry["name"] if kind == "regular" else "%s (%s)" % (entry["name"], kind)
            b_points, a_points = levels(before, k), levels(after, k)
            if entry.get("meter") == "unavailable" or not (b_points and a_points):
                continue
            b_values = [level for _, level in b_points]
            b_avg, b_peak = sum(b_values) / len(b_values), max(b_values)
            entry["change"] = round(entry["after_avg"] - entry["before_avg"], 3)
            loud_after = self._longest_run(a_points, lambda v: v >= loud, step)
            above_before = self._longest_run(a_points, lambda v: v >= b_avg + rise, step)
            drop = self._relative_drop(trace_beats, entry["trace_min"], beat, options)
            # A shorter `after` than the hold can still show an entry that lasts all of it.
            if b_peak < silent and loud_after >= min(hold, end - beat):
                entry["entered"] = True
                entered.append(label)
            elif entry["change"] >= rise and above_before >= hold:
                entry["rose"] = True
                rose.append(label)
            elif drop is not None:
                entry[drop["kind"]] = True
                entry["drop"] = {key: drop[key]
                                 for key in ("at", "beats", "reference", "low", "fall")}
                (left if drop["kind"] == "left" else dipped).append(label)
        tracks = sorted(entries, key=lambda t: -abs(t.get("change", 0.0)))
        report = {
            "beat": beat,
            "song_time": {"start": samples[0][1], "end": samples[-1][1]},
            "samples": {"before": len(before), "after": len(after)},
            "entered": entered,
            "rose": rose,
            "dipped": dipped,
            "left": left,
            "trace_beats": trace_beats,
            "tracks": tracks,
            "options": options,
            "scale": (
                "Live's meter scale, 0.0 to 1.0; the meters hold peaks and fall slowly "
                "(decay assumes %(decay)g s from about 0.8 to 0.1). trace_peak and "
                "trace_min are each track's highest and lowest reading in each beat of "
                "trace_beats. Entered: under %(silent)g before, then at least %(loud)g for "
                "%(persist)g beats beyond the decay (or all of a shorter window). Rose: "
                "average up %(rise)g and held as long. Dipped: trace_min fell at least %(dip_depth)g (as a fraction) below "
                "the median trace_min before the beat, for %(dip)g beats or more, starting "
                "no more than %(lag)g beats before the beat, then recovered. Left: the same "
                "fall without recovery by the end of the window. drop.at is the first beat "
                "of the fall." % options),
        }
        if grouped:
            report["grouped"] = grouped
            report["note"] = self.GROUPED_NOTE
        if not before:
            report["warning"] = "no samples before the beat; increase preroll"
        return report

    def _get_track_routing(self, params, respond):
        track = self._resolve_track(params)

        def describe(routing):
            return {"name": self._get(routing, "display_name")} if routing else None

        def options(attribute):
            try:
                return [r.display_name for r in getattr(track, attribute)]
            except (AttributeError, RuntimeError):
                return []

        return {
            "input_type": describe(self._get(track, "input_routing_type")),
            "input_channel": describe(self._get(track, "input_routing_channel")),
            "output_type": describe(self._get(track, "output_routing_type")),
            "output_channel": describe(self._get(track, "output_routing_channel")),
            "monitoring_state": _name_of(
                MONITORING_STATE, self._get(track, "current_monitoring_state", 1)),
            "available_input_types": options("available_input_routing_types"),
            "available_input_channels": options("available_input_routing_channels"),
            "available_output_types": options("available_output_routing_types"),
            "available_output_channels": options("available_output_routing_channels"),
        }

    def _set_track_routing(self, params, respond):
        track = self._resolve_track(params)
        changed = []

        # Routing is set by assigning one of Live's own routing objects, not a
        # string, so each name has to be matched back to the object it names.
        pairs = (
            ("input_type", "available_input_routing_types", "input_routing_type"),
            ("input_channel", "available_input_routing_channels", "input_routing_channel"),
            ("output_type", "available_output_routing_types", "output_routing_type"),
            ("output_channel", "available_output_routing_channels", "output_routing_channel"),
        )
        for key, available_attribute, target_attribute in pairs:
            wanted = params.get(key)
            if wanted is None:
                continue
            try:
                available = list(getattr(track, available_attribute))
            except (AttributeError, RuntimeError):
                raise ValueError("this track has no %s" % key)

            match = None
            for option in available:
                if option.display_name.strip().lower() == str(wanted).strip().lower():
                    match = option
                    break
            if match is None:
                raise ValueError(
                    "%r is not a valid %s here; available: %s"
                    % (wanted, key, ", ".join(o.display_name for o in available))
                )
            setattr(track, target_attribute, match)
            changed.append(key)

        # Channels are re-derived when the type changes, so read back rather
        # than reporting what was asked for.
        return {"changed": changed, "routing": self._get_track_routing(params, None)}

    # ── Clips ────────────────────────────────────────────────────────────────

    def _serialize_clip(self, clip, index, brief=False):
        info = {
            "index": index,
            "name": clip.name,
            "color": self._get(clip, "color"),
            "length": self._get(clip, "length"),
            "is_midi": self._get(clip, "is_midi_clip", False),
            "is_audio": self._get(clip, "is_audio_clip", False),
            "is_arrangement_clip": self._get(clip, "is_arrangement_clip", False),
        }
        if info["is_arrangement_clip"]:
            info["start_time"] = self._get(clip, "start_time")
            info["end_time"] = self._get(clip, "end_time")
        if brief:
            return info

        info.update({
            "muted": self._get(clip, "muted"),
            "looping": self._get(clip, "looping"),
            "loop_start": self._get(clip, "loop_start"),
            "loop_end": self._get(clip, "loop_end"),
            "start_marker": self._get(clip, "start_marker"),
            "end_marker": self._get(clip, "end_marker"),
            "signature_numerator": self._get(clip, "signature_numerator"),
            "signature_denominator": self._get(clip, "signature_denominator"),
            "is_playing": self._get(clip, "is_playing"),
            "playing_position": self._get(clip, "playing_position"),
            "launch_mode": _name_of(LAUNCH_MODE, self._get(clip, "launch_mode", 0)),
            "launch_quantization": _name_of(
                ["global"] + LAUNCH_QUANTIZATION, self._get(clip, "launch_quantization", 0)),
            "legato": self._get(clip, "legato"),
            "velocity_amount": self._get(clip, "velocity_amount"),
            "follow_action_enabled": self._get(clip, "follow_action_enabled"),
            "follow_time": self._get(clip, "follow_time"),
            "follow_action_a": _name_of(FOLLOW_ACTION, self._get(clip, "follow_action_a", 0)),
            "follow_action_b": _name_of(FOLLOW_ACTION, self._get(clip, "follow_action_b", 0)),
            "follow_action_chance_a": self._get(clip, "follow_action_chance_a"),
            "follow_action_chance_b": self._get(clip, "follow_action_chance_b"),
        })
        if info["is_audio"]:
            warp_names = {v: k for k, v in WARP_MODE.items()}
            info.update({
                "warping": self._get(clip, "warping"),
                "warp_mode": warp_names.get(self._get(clip, "warp_mode"), None),
                "gain": self._get(clip, "gain"),
                "pitch_coarse": self._get(clip, "pitch_coarse"),
                "pitch_fine": self._get(clip, "pitch_fine"),
                "ram_mode": self._get(clip, "ram_mode"),
                "file_path": self._get(clip, "file_path"),
            })
        if info["is_midi"]:
            info["note_count"] = len(self._notes_from_clip(clip))
        return info

    def _get_clip(self, params, respond):
        clip = self._resolve_clip(params)
        return self._serialize_clip(clip, int(params.get("clip_index", 0)))

    def _create_clip(self, params, respond):
        track = self._resolve_track(params)
        index = int(params["clip_index"])
        slot = self._resolve_slot(track, index)
        if slot.has_clip:
            raise ValueError(
                "clip slot %d already holds %r, delete it first if you mean to replace it"
                % (index, slot.clip.name)
            )
        if not self._get(track, "has_midi_input", False):
            raise ValueError("%r is not a MIDI track, so it cannot hold a MIDI clip" % track.name)
        length = float(params.get("length", 4.0))
        if length <= 0:
            raise ValueError("clip length must be greater than 0")
        slot.create_clip(length)
        return {"name": slot.clip.name, "length": slot.clip.length, "clip_index": index}

    def _create_audio_clip(self, params, respond):
        track = self._resolve_track(params)
        index = int(params["clip_index"])
        slot = self._resolve_slot(track, index)
        if slot.has_clip:
            raise ValueError("clip slot %d is already occupied" % index)
        if not self._get(track, "has_audio_input", False):
            raise ValueError("%r is not an audio track" % track.name)
        if not hasattr(slot, "create_audio_clip"):
            raise RuntimeError(
                "this Live version cannot import audio through the API; "
                "ClipSlot.create_audio_clip needs Live 12.0.5 or newer"
            )
        slot.create_audio_clip(str(params["path"]))
        return {"name": slot.clip.name, "length": slot.clip.length, "clip_index": index}

    def _delete_clip(self, params, respond):
        view = (params.get("view") or "session").lower()
        if view == "arrangement":
            return self._delete_arrangement_clip(params, respond)
        track = self._resolve_track(params)
        index = int(params["clip_index"])
        slot = self._resolve_slot(track, index)
        if not slot.has_clip:
            raise ValueError("clip slot %d is already empty" % index)
        name = slot.clip.name
        slot.delete_clip()
        return {"deleted": name, "clip_index": index}

    def _duplicate_clip(self, params, respond):
        if (params.get("view") or "session").lower() != "session" \
                or params.get("take_lane_index") is not None:
            raise ValueError(
                "duplicate_clip copies Session clips only; to place a clip in the "
                "Arrangement use duplicate_clip_to_arrangement"
            )
        source_track = self._resolve_track(params)
        source = self._resolve_slot(source_track, int(params["clip_index"]))
        if not source.has_clip:
            raise ValueError("clip slot %d is empty" % int(params["clip_index"]))

        target_track = source_track
        if params.get("target_track_index") is not None:
            target_params = dict(params)
            target_params["track_index"] = int(params["target_track_index"])
            target_params["expect_track_name"] = params.get("expect_target_track_name")
            target_track = self._resolve_track(target_params)
        elif params.get("expect_target_track_name") is not None:
            self._resolve_track(dict(params, expect_track_name=params["expect_target_track_name"]))

        target = self._resolve_slot(target_track, int(params["target_clip_index"]))
        if target.has_clip:
            raise ValueError(
                "target clip slot %d already holds %r"
                % (int(params["target_clip_index"]), target.clip.name)
            )
        source.duplicate_clip_to(target)
        return {"name": target.clip.name,
                "track": target_track.name,
                "clip_index": int(params["target_clip_index"])}

    def _fire_clip(self, params, respond):
        clip = self._resolve_clip(params)
        clip.fire()
        return {"fired": clip.name}

    def _stop_clip(self, params, respond):
        clip = self._resolve_clip(params)
        clip.stop()
        return {"stopped": clip.name}

    def _set_clip_properties(self, params, respond):
        clip = self._resolve_clip(params)
        is_audio = self._get(clip, "is_audio_clip", False)
        changed = []

        audio_only = ("warping", "warp_mode", "gain", "pitch_coarse", "pitch_fine", "ram_mode")
        for key in audio_only:
            if params.get(key) is not None and not is_audio:
                raise ValueError("%s applies to audio clips only; this is a MIDI clip" % key)
        if self._get(clip, "is_arrangement_clip", False):
            launch_only = [k for k in SESSION_ONLY_CLIP_FIELDS if params.get(k) is not None]
            if launch_only:
                raise ValueError(
                    "%s only %s Session clips, which are launched; %r is an "
                    "Arrangement clip"
                    % (", ".join(launch_only),
                       "affects" if len(launch_only) == 1 else "affect", clip.name)
                )

        # Loop bounds before markers: Live clamps each against the other, and
        # writing them in the wrong order can silently reject a valid range.
        if params.get("loop_end") is not None and params.get("loop_start") is not None:
            if float(params["loop_end"]) <= float(params["loop_start"]):
                raise ValueError("loop_end must be greater than loop_start")

        for key, cast in (
            ("name", str), ("color", int), ("muted", bool), ("looping", bool),
            ("loop_start", float), ("loop_end", float),
            ("start_marker", float), ("end_marker", float),
            ("signature_numerator", int), ("signature_denominator", int),
            ("legato", bool), ("velocity_amount", float),
            ("follow_action_enabled", bool),
            ("follow_action_chance_a", int), ("follow_action_chance_b", int),
            ("gain", float), ("pitch_coarse", int), ("pitch_fine", float),
            ("warping", bool), ("ram_mode", bool),
        ):
            if params.get(key) is not None:
                changed.append(self._apply(clip, key, params[key], cast))

        if params.get("follow_action_time") is not None:
            changed.append(self._apply(clip, "follow_time", float(params["follow_action_time"])))
        if params.get("launch_mode") is not None:
            clip.launch_mode = _index_of(LAUNCH_MODE, params["launch_mode"], "launch mode")
            changed.append("launch_mode")
        if params.get("launch_quantization") is not None:
            clip.launch_quantization = _index_of(
                ["global"] + LAUNCH_QUANTIZATION, params["launch_quantization"], "quantization")
            changed.append("launch_quantization")
        for key in ("follow_action_a", "follow_action_b"):
            if params.get(key) is not None:
                setattr(clip, key, _index_of(FOLLOW_ACTION, params[key], "follow action"))
                changed.append(key)
        if params.get("warp_mode") is not None:
            mode = str(params["warp_mode"]).strip().lower()
            if mode not in WARP_MODE:
                raise ValueError("unknown warp mode %r" % params["warp_mode"])
            clip.warp_mode = WARP_MODE[mode]
            changed.append("warp_mode")

        return {"changed": [c for c in changed if c],
                "clip": self._serialize_clip(clip, int(params.get("clip_index", 0)))}

    def _quantize_clip(self, params, respond):
        clip = self._resolve_clip(params)
        grid = _index_of(QUANTIZE_GRID, params.get("grid", "1/16"), "quantization grid")
        amount = max(0.0, min(1.0, float(params.get("amount", 1.0))))
        clip.quantize(grid, amount)
        return {"quantized": clip.name, "grid": params.get("grid", "1/16"), "amount": amount}

    def _crop_clip(self, params, respond):
        clip = self._resolve_clip(params)
        clip.crop()
        return {"cropped": clip.name, "length": clip.length}

    def _duplicate_clip_loop(self, params, respond):
        clip = self._resolve_clip(params)
        clip.duplicate_loop()
        return {"clip": clip.name, "loop_start": clip.loop_start, "loop_end": clip.loop_end}

    # ── Clip automation ──────────────────────────────────────────────────────

    def _envelope_for(self, params, create=False):
        """The clip, the parameter, and Live's envelope object for the pair.

        ``automation_envelope`` returns None when the clip has no envelope for
        the parameter yet, it does not create one. Creating is a separate call
        whose name has varied across Live versions, so try what is present
        rather than assuming.
        """
        clip = self._resolve_clip(params)
        _, parameter = self._resolve_parameter(params)
        if not hasattr(clip, "automation_envelope"):
            raise RuntimeError("this Live version does not expose clip automation")

        envelope = clip.automation_envelope(parameter)
        if envelope is None:
            # An Arrangement copy can hold clip envelopes, but
            # `automation_envelope(parameter)` will not resolve them, the parameter object belongs to the track, not the
            # placed copy. They are still reachable by walking the clip's own
            # envelope collection.
            envelope = self._envelope_from_collection(clip, parameter)
        if envelope is not None or not create:
            return clip, parameter, envelope

        for method in ("create_automation_envelope", "get_automation_envelope"):
            factory = getattr(clip, method, None)
            if factory is None:
                continue
            try:
                envelope = factory(parameter)
            except Exception as e:
                self.log_message("%s(%s) failed: %s" % (method, parameter.name, e))
                continue
            if envelope is not None:
                return clip, parameter, envelope

        # Creation is Session-clip-only ("Not a session clip or parameter
        # belongs to another track"), so an Arrangement clip can only be
        # automated if it already carries an envelope for this parameter.
        if self._get(clip, "is_arrangement_clip", False):
            raise RuntimeError(
                "Live will not create a new automation envelope on an Arrangement clip, "
                "and %r has none for %r yet. Write the envelope on the Session clip "
                "first and then use duplicate_clip_to_arrangement, which carries it into "
                "the Arrangement."
                % (clip.name, parameter.name)
            )

        raise RuntimeError(
            "could not create an automation envelope for %r. The clip offers: %s"
            % (parameter.name,
               ", ".join(n for n in dir(clip) if "envelope" in n.lower()) or "nothing envelope-related")
        )

    def _envelope_from_collection(self, clip, parameter):
        """Find an existing envelope for `parameter` in the clip's own list.

        Matches on the parameter Live reports for each envelope, by identity
        first and then by name, since the placed copy of a clip may expose a
        different parameter object than the track's.
        """
        try:
            envelopes = list(clip.automation_envelopes)
        except (AttributeError, RuntimeError):
            return None

        wanted = parameter.name
        by_name = None
        for envelope in envelopes:
            owner = self._get(envelope, "parameter")
            if owner is parameter:
                return envelope
            if by_name is None and owner is not None \
                    and self._get(owner, "name") == wanted:
                by_name = envelope
        return by_name

    def _get_clip_envelope(self, params, respond):
        clip, parameter, envelope = self._envelope_for(params)
        if envelope is None:
            return {"parameter": parameter.name, "has_envelope": False, "points": []}

        length = max(float(self._get(clip, "length", 0.0) or 0.0), 1.0)
        result = {
            "parameter": parameter.name,
            "has_envelope": True,
            "min": parameter.min,
            "max": parameter.max,
        }

        # Prefer the real breakpoints: sampling is lossy and cannot round-trip.
        events = self._envelope_events(envelope, length)
        if events is not None:
            points, space = events
            result["read_as"] = "breakpoints"
            result["points"] = points
            if space != "parameter":
                result["values"] = space
                result["note"] = ("Live would not report values in the parameter's units, so "
                                  "these are its internal envelope values (for volume, linear gain)")
            return result

        # Older scripts / envelope types expose no event list, so fall back to
        # sampling the curve.
        step = 0.25
        points = []
        time = 0.0
        while time <= length + 1e-9:
            # At exactly the clip origin Live reports the parameter's *static*
            # value rather than the envelope's, which reads as a jump that is
            # not really there. Sample a hair later and label it 0.
            at = time if time > 0.0 else 1e-4
            try:
                points.append([round(time, 6), envelope.value_at_time(at)])
            except Exception:
                break
            time += step

        result["read_as"] = "sampled"
        result["sampled_every_beats"] = step
        result["points"] = points
        return result

    def _envelope_events(self, envelope, length):
        """(breakpoints, value space) for the envelope, or None if Live will not list them.

        An event's `value` is Live's internal envelope value, which for volume
        is linear gain rather than the parameter value set_clip_envelope
        writes, so only the times come from the events and each value is read
        back through value_at_time in parameter space.
        """
        reader = getattr(envelope, "events_in_range", None)
        if reader is None:
            return None
        try:
            events = reader(0.0, length + 1e-6)
        except Exception as e:
            self.log_message("events_in_range failed: %s" % e)
            return None

        raw = []
        for event in events or ():
            time_value = self._get(event, "time")
            value = self._get(event, "value")
            if time_value is None or value is None:
                continue
            raw.append((float(time_value), float(value)))

        try:
            points = [[round(t, 6), round(self._parameter_value_at(envelope, t, length), 6)]
                      for t, _ in raw]
            return points, "parameter"
        except Exception as e:
            self.log_message("value_at_time failed: %s" % e)
            return [[round(t, 6), v] for t, v in raw], "internal"

    @staticmethod
    def _parameter_value_at(envelope, time_value, length, nudge=1e-4):
        """The envelope's value at a breakpoint, in the parameter's units.

        At the clip origin value_at_time reports the parameter's static value,
        and at the clip end it can read past the envelope, so those points are
        read twice just inside and extrapolated back, which is exact on the
        straight segments Live draws between breakpoints.
        """
        if time_value < nudge:
            near = envelope.value_at_time(time_value + nudge)
            far = envelope.value_at_time(time_value + 2 * nudge)
        elif time_value > length - nudge:
            near = envelope.value_at_time(time_value - nudge)
            far = envelope.value_at_time(time_value - 2 * nudge)
        else:
            return float(envelope.value_at_time(time_value))
        return float(2 * near - far)

    def _set_clip_envelope(self, params, respond):
        # Validate before creating anything, so bad input leaves no envelope.
        clip, parameter, envelope = self._envelope_for(params)
        points = self._validated_points(params.get("points") or [], parameter)
        clamped = sum(1 for point in params["points"]
                      if not parameter.min <= float(point[1]) <= parameter.max)
        interpolation = (params.get("interpolation") or "linear").lower()
        resolution = float(params.get("resolution", 0.0625))
        if resolution <= 0:
            raise ValueError("resolution must be greater than 0")

        created = envelope is None
        if created:
            clip, parameter, envelope = self._envelope_for(params, create=True)

        clip_length = float(self._get(clip, "length", 0.0) or 0.0)
        span = max(clip_length, points[-1][0])

        try:
            # Clear the old shape *without* dropping the envelope itself.
            # `clip.clear_envelope` destroys it, and Live will not recreate one
            # on an Arrangement clip, so that route is a one-way door there.
            if not created:
                envelope = self._empty_envelope(clip, parameter, envelope, span, params)
            written, method, how = self._write_points(
                envelope, points, interpolation, resolution, clip_length, span)
        except Exception:
            if created:
                # Do not leave the empty envelope behind: it reads back as
                # automation that is there but holds nothing.
                try:
                    clip.clear_envelope(parameter)
                except Exception as e:
                    self.log_message("clear_envelope after a failed write: %s" % e)
            raise

        result = {
            "parameter": parameter.name,
            "written": written,
            "wrote_as": method,
            "interpolation": interpolation,
            "range": [parameter.min, parameter.max],
        }
        if how:
            result["via"] = how
        if clamped:
            result["clamped"] = clamped
            result["clamp_note"] = ("%d value(s) fell outside %s to %s and were clamped to it"
                                    % (clamped, parameter.min, parameter.max))
        if method == "steps":
            result["resolution"] = resolution
            if interpolation != "step":
                result["note"] = ("Live would not take breakpoints, so the ramps are a "
                                  "stepped approximation every %s beats" % resolution)
        return result

    def _empty_envelope(self, clip, parameter, envelope, span, params):
        """Remove an envelope's events, returning the envelope to write into."""
        if hasattr(envelope, "delete_events_in_range"):
            try:
                envelope.delete_events_in_range(0.0, span + 1.0)
                return envelope
            except Exception as e:
                self.log_message("delete_events_in_range failed: %s" % e)
        if self._get(clip, "is_arrangement_clip", False):
            raise RuntimeError(
                "cannot replace this Arrangement clip's envelope: Live offers no way "
                "to clear it in place, and clearing it outright cannot be undone"
            )
        clip.clear_envelope(parameter)
        return self._envelope_for(params, create=True)[2]

    def _write_points(self, envelope, points, interpolation, resolution, clip_length, span):
        """Write the shape, as breakpoints where Live accepts them, else as steps.

        Returns (events written, "breakpoints" or "steps", the call used).
        """
        # Breakpoints are what Live's own automation lane holds, and it ramps
        # between them natively, so a smooth curve is a handful of events, not
        # hundreds of stepped approximations.
        if interpolation != "step" and hasattr(envelope, "create_event"):
            for name, make in self._event_writers(envelope):
                try:
                    return self._write_breakpoints(make, points, span), "breakpoints", name
                except Exception as e:
                    self.log_message("create_event via %s failed: %s" % (name, e))
                    # Drop anything a writer managed before failing.
                    if hasattr(envelope, "delete_events_in_range"):
                        envelope.delete_events_in_range(0.0, span + 1.0)

        if hasattr(envelope, "insert_step"):
            written = 0
            for start, length, value in self._envelope_steps(
                    points, interpolation, resolution, clip_length):
                envelope.insert_step(start, length, value)
                written += 1
            return written, "steps", "insert_step"

        raise RuntimeError(
            "this envelope cannot be written to; it offers: %s"
            % ", ".join(n for n in dir(envelope) if not n.startswith("_"))
        )

    @staticmethod
    def _event_writers(envelope):
        """The ways of calling create_event, newest first.

        Live 12 takes one Live.Envelope.EnvelopeEvent(time, value); passing
        time and value separately raises a Boost signature mismatch there.
        """
        writers = []
        try:
            from Live.Envelope import EnvelopeEvent
        except ImportError:
            EnvelopeEvent = None
        if EnvelopeEvent is not None:
            writers.append(("EnvelopeEvent",
                            lambda t, v: envelope.create_event(EnvelopeEvent(t, v))))
        writers.append(("create_event(time, value)",
                        lambda t, v: envelope.create_event(t, v)))
        return writers

    @staticmethod
    def _write_breakpoints(create, points, span):
        """Write points as automation events, letting Live ramp between them."""
        written = 0
        for time_value, value in points:
            create(float(time_value), float(value))
            written += 1

        # Hold the last value to the end of the clip; without a closing event
        # Live ramps on from the final breakpoint to whatever follows.
        last_time, last_value = points[-1]
        if span > last_time:
            create(float(span), float(last_value))
            written += 1
        return written

    @staticmethod
    def _validated_points(raw, parameter):
        """Clean the breakpoints: numeric, ordered, clamped to the range."""
        if not raw:
            raise ValueError("points must contain at least one [time, value] pair")
        points = []
        for i, point in enumerate(raw):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("point %d must be a [time, value] pair" % i)
            time, value = float(point[0]), float(point[1])
            if time < 0:
                raise ValueError("point %d has a negative time" % i)
            points.append([time, max(parameter.min, min(parameter.max, value))])
        points.sort(key=lambda p: p[0])
        return points

    @staticmethod
    def _envelope_steps(points, interpolation, resolution, clip_length):
        """Turn breakpoints into the stepped segments Live's API accepts.

        Live only writes steps, so a ramp is approximated by many short ones;
        `resolution` is how finely the ramp is cut.
        """
        end = max(clip_length, points[-1][0])
        if len(points) == 1:
            return [(points[0][0], max(resolution, end - points[0][0]), points[0][1])]

        steps = []
        for i in range(len(points) - 1):
            start_time, start_value = points[i]
            end_time, end_value = points[i + 1]
            span = end_time - start_time
            if span <= 0:
                continue
            if interpolation == "step" or start_value == end_value:
                steps.append((start_time, span, start_value))
                continue
            count = max(1, int(round(span / resolution)))
            width = span / count
            for n in range(count):
                # Sample at each segment's midpoint so the stepped
                # approximation straddles the ideal line rather than lagging it.
                fraction = (n + 0.5) / count
                steps.append((start_time + n * width, width,
                              start_value + (end_value - start_value) * fraction))

        # Hold the final value out to the end of the clip.
        last_time, last_value = points[-1]
        if end > last_time:
            steps.append((last_time, end - last_time, last_value))
        return steps

    def _clear_clip_envelope(self, params, respond):
        clip, parameter, envelope = self._envelope_for(params)
        if envelope is None:
            return {"cleared": False, "parameter": parameter.name,
                    "note": "the clip has no automation for this parameter"}
        clip.clear_envelope(parameter)
        result = {"cleared": True, "parameter": parameter.name}
        if self._get(clip, "is_arrangement_clip", False):
            # A placed copy's envelope may belong to a parameter object other
            # than the track's (see _envelope_for), so clearing by the track's
            # parameter can miss it; clear by the envelope's own owner too.
            if self._envelope_from_collection(clip, parameter) is not None:
                owner = self._get(envelope, "parameter")
                if owner is not None and owner is not parameter:
                    clip.clear_envelope(owner)
            if self._envelope_from_collection(clip, parameter) is not None:
                raise RuntimeError("Live did not remove %r's automation from %r"
                                   % (parameter.name, clip.name))
            result["note"] = ("Live will not create automation on an Arrangement clip, "
                              "so this envelope cannot be written again in place")
        return result

    # ── Notes ────────────────────────────────────────────────────────────────

    @staticmethod
    def _note_window(params):
        """The (from_pitch, pitch_span, from_time, time_span) Live wants."""
        from_time = float(params.get("from_time") or 0.0)
        time_span = params.get("time_span")
        from_pitch = int(params.get("from_pitch") or 0)
        pitch_span = params.get("pitch_span")
        return (
            from_pitch,
            128 - from_pitch if pitch_span is None else int(pitch_span),
            from_time,
            # Live has no "to the end" sentinel, so use a span no clip exceeds.
            1000000.0 if time_span is None else float(time_span),
        )

    @classmethod
    def _serialize_note(cls, note):
        return {
            "note_id": cls._get(note, "note_id"),
            "pitch": cls._get(note, "pitch"),
            "start_time": cls._get(note, "start_time"),
            "duration": cls._get(note, "duration"),
            "velocity": cls._get(note, "velocity"),
            "mute": cls._get(note, "mute"),
            "probability": cls._get(note, "probability"),
            "velocity_deviation": cls._get(note, "velocity_deviation"),
            "release_velocity": cls._get(note, "release_velocity"),
        }

    def _notes_from_clip(self, clip, window=None):
        """Read notes, preferring the extended API for its per-note attributes."""
        from_pitch, pitch_span, from_time, time_span = window or (0, 128, 0.0, 1000000.0)

        if hasattr(clip, "get_notes_extended"):
            return [
                self._serialize_note(note)
                for note in clip.get_notes_extended(from_pitch, pitch_span, from_time, time_span)
            ]

        return [
            {"pitch": n[0], "start_time": n[1], "duration": n[2],
             "velocity": n[3], "mute": n[4]}
            for n in clip.get_notes(from_time, from_pitch, time_span, pitch_span)
        ]

    def _get_clip_notes(self, params, respond):
        clip = self._resolve_clip(params)
        if not self._get(clip, "is_midi_clip", False):
            raise ValueError("%r is an audio clip and has no notes" % clip.name)
        notes = self._notes_from_clip(clip, self._note_window(params))
        return {"clip": clip.name, "length": clip.length,
                "note_count": len(notes), "notes": notes}

    @staticmethod
    def _validate_notes(raw):
        notes = []
        for i, note in enumerate(raw):
            if not isinstance(note, dict):
                raise ValueError("note %d must be an object" % i)
            try:
                pitch = int(note["pitch"])
                start = float(note["start_time"])
                duration = float(note["duration"])
            except KeyError as missing:
                raise ValueError("note %d is missing %s" % (i, missing))
            except (TypeError, ValueError):
                raise ValueError("note %d has a non-numeric pitch, start_time or duration" % i)

            if not 0 <= pitch <= 127:
                raise ValueError("note %d has pitch %d, outside 0-127" % (i, pitch))
            if duration <= 0:
                raise ValueError("note %d has a duration of %s; it must be positive" % (i, duration))
            if start < 0:
                raise ValueError("note %d starts at %s, before the clip" % (i, start))

            notes.append({
                "pitch": pitch,
                "start_time": start,
                "duration": duration,
                "velocity": max(0.0, min(127.0, float(note.get("velocity", 100)))),
                "mute": bool(note.get("mute", False)),
                "probability": None if note.get("probability") is None
                else max(0.0, min(1.0, float(note["probability"]))),
                "velocity_deviation": None if note.get("velocity_deviation") is None
                else float(note["velocity_deviation"]),
                "release_velocity": None if note.get("release_velocity") is None
                else max(0.0, min(127.0, float(note["release_velocity"]))),
            })
        return notes

    def _write_notes(self, clip, notes):
        """Add notes, using the extended API when the per-note fields need it."""
        extended = hasattr(clip, "add_new_notes")
        wants_extended = any(
            n["probability"] is not None
            or n["velocity_deviation"] is not None
            or n["release_velocity"] is not None
            for n in notes
        )

        if extended:
            try:
                from Live.Clip import MidiNoteSpecification
            except ImportError:
                MidiNoteSpecification = None

            if MidiNoteSpecification is not None:
                specifications = []
                for n in notes:
                    fields = {
                        "pitch": n["pitch"],
                        "start_time": n["start_time"],
                        "duration": n["duration"],
                        "velocity": n["velocity"],
                        "mute": n["mute"],
                    }
                    for optional in ("probability", "velocity_deviation", "release_velocity"):
                        if n[optional] is not None:
                            fields[optional] = n[optional]
                    specifications.append(MidiNoteSpecification(**fields))
                clip.add_new_notes(tuple(specifications))
                return "add_new_notes"

        if wants_extended:
            raise RuntimeError(
                "this Live version cannot set probability, velocity deviation or "
                "release velocity; remove those fields or upgrade to Live 11+"
            )

        existing = clip.get_notes(0, 0, 1000000.0, 128)
        added = tuple(
            (n["pitch"], n["start_time"], n["duration"], int(n["velocity"]), n["mute"])
            for n in notes
        )
        clip.set_notes(tuple(existing) + added)
        return "set_notes"

    def _add_notes_to_clip(self, params, respond):
        clip = self._resolve_clip(params)
        if not self._get(clip, "is_midi_clip", False):
            raise ValueError("%r is an audio clip and cannot hold notes" % clip.name)
        notes = self._validate_notes(params.get("notes") or [])
        if not notes:
            return {"added": 0, "clip": clip.name}
        method = self._write_notes(clip, notes)
        return {"added": len(notes), "clip": clip.name, "via": method}

    def _replace_clip_notes(self, params, respond):
        clip = self._resolve_clip(params)
        if not self._get(clip, "is_midi_clip", False):
            raise ValueError("%r is an audio clip and cannot hold notes" % clip.name)
        notes = self._validate_notes(params.get("notes") or [])
        removed = self._remove_notes(clip, (0, 128, 0.0, 1000000.0))
        if notes:
            self._write_notes(clip, notes)
        return {"removed": removed, "added": len(notes), "clip": clip.name}

    def _clear_notes_from_clip(self, params, respond):
        clip = self._resolve_clip(params)
        if not self._get(clip, "is_midi_clip", False):
            raise ValueError("%r is an audio clip and has no notes" % clip.name)
        removed = self._remove_notes(clip, self._note_window(params))
        return {"removed": removed, "clip": clip.name}

    def _remove_notes(self, clip, window):
        from_pitch, pitch_span, from_time, time_span = window
        before = len(self._notes_from_clip(clip, window))
        if hasattr(clip, "remove_notes_extended"):
            clip.remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
        else:
            kept = [
                n for n in clip.get_notes(0, 0, 1000000.0, 128)
                if not (from_pitch <= n[0] < from_pitch + pitch_span
                        and from_time <= n[1] < from_time + time_span)
            ]
            clip.set_notes(tuple(kept))
        return before

    # ── Note editing ─────────────────────────────────────────────────────────

    NOTE_EDIT_FIELDS = (
        "pitch", "start_time", "duration", "velocity", "mute",
        "probability", "velocity_deviation", "release_velocity",
    )

    @staticmethod
    def _check_pitch_shift(notes, semitones):
        """Refuse a shift that would push any note outside 0-127, naming them."""
        overflowing = [n for n in notes if not 0 <= n["pitch"] + semitones <= 127]
        if not overflowing:
            return
        shown = ", ".join(
            "pitch %d at beat %s%s" % (
                n["pitch"], n["start_time"],
                "" if n.get("note_id") is None else " (id %s)" % n["note_id"])
            for n in overflowing[:10]
        )
        more = "" if len(overflowing) <= 10 else ", and %d more" % (len(overflowing) - 10)
        raise ValueError(
            "shifting by %+d semitones would move %d note(s) outside 0-127: %s%s. "
            "Narrow the window or use a smaller shift."
            % (semitones, len(overflowing), shown, more)
        )

    @staticmethod
    def _int_arg(params, name, default=None):
        value = params.get(name)
        if value is None:
            value = default
        if value is None:
            raise ValueError("%s is required" % name)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a whole number, got %r" % (name, value))
        if number != int(number):
            raise ValueError("%s must be a whole number, got %r" % (name, value))
        return int(number)

    def _midi_clip(self, params):
        clip = self._resolve_clip(params)
        if not self._get(clip, "is_midi_clip", False):
            raise ValueError("%r is an audio clip and has no notes" % clip.name)
        return clip

    @staticmethod
    def _in_window(pitch, start, window):
        from_pitch, pitch_span, from_time, time_span = window
        return (from_pitch <= pitch < from_pitch + pitch_span
                and from_time <= start < from_time + time_span)

    @staticmethod
    def _legacy_replace_all(clip, notes):
        if hasattr(clip, "remove_notes"):
            clip.remove_notes(0.0, 0, 1000000.0, 128)
        clip.set_notes(tuple(notes))

    def _transpose_notes(self, params, respond):
        clip = self._midi_clip(params)
        semitones = self._int_arg(params, "semitones")
        window = self._note_window(params)

        if hasattr(clip, "apply_note_modifications"):
            # apply_note_modifications only accepts the MidiNoteVector that
            # get_notes_extended returned, mutated in place; new specs or dicts
            # are rejected.
            vector = clip.get_notes_extended(*window)
            self._check_pitch_shift([self._serialize_note(n) for n in vector], semitones)
            for note in vector:
                note.pitch = note.pitch + semitones
            if len(vector) and semitones:
                clip.apply_note_modifications(vector)
            return {"transposed": len(vector), "semitones": semitones,
                    "clip": clip.name, "via": "apply_note_modifications"}

        everything = list(clip.get_notes(0.0, 0, 1000000.0, 128))
        inside = [n for n in everything if self._in_window(n[0], n[1], window)]
        self._check_pitch_shift(
            [{"pitch": n[0], "start_time": n[1]} for n in inside], semitones)
        if inside and semitones:
            self._legacy_replace_all(clip, [
                (n[0] + semitones,) + tuple(n[1:]) if self._in_window(n[0], n[1], window)
                else tuple(n)
                for n in everything
            ])
        return {"transposed": len(inside), "semitones": semitones,
                "clip": clip.name, "via": "set_notes"}

    def _duplicate_notes(self, params, respond):
        clip = self._midi_clip(params)
        try:
            time_offset = float(params.get("time_offset"))
        except (TypeError, ValueError):
            raise ValueError("time_offset must be a number of beats")
        pitch_offset = self._int_arg(params, "pitch_offset", 0)
        extend_clip = params.get("extend_clip", True) is not False
        if time_offset == 0 and pitch_offset == 0:
            raise ValueError(
                "time_offset and pitch_offset are both 0, so the copies would land "
                "exactly on the originals")

        originals = self._notes_from_clip(clip, self._note_window(params))
        if not originals:
            return {"copied": 0, "clip": clip.name}
        self._check_pitch_shift(originals, pitch_offset)
        early = [n for n in originals if n["start_time"] + time_offset < 0]
        if early:
            raise ValueError(
                "a time_offset of %s would move %d note(s) before the clip start "
                "(the earliest starts at beat %s)"
                % (time_offset, len(early), min(n["start_time"] for n in early)))

        copies = []
        for n in originals:
            copy = dict(n)
            copy.pop("note_id", None)
            copy["pitch"] = n["pitch"] + pitch_offset
            copy["start_time"] = n["start_time"] + time_offset
            copies.append(copy)
        copies = self._validate_notes(copies)
        method = self._write_notes(clip, copies)

        result = {"copied": len(copies), "clip": clip.name, "via": method}
        end = max(n["start_time"] + n["duration"] for n in copies)
        result.update(self._fit_clip_to(clip, end, extend_clip))
        return result

    def _fit_clip_to(self, clip, end, extend):
        """Lengthen a Session clip so notes up to `end` play, rounded up to a bar."""
        looping = bool(self._get(clip, "looping", False))
        boundary = "loop_end" if looping else "end_marker"
        current = float(self._get(clip, boundary, 0.0) or 0.0)
        if end <= current + 1e-9:
            return {}
        if not extend or self._get(clip, "is_arrangement_clip", False):
            return {"past_clip_end": True, boundary: current,
                    "note": "some copies start or run past the clip's %s and will "
                            "not all play" % boundary}

        numerator = self._get(clip, "signature_numerator", 4) or 4
        denominator = self._get(clip, "signature_denominator", 4) or 4
        bar = numerator * 4.0 / denominator
        bars = int(end / bar - 1e-9) + 1
        new_end = bars * bar
        setattr(clip, boundary, new_end)
        return {"extended": boundary, boundary: new_end}

    @classmethod
    def _validated_note_edits(cls, raw):
        if not isinstance(raw, list) or not raw:
            raise ValueError("changes must be a non-empty list of {note_id, ...} objects")
        edits = []
        seen = set()
        for i, change in enumerate(raw):
            if not isinstance(change, dict) or change.get("note_id") is None:
                raise ValueError("change %d must be an object with a note_id" % i)
            try:
                note_id = int(change["note_id"])
            except (TypeError, ValueError):
                raise ValueError("change %d has a non-integer note_id" % i)
            if note_id in seen:
                raise ValueError("note %d is changed twice; merge the changes" % note_id)
            seen.add(note_id)

            unknown = [k for k in change if k != "note_id" and k not in cls.NOTE_EDIT_FIELDS]
            if unknown:
                raise ValueError("change %d has unknown field(s) %s; editable fields are %s"
                                 % (i, ", ".join(sorted(unknown)), ", ".join(cls.NOTE_EDIT_FIELDS)))
            fields = {}
            try:
                for key in cls.NOTE_EDIT_FIELDS:
                    value = change.get(key)
                    if value is None:
                        continue
                    if key == "pitch":
                        value = int(value)
                        if not 0 <= value <= 127:
                            raise ValueError("change %d sets pitch %d, outside 0-127" % (i, value))
                    elif key == "mute":
                        value = bool(value)
                    elif key == "start_time":
                        value = float(value)
                        if value < 0:
                            raise ValueError("change %d moves note %d before the clip start" % (i, note_id))
                    elif key == "duration":
                        value = float(value)
                        if value <= 0:
                            raise ValueError("change %d sets a duration of %s; it must be positive" % (i, value))
                    elif key == "probability":
                        value = max(0.0, min(1.0, float(value)))
                    elif key in ("velocity", "release_velocity"):
                        value = max(0.0, min(127.0, float(value)))
                    else:
                        value = float(value)
                    fields[key] = value
            except (TypeError, ValueError) as e:
                if "change %d" % i in str(e):
                    raise
                raise ValueError("change %d has a non-numeric value: %s" % (i, e))
            if not fields:
                raise ValueError("change %d for note %d changes nothing" % (i, note_id))
            edits.append((note_id, fields))
        return edits

    def _notes_by_id(self, clip, wanted):
        """Every note in the clip keyed by id, erroring on ids it does not hold."""
        vector = clip.get_notes_extended(0, 128, 0.0, 1000000.0)
        by_id = dict((self._get(note, "note_id"), note) for note in vector)
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise ValueError(
                "clip %r has no note(s) with id %s; note ids change when notes are "
                "replaced, so re-read them with get_clip_notes"
                % (clip.name, ", ".join(str(i) for i in missing)))
        return vector, by_id

    def _modify_notes(self, params, respond):
        clip = self._midi_clip(params)
        if not (hasattr(clip, "apply_note_modifications") and hasattr(clip, "get_notes_extended")):
            raise RuntimeError(
                "modify_notes needs Live 11 or newer (Clip.apply_note_modifications). "
                "On this version, read with get_clip_notes, edit the list and write it "
                "back with replace_clip_notes")
        edits = self._validated_note_edits(params.get("changes"))
        # Read the whole clip rather than get_notes_by_id: it lets unknown ids
        # be reported before anything changes, and it yields the vector type
        # apply_note_modifications insists on.
        vector, by_id = self._notes_by_id(clip, [note_id for note_id, _ in edits])
        for note_id, fields in edits:
            note = by_id[note_id]
            for key, value in fields.items():
                setattr(note, key, value)
        clip.apply_note_modifications(vector)
        return {"modified": len(edits), "clip": clip.name,
                "notes": [self._serialize_note(by_id[note_id]) for note_id, _ in edits]}

    def _remove_notes_by_id(self, params, respond):
        clip = self._midi_clip(params)
        if not (hasattr(clip, "remove_notes_by_id") and hasattr(clip, "get_notes_extended")):
            raise RuntimeError(
                "remove_notes_by_id needs Live 11 or newer (Clip.remove_notes_by_id). "
                "On this version, use clear_notes_from_clip with a window, or "
                "replace_clip_notes")
        raw = params.get("note_ids")
        if not isinstance(raw, list) or not raw:
            raise ValueError("note_ids must be a non-empty list of note ids")
        try:
            note_ids = sorted(set(int(i) for i in raw))
        except (TypeError, ValueError):
            raise ValueError("note_ids must all be integers")
        self._notes_by_id(clip, note_ids)
        clip.remove_notes_by_id(tuple(note_ids))
        return {"removed": len(note_ids), "clip": clip.name}

    # ── Devices ──────────────────────────────────────────────────────────────

    def _serialize_device(self, device, index, include_params=False):
        info = {
            "index": index,
            "name": device.name,
            "class_name": self._get(device, "class_name"),
            "display_name": self._get(device, "class_display_name"),
            "type": DEVICE_TYPE.get(self._get(device, "type"), "unknown"),
            "is_active": self._get(device, "is_active"),
            "is_rack": bool(self._get(device, "can_have_chains", False)),
            "has_drum_pads": bool(self._get(device, "can_have_drum_pads", False)),
            "parameter_count": len(self._get(device, "parameters", []) or []),
        }
        if info["is_rack"]:
            info["chains"] = [
                {"index": i, "name": chain.name,
                 "device_count": len(chain.devices),
                 "devices": [d.name for d in chain.devices]}
                for i, chain in enumerate(self._get(device, "chains", []) or [])
            ]
        if include_params:
            info["parameters"] = [
                self._serialize_parameter(p, i)
                for i, p in enumerate(device.parameters)
            ]
        return info

    def _serialize_parameter(self, parameter, index):
        """Everything needed to set a parameter meaningfully.

        The display string and the named settings are the difference between
        "parameter 12 is 0.63" and "Filter Freq is 2.5 kHz".
        """
        info = {
            "index": index,
            "name": parameter.name,
            "value": parameter.value,
            "display_value": self._display_value(parameter),
            "min": parameter.min,
            "max": parameter.max,
            "default_value": self._get(parameter, "default_value"),
            "is_quantized": self._get(parameter, "is_quantized", False),
            "is_enabled": self._get(parameter, "is_enabled", True),
            "automation_state": self._get(parameter, "automation_state", 0),
        }
        if info["is_quantized"]:
            items = self._get(parameter, "value_items")
            if items:
                info["value_items"] = [str(item) for item in items]
                info["current_item"] = _name_of(info["value_items"], parameter.value)
        original = self._get(parameter, "original_name")
        if original and original != parameter.name:
            info["original_name"] = original
        return info

    def _get_devices(self, params, respond):
        track = self._resolve_track(params)
        return {"track": track.name,
                "devices": [self._serialize_device(d, i)
                            for i, d in enumerate(track.devices)]}

    def _get_device_parameters(self, params, respond):
        device = self._resolve_device(params)
        return self._serialize_device(
            device, int(params.get("device_index", 0)), include_params=True)

    def _set_device_parameter(self, params, respond):
        device, parameter = self._resolve_parameter(params)
        if params.get("parameter_index") is None and not params.get("parameter_name"):
            raise ValueError("give either parameter_index or parameter_name")
        previous = parameter.value
        outcome = None
        if params.get("display") is not None:
            if params.get("value") is not None:
                raise ValueError("give value or display, not both")
            outcome = self._set_by_display(parameter, params["display"])
        elif params.get("value") is not None:
            parameter.value = self._clamp_parameter(parameter, params["value"])
        else:
            raise ValueError("give either value or display")
        result = {
            "device": device.name if device else "mixer",
            "parameter": parameter.name,
            "from": previous,
            "to": parameter.value,
            "display": self._display_value(parameter),
        }
        if outcome is not None:
            result["target"] = outcome["target"]
            result["method"] = outcome["method"]
            if "error" in outcome:
                result["error"] = outcome["error"]
        return result

    def _set_device_parameters(self, params, respond):
        device = self._resolve_device(params)
        by_name = {p.name.strip().lower(): p for p in device.parameters}
        applied, failed = [], []

        for i, entry in enumerate(params.get("values") or []):
            if not isinstance(entry, dict) or (entry.get("value") is None
                                               and entry.get("display") is None):
                failed.append({"at": i, "error": "each entry needs a value or display, "
                                                 "and a name or index"})
                continue
            try:
                if entry.get("name") is not None:
                    parameter = by_name.get(str(entry["name"]).strip().lower())
                    if parameter is None:
                        raise ValueError("no parameter named %r" % entry["name"])
                else:
                    index = int(entry["index"])
                    if not 0 <= index < len(device.parameters):
                        raise IndexError("parameter %d does not exist" % index)
                    parameter = device.parameters[index]
                if entry.get("display") is not None:
                    outcome = self._set_by_display(parameter, entry["display"])
                    applied.append({"name": parameter.name, "value": parameter.value,
                                    "display": outcome["achieved"],
                                    "target": outcome["target"],
                                    "error": outcome.get("error")})
                    continue
                parameter.value = self._clamp_parameter(parameter, entry["value"])
                applied.append({"name": parameter.name, "value": parameter.value,
                                "display": self._display_value(parameter)})
            except Exception as e:
                failed.append({"at": i, "error": str(e)})

        return {"device": device.name, "applied": applied, "failed": failed}

    def _set_device_enabled(self, params, respond):
        device = self._resolve_device(params)
        # The on/off switch is always parameter 0 on every Live device.
        switch = device.parameters[0]
        switch.value = 1.0 if params["enabled"] else 0.0
        return {"device": device.name, "enabled": bool(switch.value)}

    def _delete_device(self, params, respond):
        if (params.get("chain_path") or "").strip():
            raise ValueError(
                "devices nested inside a rack cannot be deleted through the API"
            )
        track = self._resolve_track(params)
        index = int(params.get("device_index", 0))
        if not 0 <= index < len(track.devices):
            raise IndexError("device %d does not exist on this track" % index)
        name = track.devices[index].name
        track.delete_device(index)
        return {"deleted": name}

    def _get_rack_chains(self, params, respond):
        device = self._resolve_device(params)
        if not self._get(device, "can_have_chains", False):
            raise ValueError("%r is not a rack" % device.name)

        macros = [
            self._serialize_parameter(p, i)
            for i, p in enumerate(device.parameters)
            if p.name.lower().startswith("macro")
        ]
        chains = []
        for i, chain in enumerate(self._get(device, "chains", []) or []):
            mixer = self._get(chain, "mixer_device")
            chains.append({
                "index": i,
                "name": chain.name,
                "color": self._get(chain, "color"),
                "mute": self._get(chain, "mute"),
                "solo": self._get(chain, "solo"),
                "volume": self._get(getattr(mixer, "volume", None), "value"),
                "panning": self._get(getattr(mixer, "panning", None), "value"),
                "devices": [self._serialize_device(d, j)
                            for j, d in enumerate(chain.devices)],
            })

        result = {"device": device.name, "macros": macros, "chains": chains}
        if self._get(device, "can_have_drum_pads", False):
            result["drum_pads"] = [
                {"note": pad.note, "name": pad.name,
                 "mute": self._get(pad, "mute"), "solo": self._get(pad, "solo")}
                for pad in (self._get(device, "drum_pads", []) or [])
                if pad.chains
            ]
        return result

    def _set_chain_mixer(self, params, respond):
        device = self._resolve_device(params)
        chains = self._get(device, "chains", None)
        if not chains:
            raise ValueError("%r is not a rack" % device.name)
        index = int(params["chain_index"])
        if not 0 <= index < len(chains):
            raise IndexError("chain %d does not exist (there are %d)" % (index, len(chains)))

        chain = chains[index]
        mixer = chain.mixer_device
        changed = []
        for key, cast in (("name", str), ("color", int), ("mute", bool), ("solo", bool)):
            if params.get(key) is not None:
                changed.append(self._apply(chain, key, params[key], cast))
        if params.get("volume") is not None:
            mixer.volume.value = self._clamp_parameter(mixer.volume, params["volume"])
            changed.append("volume")
        if params.get("panning") is not None:
            mixer.panning.value = self._clamp_parameter(mixer.panning, params["panning"])
            changed.append("panning")

        return {"chain": chain.name, "changed": [c for c in changed if c]}

    def _get_simpler_sample(self, params, respond):
        """Where a Simpler's sample lives, for analyze_sample to read the file."""
        device = self._resolve_device(params)
        class_name = self._get(device, "class_name")
        if not hasattr(device, "sample"):
            raise ValueError("%r is a %s, not a Simpler" % (device.name, class_name))
        sample = self._get(device, "sample")
        if sample is None:
            raise ValueError("Simpler %r has no sample loaded" % device.name)
        path = self._get(sample, "file_path")
        if not path:
            raise ValueError("Live does not report a file for %r's sample" % device.name)
        info = {
            "device": device.name,
            "class_name": class_name,
            "file_path": path,
            "sample_rate": self._get(sample, "sample_rate"),
            "length": self._get(sample, "length"),
            "start_marker": self._get(sample, "start_marker"),
            "end_marker": self._get(sample, "end_marker"),
            "warping": self._get(sample, "warping"),
        }
        for parameter in device.parameters:
            if parameter.name in ("Transpose", "Detune"):
                info[parameter.name.lower()] = parameter.value
        return info

    # ── Browser ──────────────────────────────────────────────────────────────

    def _browser(self):
        browser = self._get(self.application(), "browser")
        if browser is None:
            raise RuntimeError("Live's browser is not available yet")
        return browser

    def _browser_roots(self, category_type):
        browser = self._browser()
        category = (category_type or "all").lower()
        names = BROWSER_CATEGORIES if category == "all" else [category]

        roots = []
        for name in names:
            if name not in BROWSER_CATEGORIES:
                raise ValueError(
                    "unknown browser category %r; expected one of: all, %s"
                    % (category_type, ", ".join(BROWSER_CATEGORIES))
                )
            root = self._get(browser, name)
            if root is not None:
                roots.append((name, root))
        if not roots:
            raise RuntimeError("no browser categories are available")
        return roots

    def _search_browser(self, params, respond):
        query = str(params["query"]).strip().lower()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(200, int(params.get("limit", 30))))
        terms = query.split()

        results = []
        # Bounded so a broad query cannot walk an entire multi-gigabyte library.
        budget = [40000]

        def walk(item, category, path, depth):
            if len(results) >= limit or budget[0] <= 0 or depth > 8:
                return
            budget[0] -= 1
            name = self._get(item, "name", "") or ""
            lowered = name.lower()
            if self._get(item, "is_loadable", False) and all(t in lowered for t in terms):
                results.append({
                    "name": name,
                    "uri": self._get(item, "uri"),
                    "category": category,
                    "path": "/".join(path + [name]),
                    "source": self._get(item, "source"),
                })
            try:
                children = item.children
            except (AttributeError, RuntimeError):
                return
            for child in children:
                if len(results) >= limit or budget[0] <= 0:
                    return
                walk(child, category, path + [name] if name else path, depth + 1)

        for category, root in self._browser_roots(params.get("category_type", "all")):
            if len(results) >= limit:
                break
            walk(root, category, [], 0)

        return {
            "query": params["query"],
            "count": len(results),
            "truncated": len(results) >= limit or budget[0] <= 0,
            "results": results,
        }

    def _get_browser_tree(self, params, respond):
        categories = []
        for name, root in self._browser_roots(params.get("category_type", "all")):
            categories.append(self._browser_node(root, name, depth=0, max_depth=3))
        return {"categories": categories}

    def _browser_node(self, item, fallback_name, depth, max_depth):
        node = {
            "name": self._get(item, "name", fallback_name) or fallback_name,
            "uri": self._get(item, "uri"),
            "is_loadable": self._get(item, "is_loadable", False),
            "is_folder": self._get(item, "is_folder", False),
        }
        if depth >= max_depth:
            try:
                node["child_count"] = len(item.children)
                node["has_more"] = node["child_count"] > 0
            except (AttributeError, RuntimeError):
                pass
            return node

        try:
            children = list(item.children)
        except (AttributeError, RuntimeError):
            return node
        if children:
            node["children"] = [
                self._browser_node(child, "", depth + 1, max_depth)
                for child in children[:200]
            ]
            if len(children) > 200:
                node["truncated_children"] = len(children) - 200
        return node

    def _get_browser_items_at_path(self, params, respond):
        path = str(params.get("path", "")).strip("/ ")
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError("path must not be empty")

        roots = dict(self._browser_roots("all"))
        head = parts[0].lower()
        if head not in roots:
            raise ValueError(
                "unknown browser category %r; expected one of: %s"
                % (parts[0], ", ".join(sorted(roots)))
            )

        item = roots[head]
        for part in parts[1:]:
            wanted = part.strip().lower()
            match = None
            try:
                children = item.children
            except (AttributeError, RuntimeError):
                children = []
            for child in children:
                if (self._get(child, "name", "") or "").strip().lower() == wanted:
                    match = child
                    break
            if match is None:
                available = [self._get(c, "name") for c in children][:40]
                raise ValueError(
                    "%r not found under %r; available: %s"
                    % (part, path, ", ".join(n for n in available if n))
                )
            item = match

        try:
            children = list(item.children)
        except (AttributeError, RuntimeError):
            children = []
        return {
            "path": path,
            "items": [
                {"name": self._get(c, "name"),
                 "uri": self._get(c, "uri"),
                 "is_loadable": self._get(c, "is_loadable", False),
                 "is_folder": self._get(c, "is_folder", False)}
                for c in children
            ],
        }

    def _find_browser_item(self, uri):
        target = str(uri)
        found = [None]
        budget = [60000]

        def walk(item, depth):
            if found[0] is not None or budget[0] <= 0 or depth > 10:
                return
            budget[0] -= 1
            if self._get(item, "uri") == target:
                found[0] = item
                return
            try:
                children = item.children
            except (AttributeError, RuntimeError):
                return
            for child in children:
                walk(child, depth + 1)
                if found[0] is not None:
                    return

        for _, root in self._browser_roots("all"):
            walk(root, 0)
            if found[0] is not None:
                break
        return found[0]

    def _load_browser_item(self, params, respond):
        track = self._resolve_track(params)
        uri = str(params["uri"])
        item = self._find_browser_item(uri)
        if item is None:
            raise ValueError(
                "no browser item with URI %r; get a current URI from search_browser" % uri
            )
        if not self._get(item, "is_loadable", False):
            raise ValueError("%r is a folder, not something loadable" % self._get(item, "name"))

        before = [d.name for d in track.devices]
        # load_item has no track argument: it loads onto whatever is selected.
        self._song_ref.view.selected_track = track
        self._browser().load_item(item)
        after = [d.name for d in track.devices]

        return {
            "loaded": self._get(item, "name"),
            "track": track.name,
            "new_devices": after[len(before):] or [d for d in after if d not in before],
            "devices": after,
        }

    def _load_drum_kit(self, params, respond):
        rack = self._load_browser_item(
            {"track_index": params.get("track_index"),
             "track_type": params.get("track_type"),
             "expect_track_name": params.get("expect_track_name"),
             "uri": params["rack_uri"]}, None)

        listing = self._get_browser_items_at_path({"path": params["kit_path"]}, None)
        loadable = [i for i in listing["items"] if i["is_loadable"]]
        if not loadable:
            raise ValueError("no loadable kit found at %r" % params["kit_path"])

        kit = self._load_browser_item(
            {"track_index": params.get("track_index"),
             "track_type": params.get("track_type"),
             "expect_track_name": params.get("expect_track_name"),
             "uri": loadable[0]["uri"]}, None)
        return {"rack": rack["loaded"], "kit": kit["loaded"], "track": kit["track"]}

    # ── Arrangement ──────────────────────────────────────────────────────────

    def _get_arrangement_clips(self, params, respond):
        track, owner, lane = self._arrangement_owner(params)
        clips = list(owner.arrangement_clips)
        result = {
            "track": track.name,
            "count": len(clips),
            "clips": [self._serialize_clip(c, i, brief=True) for i, c in enumerate(clips)],
        }
        if lane is not None:
            result["take_lane_index"] = lane
        return result

    def _duplicate_clip_to_arrangement(self, params, respond):
        track = self._resolve_track(params)
        slot = self._resolve_slot(track, int(params["clip_index"]))
        if not slot.has_clip:
            raise ValueError("clip slot %d is empty" % int(params["clip_index"]))

        source = slot.clip
        repeats = max(1, int(params.get("repeats", 1)))
        length = float(source.length)
        if length <= 0:
            raise ValueError("the source clip has no length")

        start = float(params["destination_time"])
        through = start + repeats * length
        self._check_arrangement_gap(track, start, through, track.name)
        automated = self._clip_automated_parameters(track, source)

        placed = []
        for n in range(repeats):
            at = start + n * length
            track.duplicate_clip_to_arrangement(source, at)
            placed.append(at)

        clips = list(track.arrangement_clips)
        copies = []
        for at in placed:
            index = next((i for i, c in enumerate(clips)
                          if abs(float(self._get(c, "start_time", -1.0)) - at) < 1e-6), None)
            copies.append({"clip_index": index, "start_time": at})

        envelopes = []
        for device, parameter in automated:
            missing = [c["clip_index"] for c in copies
                       if c["clip_index"] is None
                       or not self._clip_has_envelope(clips[c["clip_index"]], parameter)]
            if not missing:
                via = "clip"
            elif self._get(parameter, "automation_state", 0):
                via = "track_lane"
            else:
                via = None
            envelopes.append({"device": device, "parameter": parameter.name,
                              "carried": via is not None, "via": via,
                              "missing_on": [] if via else missing})
        result = {
            "clip": source.name,
            "track": track.name,
            "placed_at": placed,
            "through": through,
            "copies": copies,
            "envelopes": envelopes,
        }
        lane = ["%s %s" % (e["device"], e["parameter"]) for e in envelopes
                if e["via"] == "track_lane"]
        lost = ["%s %s" % (e["device"], e["parameter"]) for e in envelopes if not e["carried"]]
        notes = []
        if lane:
            notes.append("The automation for %s now lives on the track's Arrangement "
                         "automation lane, not on the copies, and the API cannot read or "
                         "edit it. To change it, rewrite the Session clip envelope and copy "
                         "again (copying over the same range is expected to replace it; "
                         "untested)." % ", ".join(lane))
        if lost:
            notes.append("Live did not carry these envelopes onto the copies, as clip "
                         "envelopes or track automation: %s." % ", ".join(lost))
        if notes:
            result["note"] = " ".join(notes)
        return result

    def _clip_has_envelope(self, clip, parameter):
        try:
            if clip.automation_envelope(parameter) is not None:
                return True
        except Exception:
            pass
        return self._envelope_from_collection(clip, parameter) is not None

    def _clip_automated_parameters(self, track, clip):
        """(device name, parameter) for each mixer or top-level device
        parameter the clip has an envelope for."""
        mixer = track.mixer_device
        candidates = [("Mixer", p) for p in
                      [mixer.volume, mixer.panning] + list(getattr(mixer, "sends", []))]
        for device in getattr(track, "devices", []):
            candidates.extend((device.name, p) for p in device.parameters)

        automated = []
        for device, parameter in candidates:
            try:
                if clip.automation_envelope(parameter) is not None:
                    automated.append((device, parameter))
            except Exception:
                continue
        return automated

    def _arrangement_owner(self, params):
        """(track, what holds its Arrangement clips, take lane index or None).

        A take lane keeps its own clip list, so "the clips" and "where a new
        clip goes" are either the track's main lane or one of its take lanes.
        """
        track = self._resolve_track(params)
        lane = params.get("take_lane_index")
        if lane is None:
            return track, track, None
        return track, self._resolve_take_lane(track, int(lane)), int(lane)

    def _check_arrangement_gap(self, owner, start, end, where):
        for clip in owner.arrangement_clips:
            clip_start = float(self._get(clip, "start_time", 0.0) or 0.0)
            clip_end = float(self._get(clip, "end_time", clip_start) or clip_start)
            if clip_start < end - 1e-9 and clip_end > start + 1e-9:
                raise ValueError(
                    "%r already occupies beats %s to %s on %s; delete it first with "
                    "delete_arrangement_clip or clear_arrangement if you mean to replace it"
                    % (clip.name, clip_start, clip_end, where)
                )

    def _placed_clip(self, track, owner, lane, created, start):
        """Report a clip just placed in the Arrangement, with its clip_index."""
        clips = list(owner.arrangement_clips)
        index = None
        if created is not None:
            index = next((i for i, c in enumerate(clips) if c == created), None)
        if index is None:
            index = next((i for i, c in enumerate(clips)
                          if abs(float(self._get(c, "start_time", -1.0)) - start) < 1e-6), None)
        if index is None:
            raise RuntimeError("Live did not place a clip at beat %s" % start)
        result = {
            "track": track.name,
            "view": "arrangement",
            "clip_index": index,
            "clip": self._serialize_clip(clips[index], index, brief=True),
        }
        if lane is not None:
            result["take_lane_index"] = lane
        return result

    @staticmethod
    def _beats(params, name, minimum=0.0, strict=False):
        value = float(params[name])
        if value < minimum or (strict and value == minimum):
            raise ValueError("%s must be %s %s, got %s"
                             % (name, "greater than" if strict else "at least", minimum, value))
        return value

    def _create_arrangement_clip(self, params, respond):
        track, owner, lane = self._arrangement_owner(params)
        if not self._get(track, "has_midi_input", False):
            raise ValueError("%r is not a MIDI track, so it cannot hold a MIDI clip" % track.name)
        if not hasattr(owner, "create_midi_clip"):
            raise RuntimeError(
                "this Live version cannot create Arrangement clips through the API; "
                "create_midi_clip needs a recent Live 12 release. Create a Session clip "
                "and use duplicate_clip_to_arrangement instead"
            )
        start = self._beats(params, "start_time")
        if params.get("length") is None:
            params = dict(params, length=4.0)
        length = self._beats(params, "length", strict=True)
        where = track.name if lane is None else "take lane %d of %r" % (lane, track.name)
        self._check_arrangement_gap(owner, start, start + length, where)
        created = owner.create_midi_clip(start, length)
        return self._placed_clip(track, owner, lane, created, start)

    def _create_arrangement_audio_clip(self, params, respond):
        track, owner, lane = self._arrangement_owner(params)
        if not self._get(track, "has_audio_input", False):
            raise ValueError("%r is not an audio track" % track.name)
        if not hasattr(owner, "create_audio_clip"):
            raise RuntimeError(
                "this Live version cannot import audio into the Arrangement through the "
                "API; create_audio_clip on a track needs a recent Live 12 release"
            )
        start = self._beats(params, "start_time")
        where = track.name if lane is None else "take lane %d of %r" % (lane, track.name)
        # The file's length is unknown until Live has read it, so only the
        # start can be checked for a collision.
        self._check_arrangement_gap(owner, start, start + 1e-6, where)
        created = owner.create_audio_clip(str(params["path"]), start)
        return self._placed_clip(track, owner, lane, created, start)

    def _delete_arrangement_clip(self, params, respond):
        track, owner, lane = self._arrangement_owner(params)
        clip = self._resolve_clip(dict(params, view="arrangement"))
        name, start = clip.name, self._get(clip, "start_time")
        remaining = self._delete_placed_clip(track, owner, clip)
        result = {"deleted": name, "was_at": start, "remaining": remaining}
        if lane is not None:
            result["take_lane_index"] = lane
        return result

    def _delete_placed_clip(self, track, owner, clip):
        """Delete an Arrangement clip and confirm it is gone; returns what is left.

        Track.delete_clip silently ignores a take lane's clip, so a call that
        does not raise proves nothing. Ableton's own scripts delete through
        the clip's canonical_parent; try that, then the lane, then the track,
        checking the clip count after each.
        """
        before = len(owner.arrangement_clips)
        main_before = len(track.arrangement_clips)
        on_lane = owner is not track
        candidates = [("the clip's canonical_parent", self._get(clip, "canonical_parent"))]
        if on_lane:
            candidates.append(("the take lane", owner))
        candidates.append(("the track", track))
        tried = []
        for role, holder in candidates:
            if holder is None:
                tried.append("%s is not available" % role)
                continue
            label = "%s (%s)" % (role, type(holder).__name__)
            deleter = getattr(holder, "delete_clip", None)
            if deleter is None:
                tried.append("%s has no delete_clip" % label)
                continue
            try:
                deleter(clip)
            except Exception as e:
                tried.append("%s.delete_clip raised %s" % (label, e))
                continue
            after = len(owner.arrangement_clips)
            if after == before - 1:
                return after
            tried.append("%s.delete_clip left it in place" % label)
            if on_lane and len(track.arrangement_clips) != main_before:
                raise RuntimeError("deleting a take lane clip removed a main lane clip "
                                   "instead; undo restores it")
        if on_lane:
            raise RuntimeError(
                "Live's API offers no way to delete a take lane clip (as of Live 12.4): %s. "
                "Delete it in Live itself, or leave it: a take lane clip only plays once "
                "it is comped into the main lane." % "; ".join(tried))
        raise RuntimeError("Live did not delete the clip: %s" % "; ".join(tried))

    # ── Take lanes ───────────────────────────────────────────────────────────

    def _serialize_take_lane(self, lane, index):
        try:
            clips = list(lane.arrangement_clips)
        except (AttributeError, RuntimeError):
            clips = []
        return {
            "index": index,
            "name": self._get(lane, "name"),
            "clip_count": len(clips),
            "clips": [self._serialize_clip(c, i, brief=True) for i, c in enumerate(clips)],
        }

    def _get_take_lanes(self, params, respond):
        track = self._resolve_track(params)
        lanes = self._take_lanes(track)
        return {
            "track": track.name,
            "count": len(lanes),
            "take_lanes": [self._serialize_take_lane(l, i) for i, l in enumerate(lanes)],
        }

    def _create_take_lane(self, params, respond):
        track = self._resolve_track(params)
        self._take_lanes(track)
        if not hasattr(track, "create_take_lane"):
            raise RuntimeError(
                "this Live version cannot add take lanes through the API; "
                "Track.create_take_lane needs a recent Live 12 release"
            )
        created = track.create_take_lane()
        lanes = list(track.take_lanes)
        index = None
        if created is not None:
            index = next((i for i, l in enumerate(lanes) if l == created), None)
        if index is None:
            index = len(lanes) - 1
        lane = lanes[index]
        if params.get("name"):
            self._apply(lane, "name", str(params["name"]))
        result = {"track": track.name, "count": len(lanes)}
        result.update(self._serialize_take_lane(lane, index))
        return result

    # ── Arrangement recording ────────────────────────────────────────────────

    def _armed_tracks(self, indices=None):
        tracks = self._song_ref.tracks
        if indices is None:
            indices = [i for i, t in enumerate(tracks)
                       if self._get(t, "can_be_armed", False) and self._get(t, "arm", False)]
        return [
            {"track_index": i, "name": tracks[i].name,
             "arrangement_clip_count": len(tracks[i].arrangement_clips)}
            for i in indices
        ]

    def _recording_state(self, track_indices=None):
        song = self._song_ref
        state = {
            "recording": bool(self._get(song, "record_mode", False)),
            "is_playing": bool(song.is_playing),
            "current_song_time": song.current_song_time,
            "overdub": self._get(song, "arrangement_overdub"),
            "armed_tracks": self._armed_tracks(track_indices),
        }
        if self._get(song, "is_counting_in", False):
            state["counting_in"] = True
        return state

    def _when_settled(self, respond, settled, report):
        """Answer with report() once settled() holds, one tick later still.

        Live applies transport and record changes on a later tick, and a take
        becomes a clip only after recording stops, so reading back straight
        away reports the state from before the request.
        """
        ticks = {"count": 0, "settled": False}

        def check():
            ticks["count"] += 1
            try:
                if not ticks["settled"]:
                    if settled():
                        ticks["settled"] = True
                    elif ticks["count"] <= MAX_DEFER_TICKS:
                        self._defer(1, check)
                        return
                    else:
                        result = report()
                        result["warning"] = ("Live had not applied the change after %d "
                                             "ticks; this is its state now" % ticks["count"])
                        respond({"ok": result})
                        return
                    self._defer(1, check)
                    return
                respond({"ok": report()})
            except Exception as e:
                respond({"error": str(e)})

        self._defer(1, check)
        return DEFERRED

    def _start_arrangement_recording(self, params, respond):
        song = self._song_ref

        # Resolve everything before changing anything, so a bad index leaves
        # the set as it was.
        to_arm = []
        for index in params.get("arm_tracks") or []:
            track = self._resolve_track({"track_index": index})
            if not self._get(track, "can_be_armed", False):
                raise ValueError("%r cannot be record-armed" % track.name)
            to_arm.append(track)
        from_time = params.get("from_time")
        if from_time is not None:
            from_time = float(from_time)
            if from_time < 0:
                raise ValueError("from_time must not be negative")
            if respond is None:
                raise RuntimeError(
                    "start_arrangement_recording with from_time needs more than one pass "
                    "and cannot run inside a batch"
                )

        for track in to_arm:
            track.arm = True
        if params.get("overdub") is not None:
            song.arrangement_overdub = bool(params["overdub"])

        def begin():
            song.record_mode = True
            if not song.is_playing:
                song.start_playing()

        def report():
            state = self._recording_state()
            if not state["armed_tracks"]:
                state["warning"] = ("no track is armed, so no clips will be recorded; "
                                    "pass arm_tracks")
            return state

        def settled():
            return bool(self._get(song, "record_mode", False)) and (
                song.is_playing or self._get(song, "is_counting_in", False))

        if from_time is None:
            begin()
            if respond is None:
                return self._as_requested(report(), recording=True, is_playing=True)
            return self._when_settled(respond, settled, report)

        song.current_song_time = from_time
        ticks = {"count": 0}

        def when_positioned():
            ticks["count"] += 1
            if abs(song.current_song_time - from_time) > 1e-3:
                if ticks["count"] > MAX_DEFER_TICKS:
                    respond({"error": "Live would not move the playhead to beat %s" % from_time})
                    return
                self._defer(1, when_positioned)
                return
            try:
                begin()
                self._when_settled(respond, settled, report)
            except Exception as e:
                respond({"error": str(e)})

        self._defer(1, when_positioned)
        return DEFERRED

    @staticmethod
    def _as_requested(state, **requested):
        """Inside a batch there is no later tick to read back on."""
        state.update(requested)
        state["reported"] = ("as requested: inside a batch Live has not applied the "
                             "change yet")
        return state

    def _stop_arrangement_recording(self, params, respond):
        song = self._song_ref
        was_recording = bool(self._get(song, "record_mode", False))
        stop_playback = params.get("stop_playback", True) is not False
        # Report the tracks that were recording, even once disarmed.
        recorded = [entry["track_index"] for entry in self._armed_tracks()]
        song.record_mode = False
        if stop_playback and song.is_playing:
            song.stop_playing()
        disarmed = None
        if params.get("disarm"):
            disarmed = []
            for index in recorded:
                song.tracks[index].arm = False
                disarmed.append(song.tracks[index].name)

        def report():
            state = self._recording_state(recorded)
            state["was_recording"] = was_recording
            if disarmed is not None:
                state["disarmed"] = disarmed
            return state

        def settled():
            return not self._get(song, "record_mode", False) and (
                not stop_playback or not song.is_playing)

        if respond is None:
            requested = {"recording": False}
            if stop_playback:
                requested["is_playing"] = False
            return self._as_requested(report(), **requested)
        return self._when_settled(respond, settled, report)

    def _clear_arrangement(self, params, respond):
        song = self._song_ref
        self._require_index_for_guard(params)
        if params.get("track_index") is not None:
            tracks = [self._resolve_track(params)]
        else:
            tracks = list(song.tracks)

        from_time = params.get("from_time")
        to_time = params.get("to_time")
        deleted = []

        for track in tracks:
            # Snapshot first: deleting mutates arrangement_clips as we go.
            for clip in list(track.arrangement_clips):
                start = self._get(clip, "start_time", 0.0)
                if from_time is not None and start < float(from_time):
                    continue
                if to_time is not None and start >= float(to_time):
                    continue
                try:
                    name = clip.name
                    track.delete_clip(clip)
                    deleted.append({"track": track.name, "name": name, "start_time": start})
                except Exception as e:
                    self.log_message("could not delete arrangement clip: %s" % e)

        return {"deleted_count": len(deleted), "deleted": deleted[:100],
                "tracks_touched": len({d["track"] for d in deleted})}

    # ── Locators ─────────────────────────────────────────────────────────────

    def _get_locators(self, params, respond):
        return {
            "locators": [
                {"index": i, "name": cue.name, "time": cue.time}
                for i, cue in enumerate(self._song_ref.cue_points)
            ]
        }

    def _find_cue(self, time_value, tolerance=CUE_TOLERANCE):
        for cue in self._song_ref.cue_points:
            if abs(cue.time - time_value) <= tolerance:
                return cue
        return None

    def _cue_times(self):
        return [float(cue.time) for cue in self._song_ref.cue_points]

    @staticmethod
    def _new_cue_times(before, after):
        return [t for t in after if not any(abs(t - b) <= CUE_TOLERANCE for b in before)]

    def _locator_session(self):
        """Prepare the transport for toggling a cue; return (was_playing, restore).

        set_or_delete_cue toggles a cue wherever the playhead actually is, so a
        moving playhead is stopped first. Global launch quantization is switched
        off for the duration because Live quantizes playhead jumps by it; it
        may also be behind the stopped playhead snapping to a coarse grid seen
        in Live 12.4.6 (beat 140 landed on 128).
        """
        song = self._song_ref
        original_time = song.current_song_time
        was_playing = bool(song.is_playing)
        quantization = getattr(song, "clip_trigger_quantization", None)
        if was_playing:
            song.stop_playing()
        if quantization:
            try:
                song.clip_trigger_quantization = 0
            except Exception as e:
                self.log_message("could not switch off launch quantization: %s" % e)
                quantization = None

        def restore():
            if quantization:
                try:
                    song.clip_trigger_quantization = quantization
                except Exception as e:
                    self.log_message("could not restore launch quantization: %s" % e)
            song.current_song_time = original_time
            if was_playing:
                song.continue_playing()

        return was_playing, restore

    def _place_playhead(self, target, done, moves=()):
        """Move the playhead onto `target` and call done(landed, problem).

        problem is None once current_song_time reads the target. Each move is
        tried in turn, the next one only after the playhead has stopped moving
        somewhere else: the caller's own moves first (e.g. CuePoint.jump), then
        writing current_song_time, then jump_by from wherever it landed.
        """
        song = self._song_ref

        def jump_by(now):
            song.jump_by(target - now)

        moves = list(moves) + [
            ("current_song_time", lambda now: setattr(song, "current_song_time", target)),
            ("jump_by", jump_by),
        ]
        state = {"ticks": 0, "last": None, "stable": 0, "skip": False, "tried": []}

        def next_move():
            name, move = moves.pop(0)
            state["tried"].append(name)
            state["last"], state["stable"], state["skip"] = None, 0, False
            try:
                move(float(song.current_song_time))
            except Exception as e:
                self.log_message("playhead move %s failed: %s" % (name, e))
                state["skip"] = True

        def check():
            state["ticks"] += 1
            now = float(song.current_song_time)
            if abs(now - target) <= CUE_TOLERANCE:
                done(now, None)
                return
            if state["last"] is not None and abs(now - state["last"]) <= 1e-6:
                state["stable"] += 1
            else:
                state["stable"] = 0
            state["last"] = now
            settled = state["skip"] or state["stable"] >= PLAYHEAD_SETTLE_TICKS
            if state["ticks"] > MAX_DEFER_TICKS or (settled and not moves):
                done(now, "tried %s" % ", ".join(state["tried"]))
                return
            if settled:
                next_move()
            self._defer(1, check)

        next_move()
        self._defer(1, check)

    @staticmethod
    def _unreachable(target, landed, consequence):
        return (
            "Live would not put the playhead on beat %g; it stopped on beat %g. %s "
            "Locators can only be toggled at the playhead, so nothing was toggled. "
            "The stopped playhead appears to snap to the Arrangement grid: switch off "
            "Snap to Grid (Cmd-4 / Ctrl-4) or zoom in on that region and retry, or use "
            "a beat the playhead does reach, such as %g." % (target, landed, consequence, landed)
        )

    def _remove_cue_at(self, time_value, done):
        """Delete the cue at time_value, verified; done(ok, message).

        The cue is only toggled once the playhead reads its time, and afterwards
        the cue must be gone with no new one in its place.
        """
        song = self._song_ref
        cue = self._find_cue(time_value)
        if cue is None:
            done(False, "there is no locator at beat %g" % time_value)
            return
        before = self._cue_times()

        def placed(landed, problem):
            if problem is not None:
                done(False, self._unreachable(time_value, landed, "The locator was not deleted."))
                return
            if self._find_cue(time_value) is None:
                done(False, "the locator at beat %g vanished before it could be deleted"
                     % time_value)
                return
            song.set_or_delete_cue()
            self._defer(1, lambda: verify(0))

        def verify(attempt):
            after = self._cue_times()
            gone = self._find_cue(time_value) is None
            added = self._new_cue_times(before, after)
            if not gone and not added and attempt < 3:
                self._defer(1, lambda: verify(attempt + 1))
                return
            if added:
                done(False, "deleting the locator at beat %g created one at beat %s instead"
                     % (time_value, ", ".join("%g" % t for t in added)))
            elif not gone:
                done(False, "Live did not delete the locator at beat %g" % time_value)
            else:
                done(True, None)

        self._place_playhead(time_value, placed, moves=[("CuePoint.jump", lambda now: cue.jump())])

    def _remove_cues(self, times, done):
        """Delete cues one by one, stopping at the first failure; done(removed, error)."""
        times = list(times)
        removed = []

        def step():
            if not times:
                done(removed, None)
                return
            time_value = times.pop(0)

            def after(ok, message):
                if not ok:
                    done(removed, message)
                    return
                removed.append(time_value)
                step()

            self._remove_cue_at(time_value, after)

        step()

    def _create_locator(self, params, respond):
        """Place a locator at a beat position.

        Live can only toggle a cue at the playhead (CuePoint.time is
        read-only), so the playhead is moved there and the toggle happens only
        once current_song_time reads the target. Live may land the playhead
        somewhere else (it snapped beat 140 to 128 while stopped in 12.4.6);
        toggling there would create a stray cue, so the call fails instead.
        Afterwards exactly one new cue must sit at the target; any other new
        cue is deleted again. A playing transport is stopped for the few
        ticks this takes and continued afterwards (interrupted_playback).
        """
        song = self._song_ref
        name = str(params.get("name") or "")
        target = float(params["time"])
        if target < 0:
            raise ValueError("locator time must not be negative")

        existing = self._find_cue(target)
        if existing is not None:
            if name:
                existing.name = name
            return {"name": existing.name, "time": existing.time, "renamed": True}

        if respond is None:
            raise RuntimeError(
                "create_locator needs more than one pass and cannot run inside a batch"
            )

        before = self._cue_times()
        was_playing, restore = self._locator_session()

        def fail(message):
            restore()
            respond({"error": message})

        def placed(landed, problem):
            if problem is not None:
                fail(self._unreachable(target, landed, "No locator was created."))
                return
            try:
                song.set_or_delete_cue()
            except Exception as e:
                fail(str(e))
                return
            self._defer(1, lambda: verify(0))

        def verify(attempt):
            added = self._new_cue_times(before, self._cue_times())
            if not added and attempt < 3:
                self._defer(1, lambda: verify(attempt + 1))
                return
            strays = [t for t in added if abs(t - target) > CUE_TOLERANCE]
            if strays:
                self._remove_cues(strays, lambda removed, error: finish(strays, removed, error))
            else:
                finish([], [], None)

        def finish(strays, removed, error):
            cue = self._find_cue(target)
            if cue is not None and name:
                cue.name = name
            restore()
            if strays:
                listed = ", ".join("%g" % t for t in strays)
                if error is None:
                    note = "it was deleted again"
                else:
                    note = "deleting it again failed (%s); remove it by hand" % error
                respond({"error": "Live created a locator at beat %s instead of %g; %s"
                         % (listed, target, note)})
                return
            if cue is None:
                respond({"error": "Live did not create a locator at beat %g" % target})
                return
            result = {"name": cue.name, "time": cue.time, "created": True}
            if was_playing:
                result["interrupted_playback"] = True
            respond({"ok": result})

        self._place_playhead(target, placed)
        return DEFERRED

    def _delete_locator(self, params, respond):
        cues = list(self._song_ref.cue_points)
        index = int(params["index"])
        if not 0 <= index < len(cues):
            raise IndexError("locator %d does not exist (there are %d)" % (index, len(cues)))
        if respond is None:
            raise RuntimeError(
                "delete_locator needs more than one pass and cannot run inside a batch"
            )
        cue = cues[index]
        name, time_value = cue.name, float(cue.time)
        was_playing, restore = self._locator_session()

        def done(ok, message):
            restore()
            if not ok:
                respond({"error": message})
                return
            result = {"deleted": name, "was_at": time_value,
                      "remaining": len(self._song_ref.cue_points)}
            if was_playing:
                result["interrupted_playback"] = True
            respond({"ok": result})

        self._remove_cue_at(time_value, done)
        return DEFERRED

    def _clear_locators(self, params, respond):
        from_time = params.get("from_time")
        to_time = params.get("to_time")
        targets = []
        for cue in self._song_ref.cue_points:
            if from_time is not None and cue.time < float(from_time):
                continue
            if to_time is not None and cue.time >= float(to_time):
                continue
            targets.append({"name": cue.name, "time": float(cue.time)})

        if not targets:
            return {"deleted_count": 0, "deleted": [],
                    "remaining": len(self._song_ref.cue_points)}
        if respond is None:
            raise RuntimeError(
                "clear_locators needs more than one pass and cannot run inside a batch"
            )

        was_playing, restore = self._locator_session()

        def done(removed, error):
            restore()
            deleted = [t for t in targets if t["time"] in removed]
            if error is not None:
                respond({"error": "deleted %d of %d locators, then stopped: %s"
                         % (len(deleted), len(targets), error)})
                return
            result = {"deleted_count": len(deleted), "deleted": deleted,
                      "remaining": len(self._song_ref.cue_points)}
            if was_playing:
                result["interrupted_playback"] = True
            respond({"ok": result})

        self._remove_cues([t["time"] for t in targets], done)
        return DEFERRED

    def _jump_to_locator(self, params, respond):
        cues = list(self._song_ref.cue_points)
        index = int(params["index"])
        if not 0 <= index < len(cues):
            raise IndexError("locator %d does not exist (there are %d)" % (index, len(cues)))
        cues[index].jump()
        return {"jumped_to": cues[index].name, "time": cues[index].time}

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def _get_session_snapshot(self, params, respond):
        include_notes = bool(params.get("include_notes", False))
        include_params = bool(params.get("include_params", False))
        song = self._song_ref

        def snapshot_track(track, index, track_type):
            info = self._serialize_track(track, index, track_type,
                                         include_devices=False, include_clips=False)
            info["devices"] = [self._serialize_device(d, i, include_params)
                               for i, d in enumerate(track.devices)]
            info["session_clips"] = []
            for i, slot in enumerate(track.clip_slots):
                if not slot.has_clip:
                    continue
                clip = self._serialize_clip(slot.clip, i)
                if include_notes and self._get(slot.clip, "is_midi_clip", False):
                    clip["notes"] = self._notes_from_clip(slot.clip)
                info["session_clips"].append(clip)
            if track_type == "regular":
                info["arrangement_clips"] = [
                    self._serialize_clip(c, i, brief=True)
                    for i, c in enumerate(track.arrangement_clips)
                ]
            return info

        return {
            "session": self._get_session_info({}, None),
            "tracks": [snapshot_track(t, i, "regular") for i, t in enumerate(song.tracks)],
            "return_tracks": [snapshot_track(t, i, "return")
                              for i, t in enumerate(song.return_tracks)],
            "master_track": snapshot_track(song.master_track, 0, "master"),
            "scenes": [self._serialize_scene(s, i) for i, s in enumerate(song.scenes)],
            "locators": self._get_locators({}, None)["locators"],
        }
