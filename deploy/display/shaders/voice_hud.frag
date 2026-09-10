#version 440
// The voice HUD. A transliteration of voice_hud.Hud.render(), not a
// reinterpretation: the same trace window, the same bracket geometry, the same
// pulse rates and shade factors, on integer pixel coordinates — every rectangle
// in the reference is an integer slice, so floor(pixel) reproduces it exactly.
//
// Two things a fragment shader cannot draw for itself arrive as textures, both
// produced by voice_hud.py so they are not a second implementation:
//   word      the state word and its dot, as an intensity map the size of the
//             panel (Hud.word_layer) — multiplied by colour * pulse here, which
//             is what render() does per pixel;
//   envelope  the clip's amplitude, one texel per sample, 16 bits across R and
//             G (encode_levels) — the waveform is read from it per column.
//
// Checked against voice_hud.py by tools/verify_shaders.py.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

// The same block as every shader here, plus this one's own members at the end.
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec4 deep;          // theme.Ramp.deep
    vec4 accent;        // theme.Ramp.accent
    vec4 ok;            // listening
    vec4 warn;          // thinking
    vec2 res;
    float t;            // seconds since the animation started
    float level;        // live envelope level, 0..1 — the meter
    float voiceState;   // 0 idle, 1 listening, 2 thinking, 3 speaking
    float copper;
    vec4 win;           // trace window: x0, y0, x1, y1 (pixels, exclusive end)
    vec4 meter;         // level meter: x0, y0, width, height
    float head;         // playhead, in trace columns
    float haveClip;     // 1 while a clip is on screen
    float envLen;       // samples in the envelope texture
};
layout(binding = 1) uniform sampler2D envelope;
layout(binding = 2) uniform sampler2D word;

// theme.Ramp.at(): rim -> accent at 0.5 -> white at 1.
vec3 lvl(float u)
{
    return u <= 0.5 ? mix(deep.rgb, accent.rgb, u * 2.0)
                    : mix(accent.rgb, vec3(1.0), (u - 0.5) * 2.0);
}

// One corner bracket of build_chrome(): a horizontal and a vertical bar, each
// 34 long and 2 thick, growing from the corner in the direction of (sx, sy).
// Returns how many bars cover the pixel — the corner pixel gets both, as the
// two slice-adds in the reference give it.
float bracket(int px, int py, int cx, int cyy, int sx, int sy)
{
    int xs0 = sx > 0 ? cx : cx - 34 + 2;
    int xs1 = sx > 0 ? cx + 34 : cx + 2;
    int ys0 = sy > 0 ? cyy : cyy - 34 + 2;
    int ys1 = sy > 0 ? cyy + 34 : cyy + 2;
    float n = 0.0;
    if (py >= cyy && py < cyy + 2 && px >= xs0 && px < xs1) n += 1.0;
    if (px >= cx && px < cx + 2 && py >= ys0 && py < ys1) n += 1.0;
    return n;
}

void main()
{
    vec2 p = qt_TexCoord0 * res;
    int px = int(floor(p.x));
    int py = int(floor(p.y));

    int wx0 = int(win.x), wy0 = int(win.y), wx1 = int(win.z), wy1 = int(win.w);
    int cols = wx1 - wx0;
    int wcy = (wy0 + wy1) / 2;
    int whh = (wy1 - wy0) / 2;

    vec3 dim = vec3(28.0, 66.0, 88.0) / 255.0;
    vec3 cyan = lvl(0.45);                  // theme.HUD_LEVELS base
    vec3 white = lvl(0.82);                 // theme.HUD_LEVELS white
    vec3 amber = warn.rgb;
    bool listening = voiceState > 0.5 && voiceState < 1.5;
    bool thinking = voiceState > 1.5 && voiceState < 2.5;
    bool speaking = voiceState > 2.5;
    vec3 colour = speaking ? cyan : thinking ? amber : listening ? ok.rgb : cyan * 0.75;

    vec3 col = vec3(0.0);
    bool inX = px >= wx0 && px < wx1;
    bool inY = py >= wy0 && py < wy1;

    // ---- chrome: baseline, end caps, corner brackets ----
    if (inX && (py == wcy - 1 || py == wcy)) col += dim * 0.7;
    if (inY && (px == wx0 || px == wx0 + 1 || px == wx1 - 2 || px == wx1 - 1)) col += dim * 0.5;
    col += dim * (bracket(px, py, wx0, wy0, 1, 1) + bracket(px, py, wx1 - 2, wy0, -1, 1)
                  + bracket(px, py, wx0, wy1 - 2, 1, -1) + bracket(px, py, wx1 - 2, wy1 - 2, -1, -1));

    // ---- the trace window ----
    if (inX && inY) {
        int c = px - wx0;
        float dy = float(py - wcy);
        if (haveClip > 0.5) {
            // The utterance, whole: waveform + playhead.
            int n = int(envLen);
            int idx = clamp((c * n) / cols, 0, n - 1);
            vec4 e = texture(envelope, vec2((float(idx) + 0.5) / float(n), 0.5));
            float lv = (floor(e.r * 255.0 + 0.5) * 256.0 + floor(e.g * 255.0 + 0.5)) / 65535.0;
            float amp = max(lv * (float(whh) * 0.95), 1.5);
            if (abs(dy) <= amp) {
                // Behind the playhead, what he has said; ahead, dimmer, what
                // is coming. ceil, as the reference: a column counts as played
                // while head is anywhere past its left edge.
                float hcol = clamp(ceil(head), 0.0, float(cols));
                col += float(c) < hcol ? colour : colour * 0.28;
            }
            if (head >= 0.0 && head < float(cols)) {
                int hx = wx0 + int(floor(head));
                if (px >= max(hx - 1, wx0) && px <= hx + 1) col += white * 0.55;
            }
        } else {
            // No clip: a living baseline that says which state we're in.
            float amp = thinking ? 2.0
                      : listening ? 2.0 + 3.5 * (0.5 + 0.5 * sin(t * 3.0))
                                  : 1.5 + 1.0 * (0.5 + 0.5 * sin(t * 1.4));
            if (abs(dy) <= amp) {
                col += colour * 0.8;
                if (thinking) {
                    // A scanner sweeping the trace: he's working on it.
                    float sx = (0.5 + 0.5 * sin(t * 2.4)) * float(cols - 1);
                    float u = (float(c) - sx) / 26.0;
                    col += exp(-u * u) * amber * 1.6;
                }
            }
        }
    }

    // ---- state readout: the word and its dot, pulsing ----
    float pulse = 0.75 + 0.25 * (0.5 + 0.5 * sin(t * 3.2));
    col += texture(word, qt_TexCoord0).r * colour * pulse;

    // ---- live level meter ----
    int mx0 = int(meter.x), my0 = int(meter.y), mw = int(meter.z), mh = int(meter.w);
    if (py >= my0 && py < my0 + mh && px >= mx0 && px < mx0 + mw) {
        col += vec3(22.0, 52.0, 70.0) / 255.0;
        int fill = int(floor(float(mw) * min(level, 1.0)));
        if (px < mx0 + fill) col += colour * 0.9;
    }

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0) * qt_Opacity;
}
