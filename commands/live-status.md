---
description: Show what is currently open in Ableton Live
---

Report the state of the open Live set.

Call `get_session_info`, then `get_tracks` with `include_clips: true`. Present:

- Tempo, time signature, and whether the transport is running.
- A table of tracks: index, name, type, and what is playing on each.
- The scenes, and the Arrangement locators from `get_locators` with their bar
  numbers as well as their beat positions.

Convert beats to bars for the reader: beat 64 in 4/4 is bar 17. Keep it to
what fits on a screen and do not dump raw JSON.

If Live is not reachable, say so and suggest `crableton doctor`.
