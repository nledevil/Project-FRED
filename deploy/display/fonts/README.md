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
| Orbitron | `hud` theme | Copyright The Orbitron Project Authors |
| Rajdhani | `soft` theme | Copyright The Rajdhani Project Authors |
| Exo 2 | `neon` theme | Copyright The Exo 2 Project Authors |

Full licence text: <https://openfontlicense.org> — and each family's own
`OFL.txt` at <https://github.com/google/fonts>.

To re-bake, or to add a size (the pages pick the largest scale a label fits at,
walking 3, 2, 1, so every scale a theme names must actually exist):

    python3 tools/bake_font.py Rajdhani.ttf 30 --out fonts/rajdhani-30.npz
