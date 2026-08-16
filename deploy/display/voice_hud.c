/* Voice telemetry HUD for the InMoov chest screen — native renderer.
 *
 * A port of voice_hud.py. Same picture, same /dev/shm seams, same "child owns
 * the framebuffer" contract; the difference is where the pixels go. The Python
 * version builds a float32 (H,W,3) frame and then makes five separate passes
 * over it — copy, composite, clip, convert, pack — to feed a panel that only
 * wants 750 KB of RGB565. That is ~70% of a core at 30fps on a Pi 4, and it is
 * memory bandwidth, not arithmetic.
 *
 * Here the compositing stays in float (so the picture is identical — see the
 * note on clip order below), but the back end collapses to ONE pass: clip,
 * quantise and pack straight into the mmap'd framebuffer, with no intermediate
 * uint8 frame and no temporary buffers at all.
 *
 * On clip order: voice_hud.py clips the whole frame to 0..255 *before* it calls
 * hud.draw(), which is what applies the metrics panel's `*= DIM`. So a pixel
 * that accumulated past 255 is flattened to 255 first and dimmed second. That
 * makes saturation-at-composite-time exactly equivalent, and it is why this can
 * be a faithful port rather than an approximation.
 *
 * Build:  gcc -O2 -mcpu=cortex-a72 -o voice_hud voice_hud.c -lm
 * Usage:  ./voice_hud                  # run forever (SIGTERM/SIGINT to stop)
 *         ./voice_hud --seconds 6      # run 6s then quit (for testing)
 *         ./voice_hud --fps 30
 *
 * The --sim/--dump options exist only for the equivalence harness: they replace
 * the wall clock with a frame-indexed one and write raw RGB888 frames out, so
 * the render can be diffed pixel-for-pixel against the Python original.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>

/* ------------------------------------------------------------------ config */

#define VOICE_PATH_DEF   "/dev/shm/inmoov-voice.json"
#define METRICS_PATH_DEF "/dev/shm/inmoov-metrics.json"

#define MAX_LEVELS 65536
#define MAX_DOC    (1 << 20)
#define MAX_LINES  16
#define MAX_TEXT   64

/* How often to repaint every row regardless of damage, to erase anything the
 * kernel console drew over us. 30 frames = about a second at the default rate. */
#define FULL_REPAINT_FRAMES 30

/* metrics_hud.py layout */
#define M_SCALE 2
#define M_LINE_H 18
#define M_PAD 8
#define M_MARGIN 12
#define M_DIM 0.25f
#define M_STALE_AFTER 10.0
#define M_NO_ECHO_CM 399.0

/* cog_hud.py layout. Kept in the same shape and the same order of operations as
 * that file, because tools/verify_voice_hud.py compares the two pixel for pixel
 * and the cog is drawn on every frame. */
#define COG_N      24                 /* the bitmap is 24x24 */
#define COG_SCALE  2
#define COG_PAD    8
#define COG_MARGIN 12
#define COG_DIM    0.25f
#define COG_BOX    (COG_N * COG_SCALE + COG_PAD * 2)   /* 64 */

typedef struct { float r, g, b; } rgb_t;

/* The palette is the theme's, chosen at startup — see apply_theme(). Not const
 * any more, and not written by hand: theme_colors.h is generated from theme.py
 * by tools/gen_theme_colors.py, which is also where voice_hud.py gets these,
 * so the two renderers cannot drift. tools/verify_voice_hud.py checks it. */
static rgb_t CYAN  = { 90.f, 210.f, 255.f };
static rgb_t GREEN = { 90.f, 255.f, 150.f };
static rgb_t AMBER = { 255.f, 180.f,  60.f };
static rgb_t WHITE = { 225.f, 245.f, 255.f };

static rgb_t TITLE_RGB = {  90.f, 150.f, 190.f };
static rgb_t VALUE_RGB = { 120.f, 210.f, 255.f };
static rgb_t ALERT_RGB = { 230.f, 120.f,  90.f };

#include "theme_colors.h"

enum { ST_IDLE, ST_LISTENING, ST_THINKING, ST_SPEAKING };
/* The state word is the point of this screen; everything else is texture.
 * 11 puts LISTENING — the longest of the four — at 583 px across an 800 px
 * panel, and 77 px tall, which clears the trace window that starts at 0.30 H. */
#define STATE_SCALE_MAX 11
#define STATE_MARGIN    48
static const char *STATE_NAME[] = { "idle", "listening", "thinking", "speaking" };

/* ------------------------------------------------------------------- 5x7 font
 * Same glyphs as font5x7.py, one byte per row, bit 4 = leftmost pixel.        */

typedef struct { char ch; uint8_t rows[7]; } glyph_t;

#define G(a,b,c,d,e,f,g) { a,b,c,d,e,f,g }
static const glyph_t GLYPHS[] = {
    {'A', G(0x0E,0x11,0x11,0x1F,0x11,0x11,0x11)},
    {'B', G(0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E)},
    {'C', G(0x0E,0x11,0x10,0x10,0x10,0x11,0x0E)},
    {'D', G(0x1E,0x11,0x11,0x11,0x11,0x11,0x1E)},
    {'E', G(0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F)},
    {'F', G(0x1F,0x10,0x10,0x1E,0x10,0x10,0x10)},
    {'G', G(0x0E,0x11,0x10,0x17,0x11,0x11,0x0F)},
    {'H', G(0x11,0x11,0x11,0x1F,0x11,0x11,0x11)},
    {'I', G(0x1F,0x04,0x04,0x04,0x04,0x04,0x1F)},
    {'J', G(0x07,0x02,0x02,0x02,0x02,0x12,0x0C)},
    {'K', G(0x11,0x12,0x14,0x18,0x14,0x12,0x11)},
    {'L', G(0x10,0x10,0x10,0x10,0x10,0x10,0x1F)},
    {'M', G(0x11,0x1B,0x15,0x15,0x11,0x11,0x11)},
    {'N', G(0x11,0x11,0x19,0x15,0x13,0x11,0x11)},
    {'O', G(0x0E,0x11,0x11,0x11,0x11,0x11,0x0E)},
    {'P', G(0x1E,0x11,0x11,0x1E,0x10,0x10,0x10)},
    {'Q', G(0x0E,0x11,0x11,0x11,0x15,0x12,0x0D)},
    {'R', G(0x1E,0x11,0x11,0x1E,0x14,0x12,0x11)},
    {'S', G(0x0F,0x10,0x10,0x0E,0x01,0x01,0x1E)},
    {'T', G(0x1F,0x04,0x04,0x04,0x04,0x04,0x04)},
    {'U', G(0x11,0x11,0x11,0x11,0x11,0x11,0x0E)},
    {'V', G(0x11,0x11,0x11,0x11,0x11,0x0A,0x04)},
    {'W', G(0x11,0x11,0x11,0x15,0x15,0x1B,0x11)},
    {'X', G(0x11,0x11,0x0A,0x04,0x0A,0x11,0x11)},
    {'Y', G(0x11,0x11,0x0A,0x04,0x04,0x04,0x04)},
    {'Z', G(0x1F,0x01,0x02,0x04,0x08,0x10,0x1F)},
    {'0', G(0x0E,0x11,0x13,0x15,0x19,0x11,0x0E)},
    {'1', G(0x04,0x0C,0x04,0x04,0x04,0x04,0x0E)},
    {'2', G(0x0E,0x11,0x01,0x02,0x04,0x08,0x1F)},
    {'3', G(0x1F,0x02,0x04,0x02,0x01,0x11,0x0E)},
    {'4', G(0x02,0x06,0x0A,0x12,0x1F,0x02,0x02)},
    {'5', G(0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E)},
    {'6', G(0x06,0x08,0x10,0x1E,0x11,0x11,0x0E)},
    {'7', G(0x1F,0x01,0x02,0x04,0x08,0x08,0x08)},
    {'8', G(0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E)},
    {'9', G(0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C)},
    {' ', G(0x00,0x00,0x00,0x00,0x00,0x00,0x00)},
    {'.', G(0x00,0x00,0x00,0x00,0x00,0x0C,0x0C)},
    {':', G(0x00,0x0C,0x0C,0x00,0x0C,0x0C,0x00)},
    {'-', G(0x00,0x00,0x00,0x1F,0x00,0x00,0x00)},
    {'/', G(0x01,0x02,0x02,0x04,0x08,0x08,0x10)},
};
#define NGLYPHS ((int)(sizeof(GLYPHS)/sizeof(GLYPHS[0])))
#define CHAR_W 5
#define CHAR_H 7

static const uint8_t *glyph_for(char c)
{
    if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');       /* text.upper() */
    for (int i = 0; i < NGLYPHS; i++)
        if (GLYPHS[i].ch == c) return GLYPHS[i].rows;
    return NULL;                                               /* unknown: blank */
}

static int text_width(const char *s, int scale, int spacing)
{
    int n = (int)strlen(s);
    return n * (CHAR_W + spacing) * scale - spacing * scale;
}

/* -------------------------------------------------------------- framebuffer */

typedef struct {
    int w, h, bpp, stride;
    size_t size;
    int fd;
    uint8_t *mm;
} fb_t;

static int sysfs_int(const char *fbname, const char *attr, int *out)
{
    char p[256], buf[64];
    snprintf(p, sizeof p, "/sys/class/graphics/%s/%s", fbname, attr);
    int fd = open(p, O_RDONLY);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = 0;
    *out = atoi(buf);
    return 0;
}

static int fb_open(fb_t *fb, const char *dev)
{
    const char *base = strrchr(dev, '/');
    base = base ? base + 1 : dev;
    char vs[64], p[256];
    snprintf(p, sizeof p, "/sys/class/graphics/%s/virtual_size", base);
    int fd = open(p, O_RDONLY);
    if (fd < 0) { perror("virtual_size"); return -1; }
    ssize_t n = read(fd, vs, sizeof vs - 1);
    close(fd);
    if (n <= 0) return -1;
    vs[n] = 0;
    if (sscanf(vs, "%d,%d", &fb->w, &fb->h) != 2) return -1;
    if (sysfs_int(base, "bits_per_pixel", &fb->bpp) < 0) return -1;
    if (sysfs_int(base, "stride", &fb->stride) < 0) return -1;
    if (fb->bpp != 16 && fb->bpp != 32) {
        fprintf(stderr, "%s is %dbpp; this renderer does RGB565 (16bpp) and "
                "XRGB8888 (32bpp)\n", dev, fb->bpp);
        return -1;
    }
    fb->size = (size_t)fb->stride * fb->h;
    fb->fd = open(dev, O_RDWR);
    if (fb->fd < 0) { perror(dev); return -1; }
    fb->mm = mmap(NULL, fb->size, PROT_READ | PROT_WRITE, MAP_SHARED, fb->fd, 0);
    if (fb->mm == MAP_FAILED) { perror("mmap"); close(fb->fd); return -1; }
    return 0;
}

static void fb_clear(fb_t *fb) { if (fb->mm) memset(fb->mm, 0, fb->size); }

static void fb_close(fb_t *fb)
{
    if (fb->mm) munmap(fb->mm, fb->size);
    if (fb->fd >= 0) close(fb->fd);
    fb->mm = NULL; fb->fd = -1;
}

static void hide_cursor(void)
{
    int fd = open("/sys/class/graphics/fbcon/cursor_blink", O_WRONLY);
    if (fd >= 0) { ssize_t r = write(fd, "0", 1); (void)r; close(fd); }
}

/* --------------------------------------------------------- minimal JSON read
 * Only what the two /dev/shm docs contain: flat objects of numbers, strings,
 * booleans, one array of numbers, and one nested object of objects.          */

static const char *js_skip_ws(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}

static const char *js_skip_value(const char *p);

static const char *js_skip_string(const char *p)
{
    p++;                                                  /* opening quote */
    while (*p && *p != '"') { if (*p == '\\' && p[1]) p++; p++; }
    return *p ? p + 1 : p;
}

static const char *js_skip_value(const char *p)
{
    p = js_skip_ws(p);
    if (*p == '"') return js_skip_string(p);
    if (*p == '{' || *p == '[') {
        char open = *p, close = (open == '{') ? '}' : ']';
        int depth = 0;
        while (*p) {
            if (*p == '"') { p = js_skip_string(p); continue; }
            if (*p == open) depth++;
            else if (*p == close) { depth--; if (!depth) return p + 1; }
            p++;
        }
        return p;
    }
    while (*p && *p != ',' && *p != '}' && *p != ']') p++;
    return p;
}

/* Find `key` directly inside the object starting at `obj`. NULL if absent. */
static const char *js_find(const char *obj, const char *key)
{
    if (!obj) return NULL;
    const char *p = js_skip_ws(obj);
    if (*p != '{') return NULL;
    p++;
    size_t klen = strlen(key);
    for (;;) {
        p = js_skip_ws(p);
        if (*p == '}' || !*p) return NULL;
        if (*p != '"') return NULL;
        const char *ks = p + 1;
        const char *ke = js_skip_string(p);
        size_t n = (size_t)(ke - ks) - 1;
        p = js_skip_ws(ke);
        if (*p != ':') return NULL;
        p++;
        const char *val = js_skip_ws(p);
        if (n == klen && strncmp(ks, key, klen) == 0) return val;
        p = js_skip_value(val);
        p = js_skip_ws(p);
        if (*p == ',') p++;
    }
}

static bool js_num(const char *v, double *out)
{
    if (!v) return false;
    if (*v != '-' && *v != '+' && *v != '.' && (*v < '0' || *v > '9')) return false;
    char *end;
    double d = strtod(v, &end);
    if (end == v) return false;
    *out = d;
    return true;
}

static bool js_bool(const char *v)
{
    return v && strncmp(v, "true", 4) == 0;
}

static bool js_str(const char *v, char *out, size_t cap)
{
    if (!v || *v != '"') return false;
    v++;
    size_t i = 0;
    while (*v && *v != '"' && i + 1 < cap) {
        if (*v == '\\' && v[1]) v++;
        out[i++] = *v++;
    }
    out[i] = 0;
    return true;
}

static int js_num_array(const char *v, float *out, int cap)
{
    if (!v || *v != '[') return -1;
    v++;
    int n = 0;
    for (;;) {
        v = js_skip_ws(v);
        if (*v == ']' || !*v) return n;
        double d;
        if (!js_num(v, &d)) return n;
        if (n < cap) out[n] = (float)d;
        n++;
        v = js_skip_value(v);
        v = js_skip_ws(v);
        if (*v == ',') v++;
    }
}

/* ------------------------------------------------------------------- theme
 * The chest panel remembers its theme in state.json beside the animation pick,
 * and the daemon stops this process to hand the screen to the settings menu —
 * so by the time the menu has changed the theme, this renderer has already
 * exited. Reading it once at startup is therefore live, and costs one open(). */

static void apply_theme(const char *name)
{
    for (size_t i = 0; i < THEME_COLORS_N; i++) {
        if (strcmp(THEME_COLORS[i].name, name)) continue;
        const theme_colors_t *t = &THEME_COLORS[i];
        CYAN = t->base;  WHITE = t->white;
        GREEN = t->green; AMBER = t->amber;
        TITLE_RGB = t->title; VALUE_RGB = t->value; ALERT_RGB = t->alert;
        return;
    }
    /* An unknown name keeps the compiled-in defaults rather than failing: a
     * panel that cannot read its own preferences should still draw. */
}

static void theme_from_state(const char *path, char *out, size_t cap)
{
    out[0] = '\0';
    FILE *f = fopen(path, "rb");
    if (!f) return;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = '\0';
    const char *v = js_find(buf, "theme");
    if (v) js_str(v, out, cap);
}

/* ------------------------------------------------------------- shm doc feeds
 * Mirrors VoiceFeed/MetricsFeed: stat the file every frame, re-read only when
 * the mtime moves, keep the last good parse if a read lands mid-rename.      */

typedef struct {
    char path[256];
    struct timespec mtime;
    bool have;
    char buf[MAX_DOC];
} feed_t;

static void feed_init(feed_t *f, const char *path)
{
    snprintf(f->path, sizeof f->path, "%s", path);
    f->mtime.tv_sec = -1; f->mtime.tv_nsec = -1;
    f->have = false;
    f->buf[0] = 0;
}

/* True if the document changed this call. */
static bool feed_poll(feed_t *f)
{
    struct stat st;
    if (stat(f->path, &st) != 0) return false;
    if (st.st_mtim.tv_sec == f->mtime.tv_sec && st.st_mtim.tv_nsec == f->mtime.tv_nsec)
        return false;
    int fd = open(f->path, O_RDONLY);
    if (fd < 0) return false;
    ssize_t n = read(fd, f->buf, sizeof f->buf - 1);
    close(fd);
    if (n < 0) return false;                        /* keep the last good doc */
    f->buf[n] = 0;
    f->mtime = st.st_mtim;
    f->have = true;
    return true;
}

/* -------------------------------------------------------------- frame buffer
 * One float32 RGB working frame, exactly as the Python version composites.   */

typedef struct {
    int W, H;
    float *chrome;          /* static HUD furniture, built once   */
    float *frame;           /* working frame                      */
    /* Damage tracking. Only rows that were drawn into need restoring from
     * chrome, and only rows that changed need packing to the panel — which is
     * what turns three full-frame passes into work proportional to the ~25% of
     * rows this HUD actually lights up. */
    uint8_t *dirty, *prev_dirty;
    bool track;             /* off while chrome is being built    */
} canvas_t;

static void mark(canvas_t *c, int y0, int y1)
{
    if (!c->track) return;
    if (y0 < 0) y0 = 0;
    if (y1 > c->H) y1 = c->H;
    for (int y = y0; y < y1; y++) c->dirty[y] = 1;
}

static void px_add(canvas_t *c, int x, int y, rgb_t v)
{
    if (x < 0 || y < 0 || x >= c->W || y >= c->H) return;
    float *p = c->frame + ((size_t)y * c->W + x) * 3;
    p[0] += v.r; p[1] += v.g; p[2] += v.b;
    mark(c, y, y + 1);
}

static void rect_add(canvas_t *c, int x0, int y0, int x1, int y1, rgb_t v)
{
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > c->W) x1 = c->W;
    if (y1 > c->H) y1 = c->H;
    for (int y = y0; y < y1; y++) {
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3) { p[0] += v.r; p[1] += v.g; p[2] += v.b; }
    }
    mark(c, y0, y1);
}

/* draw_text(): adds into the frame, clipping at the edges. */
static void draw_text(canvas_t *c, const char *text, int x, int y, rgb_t col,
                      int scale, int spacing)
{
    int step = (CHAR_W + spacing) * scale;
    for (int i = 0; text[i]; i++) {
        const uint8_t *g = glyph_for(text[i]);
        if (!g) continue;
        int gx = x + i * step;
        if (gx >= c->W || gx + CHAR_W * scale <= 0) continue;
        if (y >= c->H || y + CHAR_H * scale <= 0) continue;
        for (int ry = 0; ry < CHAR_H; ry++) {
            if (!g[ry]) continue;
            for (int rx = 0; rx < CHAR_W; rx++) {
                if (!(g[ry] & (1 << (CHAR_W - 1 - rx)))) continue;
                rect_add(c, gx + rx * scale, y + ry * scale,
                         gx + (rx + 1) * scale, y + (ry + 1) * scale, col);
            }
        }
    }
}

/* ----------------------------------------------------------- the HUD chrome */

static void build_chrome(canvas_t *c, int wx0, int wx1, int wy0, int wy1)
{
    const rgb_t dim = { 28.f, 66.f, 88.f };
    size_t n = (size_t)c->W * c->H * 3;
    memset(c->chrome, 0, n * sizeof(float));

    float *save = c->frame;
    c->frame = c->chrome;                       /* draw furniture into chrome */

    int cy = (wy0 + wy1) / 2;
    rgb_t d7 = { dim.r * 0.7f, dim.g * 0.7f, dim.b * 0.7f };
    rect_add(c, wx0, cy - 1, wx1, cy + 1, d7);              /* baseline */

    rgb_t d5 = { dim.r * 0.5f, dim.g * 0.5f, dim.b * 0.5f };
    int caps[2] = { wx0, wx1 - 2 };
    for (int k = 0; k < 2; k++)
        rect_add(c, caps[k], wy0, caps[k] + 2, wy1, d5);    /* end caps */

    const int blen = 34, t = 2;
    int corner[4][4] = {                                     /* cx, cy, sx, sy */
        { wx0,     wy0,     1,  1 },
        { wx1 - t, wy0,    -1,  1 },
        { wx0,     wy1 - t, 1, -1 },
        { wx1 - t, wy1 - t,-1, -1 },
    };
    for (int k = 0; k < 4; k++) {
        int cx = corner[k][0], cyy = corner[k][1], sx = corner[k][2], sy = corner[k][3];
        int xs0 = sx > 0 ? cx  : cx - blen + t,  xs1 = sx > 0 ? cx + blen  : cx + t;
        int ys0 = sy > 0 ? cyy : cyy - blen + t, ys1 = sy > 0 ? cyy + blen : cyy + t;
        rect_add(c, xs0, cyy, xs1, cyy + t, dim);
        rect_add(c, cx, ys0, cx + t, ys1, dim);
    }
    c->frame = save;
}

/* ------------------------------------------------------------ metrics panel */

typedef struct { char text[MAX_TEXT]; rgb_t col; } line_t;

static void label_of(const char *name, char *out, size_t cap)
{
    size_t i = 0;
    for (; name[i] && i < 12 && i + 1 < cap; i++) {
        char ch = name[i];
        if (ch == '_') ch = '-';
        if (ch >= 'a' && ch <= 'z') ch = (char)(ch - 'a' + 'A');
        out[i] = ch;
    }
    out[i] = 0;
}

/* One panel line for one reading — mirrors metrics_hud._format(). */
static void format_reading(const char *name, const char *robj, line_t *ln)
{
    char label[16];
    label_of(name, label, sizeof label);
    char kind[32] = "";
    js_str(js_find(robj, "type"), kind, sizeof kind);

    if (strcmp(kind, "distance") == 0) {
        const char *v = js_find(robj, "cm");
        double cm;
        if (!v || strncmp(v, "null", 4) == 0 || !js_num(v, &cm)) {
            snprintf(ln->text, sizeof ln->text, "%s --", label);
            ln->col = ALERT_RGB; return;
        }
        if (cm >= M_NO_ECHO_CM) {
            snprintf(ln->text, sizeof ln->text, "%s ---", label);
            ln->col = TITLE_RGB; return;
        }
        /* Python round(): banker's rounding at .5 — nearbyint with the default
         * rounding mode does the same, and matters for a reading like 12.5. */
        snprintf(ln->text, sizeof ln->text, "%s %.0fCM", label, nearbyint(cm));
        ln->col = VALUE_RGB; return;
    }
    if (strcmp(kind, "motion") == 0) {
        if (js_bool(js_find(robj, "warming"))) {
            snprintf(ln->text, sizeof ln->text, "%s WARMUP", label);
            ln->col = TITLE_RGB; return;
        }
        bool active = js_bool(js_find(robj, "active"));
        snprintf(ln->text, sizeof ln->text, "%s %s", label, active ? "ACTIVE" : "IDLE");
        ln->col = active ? ALERT_RGB : VALUE_RGB; return;
    }
    snprintf(ln->text, sizeof ln->text, "%s ?", label);
    ln->col = TITLE_RGB;
}

/* Collect the panel's lines. Returns the count (0 = draw nothing). */
static int metrics_lines(feed_t *mf, double now, line_t *out, int cap)
{
    if (!mf->have) return 0;
    const char *doc = mf->buf;
    if (!js_bool(js_find(doc, "enabled"))) return 0;

    const char *readings = js_find(doc, "readings");
    double t = 0.0;
    js_num(js_find(doc, "t"), &t);
    double age = now - t;

    bool empty = true;
    if (readings && *readings == '{') {
        const char *p = js_skip_ws(readings + 1);
        if (*p == '"') empty = false;
    }
    if (empty || age > M_STALE_AFTER) {
        snprintf(out[0].text, sizeof out[0].text, "SENSORS NO DATA");
        out[0].col = ALERT_RGB;
        return 1;
    }

    /* Names, then sorted() to match Python's iteration order. */
    char names[MAX_LINES][MAX_TEXT];
    const char *objs[MAX_LINES];
    int n = 0;
    const char *p = js_skip_ws(readings + 1);
    while (*p == '"' && n < MAX_LINES - 1) {
        const char *ks = p + 1;
        const char *ke = js_skip_string(p);
        size_t klen = (size_t)(ke - ks) - 1;
        if (klen >= MAX_TEXT) klen = MAX_TEXT - 1;
        memcpy(names[n], ks, klen); names[n][klen] = 0;
        p = js_skip_ws(ke);
        if (*p != ':') break;
        const char *val = js_skip_ws(p + 1);
        objs[n] = (*val == '{') ? val : NULL;
        if (objs[n]) n++;                        /* isinstance(r, dict) */
        p = js_skip_value(val);
        p = js_skip_ws(p);
        if (*p == ',') p = js_skip_ws(p + 1); else break;
    }
    for (int i = 1; i < n; i++)                  /* insertion sort by name */
        for (int j = i; j > 0 && strcmp(names[j - 1], names[j]) > 0; j--) {
            char tn[MAX_TEXT]; const char *to;
            memcpy(tn, names[j - 1], MAX_TEXT); memcpy(names[j - 1], names[j], MAX_TEXT);
            memcpy(names[j], tn, MAX_TEXT);
            to = objs[j - 1]; objs[j - 1] = objs[j]; objs[j] = to;
        }

    int count = 0;
    snprintf(out[count].text, sizeof out[count].text, "SENSORS");
    out[count].col = TITLE_RGB;
    count++;
    for (int i = 0; i < n && count < cap; i++, count++)
        format_reading(names[i], objs[i], &out[count]);
    return count;
}

/* Paint the panel bottom-left. Mirrors MetricsHud.draw(). */
static void metrics_draw(canvas_t *c, feed_t *mf, double now)
{
    line_t lines[MAX_LINES];
    int n = metrics_lines(mf, now, lines, MAX_LINES);
    if (n <= 0) return;

    int w = 0;
    for (int i = 0; i < n; i++) {
        int tw = text_width(lines[i].text, M_SCALE, 1);
        if (tw > w) w = tw;
    }
    w += M_PAD * 2;
    int h = n * M_LINE_H - (M_LINE_H - CHAR_H * M_SCALE) + M_PAD * 2;
    int x0 = M_MARGIN, y1 = c->H - M_MARGIN;
    int y0 = y1 - h; if (y0 < 0) y0 = 0;
    int x1 = x0 + w; if (x1 > c->W) x1 = c->W;

    /* voice_hud.py clips the whole frame before calling this, and the dim below
     * therefore acts on already-clipped values. Everywhere else the clip can be
     * folded into the blit, but here it has to happen first or a pixel that
     * accumulated past 255 would dim from the wrong number. */
    for (int y = y0; y < y1; y++) {
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3)
            for (int k = 0; k < 3; k++) {
                if (p[k] < 0.f) p[k] = 0.f;
                else if (p[k] > 255.f) p[k] = 255.f;
            }
    }
    for (int y = y0; y < y1; y++) {              /* knock the animation back */
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3) { p[0] *= M_DIM; p[1] *= M_DIM; p[2] *= M_DIM; }
    }
    mark(c, y0, y1);
    for (int i = 0; i < n; i++)
        draw_text(c, lines[i].text, x0 + M_PAD, y0 + M_PAD + i * M_LINE_H,
                  lines[i].col, M_SCALE, 1);
    for (int y = y0; y < y1; y++) {              /* clip only what we touched */
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3) {
            for (int k = 0; k < 3; k++) {
                if (p[k] < 0.f) p[k] = 0.f;
                else if (p[k] > 255.f) p[k] = 255.f;
            }
        }
    }
}

/* --------------------------------------------------------------- the cog
 * The twin of cog_hud.py. Same bitmap, same constants, same clip -> dim -> add
 * -> clip order; change one file and you must change the other, which is what
 * `make verify` is there to catch.                                          */

static const char *COG_ROWS[COG_N] = {
    "000000000000000000000000",
    "000000000011110000000000",
    "000000000111111000000000",
    "000000110111111011000000",
    "000011110111111011110000",
    "000011111111111111110000",
    "000111111111111111111000",
    "000111111111111111111000",
    "000001111111111111100000",
    "001111111100001111111100",
    "011111111000000111111110",
    "011111111000000111111110",
    "011111111000000111111110",
    "011111111000000111111110",
    "001111111100001111111100",
    "000001111111111111100000",
    "000111111111111111111000",
    "000111111111111111111000",
    "000011111111111111110000",
    "000011110111111011110000",
    "000000110111111011000000",
    "000000000111111000000000",
    "000000000011110000000000",
    "000000000000000000000000",
};

static void cog_clip(canvas_t *c, int x0, int y0, int x1, int y1)
{
    for (int y = y0; y < y1; y++) {
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3)
            for (int k = 0; k < 3; k++) {
                if (p[k] < 0.f) p[k] = 0.f;
                else if (p[k] > 255.f) p[k] = 255.f;
            }
    }
}

static void cog_draw(canvas_t *c)
{
    int x1 = c->W - COG_MARGIN, y1 = c->H - COG_MARGIN;
    int x0 = x1 - COG_BOX, y0 = y1 - COG_BOX;
    if (x0 < 0 || y0 < 0) return;             /* screen too small for the corner */

    /* Clip before the dim, for the reason metrics_draw() spells out: dimming a
     * value that ran past 255 shrinks the wrong number. */
    cog_clip(c, x0, y0, x1, y1);
    for (int y = y0; y < y1; y++) {
        float *p = c->frame + ((size_t)y * c->W + x0) * 3;
        for (int x = x0; x < x1; x++, p += 3) {
            p[0] *= COG_DIM; p[1] *= COG_DIM; p[2] *= COG_DIM;
        }
    }
    mark(c, y0, y1);

    int ix = x0 + COG_PAD, iy = y0 + COG_PAD;
    for (int r = 0; r < COG_N; r++) {
        const char *row = COG_ROWS[r];
        for (int k = 0; k < COG_N; k++) {
            if (row[k] != '1') continue;
            for (int dy = 0; dy < COG_SCALE; dy++) {
                float *p = c->frame
                         + ((size_t)(iy + r * COG_SCALE + dy) * c->W
                            + (ix + k * COG_SCALE)) * 3;
                for (int dx = 0; dx < COG_SCALE; dx++, p += 3) {
                    p[0] += TITLE_RGB.r; p[1] += TITLE_RGB.g; p[2] += TITLE_RGB.b;
                }
            }
        }
    }
    cog_clip(c, x0, y0, x1, y1);
}

/* ------------------------------------------------------------- the one pass
 * clip -> quantise -> pack -> store. Replaces np.clip + astype(uint8) + three
 * uint16 casts + the bit-packing + tobytes() + the mmap slice assignment.    */

static void blit(canvas_t *c, fb_t *fb)
{
    for (int y = 0; y < c->H; y++) {
        /* A row that changed neither this frame nor last still holds the right
         * pixels on the panel; packing it again would be pure bandwidth. */
        if (!c->dirty[y] && !c->prev_dirty[y]) continue;
        const float *p = c->frame + (size_t)y * c->W * 3;
        uint8_t *rowb = fb->mm + (size_t)y * fb->stride;
        uint16_t *row16 = (uint16_t *)rowb;
        uint32_t *row32 = (uint32_t *)rowb;
        const bool deep = fb->bpp == 32;
        for (int x = 0; x < c->W; x++, p += 3) {
            float fr = p[0], fg = p[1], fb_ = p[2];
            if (fr < 0.f) fr = 0.f; else if (fr > 255.f) fr = 255.f;
            if (fg < 0.f) fg = 0.f; else if (fg > 255.f) fg = 255.f;
            if (fb_ < 0.f) fb_ = 0.f; else if (fb_ > 255.f) fb_ = 255.f;
            unsigned r = (unsigned)fr, g = (unsigned)fg, b = (unsigned)fb_;
            /* 16bpp is what the panel comes up in; 32bpp is what the fbdev
             * emulation gives once vc4-kms-v3d is loaded. Same quantisation
             * either way — only the store differs, so --dump and the
             * equivalence harness are unaffected by which one is live. */
            if (deep)
                row32[x] = 0xFF000000u | (r << 16) | (g << 8) | b;
            else
                row16[x] = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
        }
    }
}

/* Same quantisation, but into an RGB888 buffer — used only by --dump so the
 * result can be diffed against the Python version's frame. */
static void quantise_rgb888(canvas_t *c, uint8_t *out)
{
    const float *p = c->frame;
    size_t n = (size_t)c->W * c->H;
    for (size_t i = 0; i < n; i++, p += 3) {
        for (int k = 0; k < 3; k++) {
            float v = p[k];
            if (v < 0.f) v = 0.f; else if (v > 255.f) v = 255.f;
            out[i * 3 + k] = (uint8_t)v;
        }
    }
}

/* ------------------------------------------------------------------- clock */

static volatile sig_atomic_t running = 1;
static void on_signal(int s) { (void)s; running = 0; }

static double mono_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

/* --------------------------------------------------------------------- main */

int main(int argc, char **argv)
{
    double seconds = 0.0, fps = 30.0;
    const char *voice_path = VOICE_PATH_DEF, *metrics_path = METRICS_PATH_DEF;
    const char *dev = "/dev/fb0";
    const char *dump_path = NULL;
    bool sim = false;                   /* frame-indexed clock, for the harness */
    long sim_start = 0, max_frames = 0;
    const char *sim_docs = NULL;   /* per-frame voice docs, harness only */
    int sim_w = 800, sim_h = 480;
    const char *theme_name = NULL;   /* NULL = whatever state.json says */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atof(argv[++i]);
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) fps = atof(argv[++i]);
        else if (!strcmp(argv[i], "--voice-path") && i + 1 < argc) voice_path = argv[++i];
        else if (!strcmp(argv[i], "--metrics-path") && i + 1 < argc) metrics_path = argv[++i];
        else if (!strcmp(argv[i], "--fb") && i + 1 < argc) dev = argv[++i];
        else if (!strcmp(argv[i], "--dump") && i + 1 < argc) dump_path = argv[++i];
        else if (!strcmp(argv[i], "--sim")) sim = true;
        else if (!strcmp(argv[i], "--sim-start-frame") && i + 1 < argc) sim_start = atol(argv[++i]);
        else if (!strcmp(argv[i], "--sim-docs") && i + 1 < argc) sim_docs = argv[++i];
        else if (!strcmp(argv[i], "--frames") && i + 1 < argc) max_frames = atol(argv[++i]);
        else if (!strcmp(argv[i], "--theme") && i + 1 < argc) theme_name = argv[++i];
        else { fprintf(stderr, "unknown option: %s\n", argv[i]); return 2; }
    }
    if (fps <= 0.0) fps = 30.0;

    /* Colours before anything is drawn. --theme is for the equivalence
     * harness, which has to render all three without disturbing the
     * panel's actual preference. */
    char themebuf[32];
    if (!theme_name) {
        theme_from_state("state.json", themebuf, sizeof themebuf);
        theme_name = themebuf[0] ? themebuf : "soft";
    }
    apply_theme(theme_name);

    fb_t fb = { .fd = -1, .mm = NULL };
    canvas_t c = { 0 };
    if (dump_path) {                    /* headless: no panel, no mmap */
        c.W = sim_w; c.H = sim_h;
    } else {
        if (fb_open(&fb, dev) < 0) return 1;
        hide_cursor();
        c.W = fb.w; c.H = fb.h;
    }

    size_t nf = (size_t)c.W * c.H * 3;
    c.chrome = malloc(nf * sizeof(float));
    c.frame  = malloc(nf * sizeof(float));
    c.dirty      = calloc((size_t)c.H, 1);
    c.prev_dirty = malloc((size_t)c.H);
    float *levels = malloc(sizeof(float) * MAX_LEVELS);
    uint8_t *dumpbuf = dump_path ? malloc((size_t)c.W * c.H * 3) : NULL;
    if (!c.chrome || !c.frame || !c.dirty || !c.prev_dirty || !levels
        || (dump_path && !dumpbuf)) {
        fprintf(stderr, "out of memory\n"); return 1;
    }
    /* Every row counts as damaged before the first frame, so frame 0 restores
     * the whole canvas from chrome and paints the whole panel. */
    memset(c.prev_dirty, 1, (size_t)c.H);
    c.track = false;                        /* chrome is furniture, not damage */

    const int W = c.W, H = c.H;
    const int wx0 = (int)(W * 0.06), wx1 = (int)(W * 0.94);
    const int wy0 = (int)(H * 0.30), wy1 = (int)(H * 0.86);
    const int wcy = (wy0 + wy1) / 2, whh = (wy1 - wy0) / 2;
    const int cols = wx1 - wx0;

    build_chrome(&c, wx0, wx1, wy0, wy1);
    c.track = true;

    /* The state dot's falloff, built once. */
    const int r = 9;
    float *DOT = malloc(sizeof(float) * (size_t)(2 * r) * (2 * r));
    for (int y = 0; y < 2 * r; y++)
        for (int x = 0; x < 2 * r; x++) {
            double a = ((double)x - r) / (r * 0.55), b = ((double)y - r) / (r * 0.55);
            DOT[y * 2 * r + x] = (float)exp(-(a * a + b * b));
        }

    const rgb_t METER_BG = { 22.f, 52.f, 70.f };
    const int ty = (int)(H * 0.12);
    const int ddy0 = ty + (CHAR_H * 4) / 2 - r;
    const int mw = (int)(W * 0.20), mh = 8;
    const int mx0 = wx1 - mw, my0 = (int)(H * 0.145);

    feed_t vf, mf;
    feed_init(&vf, voice_path);
    feed_init(&mf, metrics_path);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    FILE *dump = NULL;
    if (dump_path) {
        dump = fopen(dump_path, "wb");
        if (!dump) { perror(dump_path); return 1; }
    }

    const double start = sim ? 0.0 : mono_now();
    const double period = 1.0 / fps;
    long n = 0;
    int nlev = 0;
    double play_at = 0.0, frame_dt = 0.0;
    bool have_clip = false;
    int state = ST_IDLE;

    while (running) {
        const double now = sim ? start + (sim_start + n) * period : mono_now();
        const double t = now - start;
        if (seconds > 0.0 && t >= seconds) break;
        if (max_frames > 0 && n >= max_frames) break;

        if (sim_docs) {          /* harness: a fresh doc per frame, so one process
                                  * spans the state changes damage tracking must
                                  * survive */
            snprintf(vf.path, sizeof vf.path, "%s/%ld.json", sim_docs, sim_start + n);
            vf.mtime.tv_sec = -1; vf.mtime.tv_nsec = -1;
        }
        if (feed_poll(&vf)) {                       /* re-parse only on change */
            char s[32] = "";
            js_str(js_find(vf.buf, "state"), s, sizeof s);
            state = ST_IDLE;
            for (int i = 0; i < 4; i++)
                if (!strcmp(s, STATE_NAME[i])) { state = i; break; }
            const char *lv = js_find(vf.buf, "levels");
            nlev = lv ? js_num_array(lv, levels, MAX_LEVELS) : 0;
            if (nlev > MAX_LEVELS) nlev = MAX_LEVELS;
            have_clip = nlev > 0
                     && js_num(js_find(vf.buf, "play_at"), &play_at)
                     && js_num(js_find(vf.buf, "frame_dt"), &frame_dt)
                     && frame_dt != 0.0;
        }

        rgb_t colour;
        switch (state) {
        case ST_LISTENING: colour = GREEN; break;
        case ST_THINKING:  colour = AMBER; break;
        case ST_SPEAKING:  colour = CYAN;  break;
        default:           colour = (rgb_t){ CYAN.r * 0.75f, CYAN.g * 0.75f,
                                             CYAN.b * 0.75f }; break;
        }

        /* We are not the only writer to this framebuffer: fbcon puts kernel
         * messages straight into it, and damage tracking cannot see that happen.
         * Left alone, a stray printk would sit on the panel until the next time
         * the animation happened to redraw those rows — which for the static
         * parts of the HUD is never. So force a full repaint about once a
         * second. One extra blit per 30 frames costs far less than the bug.
         * (voice_hud.py never had this problem: it rewrote all 480 rows every
         * frame, which papered over the console by brute force.) */
        if (n % FULL_REPAINT_FRAMES == 0) memset(c.prev_dirty, 1, (size_t)H);

        /* Restore only what we drew on last frame; every other row still holds
         * chrome untouched. Replaces a 4.6 MB copy with ~25% of one. */
        for (int y = 0; y < H; y++) {
            if (!c.prev_dirty[y]) continue;
            size_t off = (size_t)y * W * 3;
            memcpy(c.frame + off, c.chrome + off, (size_t)W * 3 * sizeof(float));
        }
        memset(c.dirty, 0, (size_t)H);

        bool have_frac = false;
        double frac = 0.0;
        if (have_clip) {
            double dur = (double)nlev * frame_dt;
            if (dur > 0.0) { frac = (now - play_at) / dur; have_frac = true; }
        }

        if (have_clip && have_frac && frac >= -0.5 && frac <= 1.25) {
            /* --- the utterance, whole: waveform + playhead --- */
            const double head = frac * cols;
            long hc = (long)ceil(head);
            if (hc < 0) hc = 0;
            if (hc > cols) hc = cols;

            for (int x = 0; x < cols; x++) {
                int idx = (int)((double)x / cols * nlev);
                if (idx < 0) idx = 0;
                if (idx > nlev - 1) idx = nlev - 1;
                float amp = levels[idx] * (float)(whh * 0.95);
                if (!(amp > 1.5f)) amp = 1.5f;         /* np.maximum */
                rgb_t col = (x < hc) ? colour
                          : (rgb_t){ colour.r * 0.28f, colour.g * 0.28f,
                                     colour.b * 0.28f };
                /* |y - wcy| is a non-negative integer, so the lit rows are
                 * exactly those within floor(amp) of the centre — solve for the
                 * range instead of testing all 268 rows to light about five. */
                int k = (int)floorf(amp);
                int y0 = wcy - k, y1 = wcy + k + 1;
                if (y0 < wy0) y0 = wy0;
                if (y1 > wy1) y1 = wy1;
                rect_add(&c, wx0 + x, y0, wx0 + x + 1, y1, col);
            }
            if (head >= 0.0 && head < cols) {           /* the playhead itself */
                int hx = wx0 + (int)head;
                int px0 = hx - 1; if (px0 < wx0) px0 = wx0;
                rgb_t wh = { WHITE.r * 0.55f, WHITE.g * 0.55f, WHITE.b * 0.55f };
                rect_add(&c, px0, wy0, hx + 2, wy1, wh);
            }
        } else {
            /* --- no clip: a living baseline that says which state we're in --- */
            double amp;
            if (state == ST_LISTENING)     amp = 2.0 + 3.5 * (0.5 + 0.5 * sin(t * 3.0));
            else if (state == ST_THINKING) amp = 2.0;
            else                           amp = 1.5 + 1.0 * (0.5 + 0.5 * sin(t * 1.4));

            int br0 = -1, br1 = -1;
            for (int y = wy0; y < wy1; y++) {
                float dy = (float)y - (float)wcy;
                if (fabsf(dy) <= (float)amp) { if (br0 < 0) br0 = y; br1 = y + 1; }
            }
            if (br0 >= 0) {
                rgb_t col = { colour.r * 0.8f, colour.g * 0.8f, colour.b * 0.8f };
                rect_add(&c, wx0, br0, wx1, br1, col);

                if (state == ST_THINKING) {   /* a scanner sweeping the trace */
                    double sx = (0.5 + 0.5 * sin(t * 2.4)) * (cols - 1);
                    for (int x = 0; x < cols; x++) {
                        double u = ((double)x - sx) / 26.0;
                        float g = (float)exp(-(u * u));
                        rgb_t col2 = { g * AMBER.r * 1.6f, g * AMBER.g * 1.6f,
                                       g * AMBER.b * 1.6f };
                        for (int y = br0; y < br1; y++) px_add(&c, wx0 + x, y, col2);
                    }
                }
            }
        }

        /* --- state readout --- */
        const double pulse = 0.75 + 0.25 * (0.5 + 0.5 * sin(t * 3.2));
        rgb_t cp = { colour.r * (float)pulse, colour.g * (float)pulse,
                     colour.b * (float)pulse };
        char label[32];
        snprintf(label, sizeof label, "%s", STATE_NAME[state]);
        for (char *q = label; *q; q++)
            if (*q >= 'a' && *q <= 'z') *q = (char)(*q - 'a' + 'A');
        /* Big, centred, and the largest thing on the panel. This screen sits at
         * a child's eyeline and its job in a crowd is turn-taking: whether he
         * is listening to you or to somebody else has to be readable from the
         * back of a queue, which a 28-pixel word in the corner was not. The
         * label is scaled to fill the width it is given rather than fixed, so
         * SPEAKING and LISTENING look like the same control at different
         * moments instead of two differently-sized labels. */
        int scale = STATE_SCALE_MAX;
        while (scale > 4 && text_width(label, scale, 1) > W - 2 * STATE_MARGIN)
            scale--;
        const int tw = text_width(label, scale, 1);
        const int tx = (W - tw) / 2;
        draw_text(&c, label, tx, ty, cp, scale, 1);
        /* The dot keeps its meaning — a pulse that says the panel is live even
         * mid-word — but moves out of the way of the text it used to precede. */
        for (int y = 0; y < 2 * r; y++)
            for (int x = 0; x < 2 * r; x++) {
                float d = DOT[y * 2 * r + x];
                rgb_t dv = { d * colour.r * (float)pulse, d * colour.g * (float)pulse,
                             d * colour.b * (float)pulse };
                px_add(&c, tx - 4 * r + x, ddy0 + y, dv);
            }

        /* --- live level meter, bottom right --- */
        float lvl = 0.f;
        if (have_clip) {
            long i = (long)((now - play_at) / frame_dt);
            if (i >= 0 && i < nlev) lvl = levels[i];
        }
        rect_add(&c, mx0, my0, mx0 + mw, my0 + mh, METER_BG);
        float lf = lvl < 1.f ? lvl : 1.f;
        int fill = (int)(mw * lf);
        if (fill > 0) {
            rgb_t lc = { colour.r * 0.9f, colour.g * 0.9f, colour.b * 0.9f };
            rect_add(&c, mx0, my0, mx0 + fill, my0 + mh, lc);
        }

        /* No full-frame clip here: the blit clamps as it packs, and the one
         * place that must see clipped values first — the metrics panel's dim —
         * clips its own rect. */
        feed_poll(&mf);
        metrics_draw(&c, &mf, now);
        cog_draw(&c);

        if (dump) {
            quantise_rgb888(&c, dumpbuf);
            fwrite(dumpbuf, 1, (size_t)W * H * 3, dump);
        } else {
            blit(&c, &fb);
        }
        memcpy(c.prev_dirty, c.dirty, (size_t)H);

        n++;
        if (!sim) {
            double sleep_for = period - ((mono_now() - start) - n * period);
            if (sleep_for > 0.0) {
                struct timespec ts = { (time_t)sleep_for,
                                       (long)((sleep_for - (long)sleep_for) * 1e9) };
                nanosleep(&ts, NULL);
            }
        }
    }

    const double dur = sim ? n * period : mono_now() - start;
    if (dump) fclose(dump);
    if (!dump_path) { fb_clear(&fb); fb_close(&fb); }
    fprintf(stderr, "%ld frames in %.1fs = %.1f fps\n", n, dur,
            n / (dur > 1e-6 ? dur : 1e-6));
    free(c.chrome); free(c.frame); free(c.dirty); free(c.prev_dirty);
    free(levels); free(DOT); free(dumpbuf);
    return 0;
}
