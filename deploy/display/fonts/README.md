# The panel's typefaces

TrueType faces in `ttf/`, loaded by `panel.py` at startup; which theme wears
which face — and at what sizes — is `theme.py`'s to say.

All three are under the **SIL Open Font License 1.1**, which permits
redistribution; each family's `OFL.txt` travels with it, as the licence asks.

| Face | Used by | Copyright |
|---|---|---|
| Orbitron (variable) | `hud` theme | Copyright The Orbitron Project Authors |
| Rajdhani **Medium** | `soft` theme | Copyright The Rajdhani Project Authors |
| Exo 2 (variable) | `neon` theme | Copyright The Exo 2 Project Authors |

Until 2026-08-16 this directory also held `.npz` glyph atlases — raster
snapshots of these faces at fixed sizes, baked for the numpy renderer because
the Pi has no font engine. That renderer retired with the numpy menu; Qt
rasterises the TTFs itself. The weights here were recovered by re-baking
atlases from candidate files and diffing against the shipped ones — Rajdhani
**Medium** matched byte for byte and Regular did not — so if a face is ever
replaced, know that the exact weight matters and was once proven, not guessed.
