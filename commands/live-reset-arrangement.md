---
description: Clear the Arrangement timeline and its locators, for a fresh arranging pass
---

Clear the Arrangement so a new arranging pass starts from nothing.

This throws away work, so:

1. First call `get_arrangement_clips` for each track and `get_locators`, and
   show the user exactly what is about to be deleted: how many clips on which
   tracks, and the locator names with their bar numbers.
2. Ask for confirmation. Do not proceed without it.
3. Then call `clear_arrangement` and `clear_locators`.
4. Confirm what was removed.

The Session view clips are untouched by this. Say so, so the user knows their
source material is safe.

If the user named a range of bars, convert to beats and pass `from_time` and
`to_time` rather than clearing everything.

$ARGUMENTS
