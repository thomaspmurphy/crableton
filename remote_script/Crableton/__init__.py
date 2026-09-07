# Crableton — the Live-side half of the crableton MCP server.
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
import socket
import threading
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

SCRIPT_VERSION = "2.0.0"
PROTOCOL_VERSION = 2

# Sentinel returned by handlers that will answer on a later tick.
DEFERRED = object()

# How many main-thread ticks a deferred handler may wait before giving up.
MAX_DEFER_TICKS = 40


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
            self.show_message("Crableton: port %d unavailable — %s" % (DEFAULT_PORT, e))

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
        clip_envelopes = False
        try:
            # Feature-detect rather than assume: the envelope API is not
            # uniformly present across Live versions and editions.
            for track in song.tracks:
                for slot in track.clip_slots:
                    if slot.has_clip:
                        clip_envelopes = hasattr(slot.clip, "automation_envelope")
                        raise StopIteration
        except StopIteration:
            pass
        except Exception:
            pass

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
                "extended_notes": hasattr(song, "view"),
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
            # devices
            "get_devices": self._get_devices,
            "get_device_parameters": self._get_device_parameters,
            "set_device_parameter": self._set_device_parameter,
            "set_device_parameters": self._set_device_parameters,
            "set_device_enabled": self._set_device_enabled,
            "delete_device": self._delete_device,
            "get_rack_chains": self._get_rack_chains,
            "set_chain_mixer": self._set_chain_mixer,
            # browser
            "search_browser": self._search_browser,
            "get_browser_tree": self._get_browser_tree,
            "get_browser_items_at_path": self._get_browser_items_at_path,
            "load_browser_item": self._load_browser_item,
            "load_drum_kit": self._load_drum_kit,
            # arrangement
            "get_arrangement_clips": self._get_arrangement_clips,
            "duplicate_clip_to_arrangement": self._duplicate_clip_to_arrangement,
            "delete_arrangement_clip": self._delete_arrangement_clip,
            "clear_arrangement": self._clear_arrangement,
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
        if track_type == "master":
            return song.master_track
        collection = song.return_tracks if track_type == "return" else song.tracks
        index = int(params.get("track_index", 0))
        if not 0 <= index < len(collection):
            raise IndexError(
                "%s track %d does not exist (there are %d)"
                % (track_type, index, len(collection))
            )
        return collection[index]

    def _resolve_clip(self, params):
        track = self._resolve_track(params)
        index = int(params.get("clip_index", 0))
        view = (params.get("view") or "session").lower()

        if view == "arrangement":
            clips = list(track.arrangement_clips)
            if not 0 <= index < len(clips):
                raise IndexError(
                    "arrangement clip %d does not exist on this track (there are %d)"
                    % (index, len(clips))
                )
            return clips[index]

        slot = self._resolve_slot(track, index)
        if not slot.has_clip:
            raise ValueError("clip slot %d is empty" % index)
        return slot.clip

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
        song = self._song_ref
        view = song.view
        selected = {}

        if params.get("track_index") is not None:
            index = int(params["track_index"])
            if not 0 <= index < len(song.tracks):
                raise IndexError("track %d does not exist" % index)
            view.selected_track = song.tracks[index]
            selected["track"] = song.tracks[index].name
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
        """How Live shows a parameter — "-6.0 dB" rather than 0.63."""
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

        if params.get("volume") is not None:
            mixer.volume.value = self._clamp_parameter(mixer.volume, params["volume"])
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

        return {
            "changed": [c for c in changed if c],
            "volume": mixer.volume.value,
            "volume_display": self._display_value(mixer.volume),
            "panning": mixer.panning.value,
        }

    @staticmethod
    def _clamp_parameter(parameter, value):
        """Keep writes inside the parameter's range — Live raises otherwise."""
        return max(parameter.min, min(parameter.max, float(value)))

    def _set_track_send(self, params, respond):
        track = self._resolve_track(params)
        sends = track.mixer_device.sends
        index = int(params["send_index"])
        if not 0 <= index < len(sends):
            raise IndexError(
                "send %d does not exist; this set has %d return track(s)"
                % (index, len(sends))
            )
        sends[index].value = self._clamp_parameter(sends[index], params["value"])
        return {"send_index": index, "value": sends[index].value,
                "display": self._display_value(sends[index])}

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
                "clip slot %d already holds %r — delete it first if you mean to replace it"
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
        source_track = self._resolve_track(params)
        source = self._resolve_slot(source_track, int(params["clip_index"]))
        if not source.has_clip:
            raise ValueError("clip slot %d is empty" % int(params["clip_index"]))

        target_track = source_track
        if params.get("target_track_index") is not None:
            target_params = dict(params)
            target_params["track_index"] = int(params["target_track_index"])
            target_track = self._resolve_track(target_params)

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
        the parameter yet — it does not create one. Creating is a separate call
        whose name has varied across Live versions, so try what is present
        rather than assuming.
        """
        clip = self._resolve_clip(params)
        _, parameter = self._resolve_parameter(params)
        if not hasattr(clip, "automation_envelope"):
            raise RuntimeError("this Live version does not expose clip automation")

        envelope = clip.automation_envelope(parameter)
        if envelope is None:
            # An Arrangement clip duplicated from an automated Session clip
            # keeps its envelopes, but `automation_envelope(parameter)` will not
            # resolve them — the parameter object belongs to the track, not the
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
                "first and then use duplicate_clip_to_arrangement — the placed copies "
                "keep it, and can be rewritten in place afterwards."
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
            result["read_as"] = "breakpoints"
            result["points"] = events
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
        """The envelope's actual breakpoints, or None if Live will not list them."""
        reader = getattr(envelope, "events_in_range", None)
        if reader is None:
            return None
        try:
            events = reader(0.0, length + 1e-6)
        except Exception as e:
            self.log_message("events_in_range failed: %s" % e)
            return None

        points = []
        for event in events or ():
            time_value = self._get(event, "time")
            value = self._get(event, "value")
            if time_value is None or value is None:
                continue
            points.append([round(float(time_value), 6), float(value)])
        return points

    def _set_clip_envelope(self, params, respond):
        clip, parameter, envelope = self._envelope_for(params, create=True)

        points = self._validated_points(params.get("points") or [], parameter)
        interpolation = (params.get("interpolation") or "linear").lower()
        resolution = float(params.get("resolution", 0.0625))
        if resolution <= 0:
            raise ValueError("resolution must be greater than 0")

        clip_length = float(self._get(clip, "length", 0.0) or 0.0)
        span = max(clip_length, points[-1][0])

        # Clear the old shape *without* dropping the envelope itself.
        # `clip.clear_envelope` destroys it, and Live will not recreate one on
        # an Arrangement clip — so that route is a one-way door there.
        cleared = False
        if hasattr(envelope, "delete_events_in_range"):
            try:
                envelope.delete_events_in_range(0.0, span + 1.0)
                cleared = True
            except Exception as e:
                self.log_message("delete_events_in_range failed: %s" % e)
        if not cleared:
            if self._get(clip, "is_arrangement_clip", False):
                raise RuntimeError(
                    "cannot replace this Arrangement clip's envelope: Live offers no way "
                    "to clear it in place, and clearing it outright cannot be undone"
                )
            try:
                clip.clear_envelope(parameter)
            except Exception as e:
                self.log_message("clear_envelope(%s): %s" % (parameter.name, e))
            _, _, envelope = self._envelope_for(params, create=True)

        # Breakpoints are what Live's own automation lane holds, and it ramps
        # between them natively — so a smooth curve is a handful of events, not
        # hundreds of stepped approximations. Steps are only for a deliberate
        # staircase, or where `create_event` is unavailable.
        use_events = interpolation != "step" and hasattr(envelope, "create_event")
        if use_events:
            written = self._write_breakpoints(envelope, points, span)
            method = "breakpoints"
        elif hasattr(envelope, "insert_step"):
            written = 0
            for start, length, value in self._envelope_steps(
                    points, interpolation, resolution, clip_length):
                envelope.insert_step(start, length, value)
                written += 1
            method = "steps"
        else:
            raise RuntimeError(
                "this envelope cannot be written to; it offers: %s"
                % ", ".join(n for n in dir(envelope) if not n.startswith("_"))
            )

        result = {
            "parameter": parameter.name,
            "written": written,
            "wrote_as": method,
            "interpolation": interpolation,
            "range": [parameter.min, parameter.max],
        }
        if method == "steps":
            result["resolution"] = resolution
        return result

    @staticmethod
    def _write_breakpoints(envelope, points, span):
        """Write points as automation events, letting Live ramp between them."""
        written = 0
        for time_value, value in points:
            envelope.create_event(time_value, value)
            written += 1

        # Hold the last value to the end of the clip; without a closing event
        # Live ramps on from the final breakpoint to whatever follows.
        last_time, last_value = points[-1]
        if span > last_time:
            envelope.create_event(span, last_value)
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
        clip = self._resolve_clip(params)
        _, parameter = self._resolve_parameter(params)
        clip.clear_envelope(parameter)
        return {"cleared": parameter.name}

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

    def _notes_from_clip(self, clip, window=None):
        """Read notes, preferring the extended API for its per-note attributes."""
        from_pitch, pitch_span, from_time, time_span = window or (0, 128, 0.0, 1000000.0)

        if hasattr(clip, "get_notes_extended"):
            notes = []
            for note in clip.get_notes_extended(from_pitch, pitch_span, from_time, time_span):
                notes.append({
                    "note_id": self._get(note, "note_id"),
                    "pitch": self._get(note, "pitch"),
                    "start_time": self._get(note, "start_time"),
                    "duration": self._get(note, "duration"),
                    "velocity": self._get(note, "velocity"),
                    "mute": self._get(note, "mute"),
                    "probability": self._get(note, "probability"),
                    "velocity_deviation": self._get(note, "velocity_deviation"),
                    "release_velocity": self._get(note, "release_velocity"),
                })
            return notes

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
        parameter.value = self._clamp_parameter(parameter, params["value"])
        return {
            "device": device.name if device else "mixer",
            "parameter": parameter.name,
            "from": previous,
            "to": parameter.value,
            "display": self._display_value(parameter),
        }

    def _set_device_parameters(self, params, respond):
        device = self._resolve_device(params)
        by_name = {p.name.strip().lower(): p for p in device.parameters}
        applied, failed = [], []

        for i, entry in enumerate(params.get("values") or []):
            if not isinstance(entry, dict) or "value" not in entry:
                failed.append({"at": i, "error": "each entry needs a value and a name or index"})
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
             "uri": params["rack_uri"]}, None)

        listing = self._get_browser_items_at_path({"path": params["kit_path"]}, None)
        loadable = [i for i in listing["items"] if i["is_loadable"]]
        if not loadable:
            raise ValueError("no loadable kit found at %r" % params["kit_path"])

        kit = self._load_browser_item(
            {"track_index": params.get("track_index"),
             "track_type": params.get("track_type"),
             "uri": loadable[0]["uri"]}, None)
        return {"rack": rack["loaded"], "kit": kit["loaded"], "track": kit["track"]}

    # ── Arrangement ──────────────────────────────────────────────────────────

    def _get_arrangement_clips(self, params, respond):
        track = self._resolve_track(params)
        clips = list(track.arrangement_clips)
        return {
            "track": track.name,
            "count": len(clips),
            "clips": [self._serialize_clip(c, i, brief=True) for i, c in enumerate(clips)],
        }

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
        placed = []
        for n in range(repeats):
            at = start + n * length
            track.duplicate_clip_to_arrangement(source, at)
            placed.append(at)

        return {
            "clip": source.name,
            "track": track.name,
            "placed_at": placed,
            "through": start + repeats * length,
        }

    def _delete_arrangement_clip(self, params, respond):
        track = self._resolve_track(params)
        clips = list(track.arrangement_clips)
        index = int(params["clip_index"])
        if not 0 <= index < len(clips):
            raise IndexError(
                "arrangement clip %d does not exist on %r (there are %d)"
                % (index, track.name, len(clips))
            )
        clip = clips[index]
        name, start = clip.name, self._get(clip, "start_time")
        track.delete_clip(clip)
        return {"deleted": name, "was_at": start, "remaining": len(track.arrangement_clips)}

    def _clear_arrangement(self, params, respond):
        song = self._song_ref
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

    def _find_cue(self, time_value, tolerance=1e-3):
        for cue in self._song_ref.cue_points:
            if abs(cue.time - time_value) < tolerance:
                return cue
        return None

    def _create_locator(self, params, respond):
        """Place a locator at a beat position.

        Live can only toggle a cue at the playhead, and writing
        current_song_time does not necessarily take effect within the same
        tick — so the toggle waits for the playhead to actually arrive rather
        than assuming it has. Toggling early creates a stray locator at the old
        position and then fails to find the intended one.
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

        original_time = song.current_song_time
        was_playing = song.is_playing
        song.current_song_time = target

        state = {"ticks": 0}

        def toggle():
            state["ticks"] += 1
            if abs(song.current_song_time - target) > 1e-3:
                if state["ticks"] > MAX_DEFER_TICKS:
                    respond({"error": "Live would not move the playhead to beat %s" % target})
                    return
                self._defer(1, toggle)
                return

            try:
                song.set_or_delete_cue()
                cue = self._find_cue(target)
                if cue is None:
                    respond({"error": "Live did not create a locator at beat %s" % target})
                    return
                if name:
                    cue.name = name
                # Only restore the playhead when the transport is stopped;
                # moving it mid-playback would audibly jump the set.
                if not was_playing:
                    song.current_song_time = original_time
                respond({"ok": {"name": cue.name, "time": cue.time, "created": True}})
            except Exception as e:
                respond({"error": str(e)})

        self._defer(1, toggle)
        return DEFERRED

    def _delete_locator(self, params, respond):
        cues = list(self._song_ref.cue_points)
        index = int(params["index"])
        if not 0 <= index < len(cues):
            raise IndexError("locator %d does not exist (there are %d)" % (index, len(cues)))
        cue = cues[index]
        name, time_value = cue.name, cue.time
        # A cue knows how to move the playhead onto itself, which is exactly
        # the position set_or_delete_cue needs in order to remove it.
        cue.jump()
        self._song_ref.set_or_delete_cue()
        return {"deleted": name, "was_at": time_value,
                "remaining": len(self._song_ref.cue_points)}

    def _clear_locators(self, params, respond):
        from_time = params.get("from_time")
        to_time = params.get("to_time")
        deleted = []

        while True:
            target = None
            for cue in self._song_ref.cue_points:
                if from_time is not None and cue.time < float(from_time):
                    continue
                if to_time is not None and cue.time >= float(to_time):
                    continue
                target = cue
                break
            if target is None:
                break

            name, time_value = target.name, target.time
            target.jump()
            self._song_ref.set_or_delete_cue()
            deleted.append({"name": name, "time": time_value})

            if len(deleted) > 512:  # guard against a cue that will not delete
                break

        return {"deleted_count": len(deleted), "deleted": deleted,
                "remaining": len(self._song_ref.cue_points)}

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
