# Terminator mode clips

Drop movie sound clips in **this folder** as `.wav` files. When terminator mode
is engaged (red LED on — "terminator mode on"), FRED plays **one of these at
random** instead of speaking a synthesized catchphrase. If this folder is empty,
he falls back to a spoken one-liner.

## Requirements
- Format: **`.wav`** (any sample rate — `aplay` on `plughw:0,0` auto-converts).
  MP3/M4A won't play; convert them to WAV first.
- Filename doesn't matter — any `*.wav` here is eligible. Keep clips short
  (a single line, ~1–4 s) so they don't drag.
- Example: `ill-be-back.wav`, `hasta-la-vista.wav`, `come-with-me.wav`.

## Notes
- These files are git-ignored (`sounds/terminator/*.wav`) — movie audio is
  copyrighted, so it stays on this Pi and out of the repo. This is a personal,
  non-commercial hobby build.
- Wired up in `inmoov/commands.py` (`set_led` action → `sound.play_random("terminator")`).
