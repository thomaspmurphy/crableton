---
name: ableton-live
description: Use when working on music in Ableton Live through the crableton MCP tools: writing or editing MIDI clips, arranging, mixing, choosing and dialling in instruments and effects, or drawing automation. Covers the conventions that are easy to get wrong, including beats-not-bars arithmetic, additive note writes, batching multi-step edits, and how automation actually works in Live.
---

# Working in Ableton Live

The crableton tools drive a live, open Live set that someone is listening to.
Treat it as shared, mutable state: read before you write, make changes in whole
musical units rather than a visible trickle, and never assume an index you have
not checked this turn.

## Orient before editing

Call `get_session_info` first. It is cheap and tells you the tempo, time
signature, track and scene names, and what is playing. `get_session_snapshot`
exists but returns the entire set. Reach for the targeted reads instead:
`get_track_info`, `get_clip`, `get_devices`, `get_arrangement_clips`.

Indices shift whenever a track, scene or clip is created or deleted. If you
have added or removed anything, re-read before using an index you cached.

## Time is in beats

Every time and duration is in beats, never bars or seconds. In 4/4 one bar is
4 beats, so:

| Bar | Beat |
| --- | ---- |
| 1   | 0    |
| 5   | 16   |
| 17  | 64   |
| 33  | 128  |

`bar_number` → `(bar_number - 1) * beats_per_bar`. Check the time signature in
`get_session_info` rather than assuming 4/4.

Clip times are relative to the clip's own start. Arrangement positions are
relative to the start of the Arrangement. These are different origins and
mixing them up is the most common way to place something in the wrong place.

## Batch multi-step edits

`batch` runs a sequence in one pass on Live's main thread. Use it for anything
beyond a couple of edits. It is much faster, and the user sees one change
rather than watching the set assemble itself step by step.

```
batch(steps: [
  { tool: "create_midi_track", arguments: { index: -1 } },
  { tool: "set_track_name",    arguments: { track_index: 4, name: "Bass" } },
  { tool: "create_clip",       arguments: { track_index: 4, clip_index: 0, length: 16 } },
])
```

Steps run in order and stop at the first failure unless `stop_on_error: false`.
`create_locator` cannot go in a batch, because it needs more than one pass.
Neither can the tools crableton answers itself: `lom_reference`,
`device_reference` and the `analyze_*` family.

## Notes are read, modify, write

`add_notes_to_clip` **appends**. Calling it twice with the same notes gives you
doubled notes, not an updated clip.

To change a clip: `get_clip_notes` → edit the list → `replace_clip_notes`. That
is atomic. Do not clear-then-add; it leaves the clip momentarily empty and costs
an extra round trip.

Notes carry `probability`, `velocity_deviation` and `release_velocity` on Live
11+. They are what make a programmed part feel less rigid: a hi-hat at
probability 0.8 with some velocity deviation beats a perfectly even one.

## Measure before you judge

Three tools compute facts you cannot otherwise get, since you cannot hear the
set. Use them instead of guessing, and quote what they say.

- `analyze_harmony` gives the estimated key with a confidence, the chord in
  each bar, and any notes outside the scale. Call it before writing a part that
  has to sit with existing material.
- `analyze_arrangement` infers sections from which tracks are actually playing,
  and reports what enters and exits at each transition. Good for understanding
  a set you did not write, and for checking a pass did what you intended.
- `analyze_mix` reports each track's pitch range and centre, and which pairs
  compete for the same register.

They report measurements, not opinions, and each carries a caveat about what it
cannot see. Respect those: register ranges are note fundamentals, so saturation
and unison move where a part really sits.

## Devices: read the parameters before setting them

`get_device_parameters` returns each parameter's name, range, the value as Live
displays it, and for quantized parameters the named settings in order. Use
it. A raw `0.63` means nothing, whereas `"2.5 kHz"` and
`value_items: ["LP", "HP", "BP", "Notch"]` tell you what you are setting.

Set parameters by `parameter_name` rather than index where you can; indices
shift between device versions. Use `set_device_parameters` to dial in several at
once.

To find instruments and effects, use `search_browser`, which returns the URIs
`load_device` needs. Walking `get_browser_tree` by hand is slow and the layout
varies per installation.

`device_reference` covers which parameters matter on Live's stock devices and
what they change musically, which `get_device_parameters` cannot tell you.
`lom_reference` describes Live's API surface, so consult it before concluding
something is impossible. When the two disagree with what you actually see,
`describe_live_object` interrogates the running instance directly.

## Automation

`set_clip_envelope` writes automation. Target a device parameter, or the track
mixer with `device_index: -1` (0 volume, 1 panning, 2 track on/off, 3+ sends).

Give it breakpoints and it fills in the shape:

```
set_clip_envelope(
  track_index: 1, clip_index: 0, device_index: 0, parameter_index: 1,
  points: [[0, 0.25], [16, 0.60], [32, 0.60], [48, 0.75]],
  interpolation: "linear",
)
```

Two things to know:

- Values are in the parameter's own units, so read the range from
  `get_device_parameters` first. A value outside it is clamped, not rejected.
- **Automation belongs to a clip, and only a Session clip.** Live refuses to
  create an envelope on an Arrangement clip, answering "Not a session clip".
  So sequence it: write the envelope on the Session clip first, then
  `duplicate_clip_to_arrangement`, and every copy carries the automation. A
  clip already sitting in the Arrangement cannot have automation created on it
  through the API; that needs the mouse. Say so plainly rather than implying
  it was written.
- `get_clip_envelope` samples the envelope rather than reading breakpoints, so
  what comes back is the shape, not the exact points you sent.

## Arranging

Build in the Session view, then place clips with
`duplicate_clip_to_arrangement`. Its `repeats` argument lays down consecutive
copies in one call: a 4-bar loop across 32 bars is one call, not eight.

Mark sections with `create_locator` before arranging; it makes the result
legible to the person looking at the screen.

To redo an arrangement pass, clear the old one first: `clear_arrangement`
(whole track, all tracks, or a beat range) and `clear_locators`. Otherwise you
are layering a new arrangement on top of the old one.

## Destructive changes

`undo` reaches into the user's own edit history, not just yours, so it can
throw away work you never touched. Say what you are about to undo first.

The same care applies to `delete_track`, `clear_arrangement`, `crop_clip` and
`replace_clip_notes`: confirm first unless the user just asked for exactly that.

## Reporting back

The person can hear the result, so do not narrate every call. Say what changed
musically ("eight-bar intro on the pad, filter opening from bar 5"), and be
straight about anything that did not work rather than describing the intent as
though it landed.
