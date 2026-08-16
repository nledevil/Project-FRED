# Baked font atlases

Anti-aliased glyph coverage maps for the chest panel, produced from TrueType
faces by `../tools/bake_font.py` on the NUC. The Pi has no Pillow and no system
fonts, so rasterising happens once, offline, and this directory is what ships.
See `../font_atlas.py` for the reader and `../theme.py` for which theme uses
which size.

These are derived from the fonts below, all under the **SIL Open Font License
1.1**, which permits redistribution of the originals and of derivatives — this
notice travels with them because the licence asks that it does.

| Face | Used by | Copyright |
|---|---|---|
| Orbitron (variable) | `hud` theme | Copyright The Orbitron Project Authors |
| Rajdhani **Medium** | `soft` theme | Copyright The Rajdhani Project Authors |
| Exo 2 (variable) | `neon` theme | Copyright The Exo 2 Project Authors |

The originals are in `ttf/`, with each family's `OFL.txt`. They were not here
until 2026-08-15, which meant these atlases could not be re-baked and no other
renderer could use the panel's typefaces at all — a Qt build of the menu would
have fallen back to DejaVu and quietly changed how the whole panel reads.

The exact weight was recovered rather than guessed: `tools/bake_font.py` was run
against each candidate and the result compared to the shipped atlas. Rajdhani
**Medium** matches byte for byte and Regular does not, and the two variable
fonts match at their default instance. If a face is ever replaced, re-run that
comparison — the atlases are the record of what the panel is supposed to look
like.

Full licence text: <https://openfontlicense.org> — and each family's own
`OFL.txt` at <https://github.com/google/fonts>.

To re-bake, or to add a size (the pages pick the largest scale a label fits at,
walking 3, 2, 1, so every scale a theme names must actually exist):

    python3 tools/bake_font.py Rajdhani.ttf 30 --out fonts/rajdhani-30.npz
