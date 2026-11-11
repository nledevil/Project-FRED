#version 440
// FRED's face. A transliteration of face.py: the HUD ring and its ticks, the
// broken inner arcs, two almond eyes with pupils that follow a gaze, and a
// mouth drawn as a waveform that opens with his actual speech envelope.
//
// face.py measured at 100% of a core — saturated, so it was not reaching 30fps,
// which on the one animation that lip-syncs is the one place it shows.
//
// Unlike the reactor and the flux capacitor, this is not a pure function of
// time: the gaze drifts to new targets and the blink timer accumulates. That
// state is half a dozen floats and stays in Python, where it costs nothing;
// only the per-pixel work is here. See gpu_anim.State.
//
// Checked against face.py by tools/verify_shaders.py.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec4 deep;
    vec4 accent;
    vec4 ok;
    vec4 warn;
    vec2 res;
    float t;
    float level;        // 0..1 speech envelope, straight off the mic
    float voiceState;   // 0 idle, 1 listening, 2 thinking, 3 speaking
    float copper;
    float gazeX;        // -1..1, where he is looking
    float gazeY;
    float openness;     // 1 open, 0 shut — the blink
    float glow;         // ring brightness; breathes with the voice
    float talk;         // 1 = the demo mouth, ignoring the microphone
};

// The face's own colour follows the theme; listening and thinking do not,
// because green and amber are the message. Mirrors face.STATE_STYLE.
vec3 tint()
{
    if (voiceState > 2.5) return mix(deep.rgb, accent.rgb, 0.94);   // speaking
    if (voiceState > 1.5) return warn.rgb;                          // thinking
    if (voiceState > 0.5) return ok.rgb;                            // listening
    return mix(deep.rgb, accent.rgb, 0.94);                         // idle
}

void main()
{
    vec2 p = qt_TexCoord0 * res;
    float W = res.x, H = res.y;
    vec2 c = res * 0.5;
    vec2 d = p - c;
    float dist = length(d);
    float ang = atan(d.y, d.x);
    float R = min(W, H) * 0.5;

    vec3 col = vec3(0.0);
    vec3 CYAN = tint();
    vec3 WHITE = mix(CYAN, vec3(1.0), 0.75);

    // --- outer HUD ring with tick marks ---
    float ring = exp(-pow((dist - R * 0.95) / 2.0, 2.0));
    float ticks = pow(0.5 + 0.5 * cos(ang * 60.0), 8.0);
    col += ring * (vec3(30.0, 70.0, 95.0) / 255.0);
    col += ring * (0.4 + ticks) * CYAN * 0.5 * glow;

    // --- inner arc segments (broken ring) ---
    float arc = exp(-pow((dist - R * 0.82) / 1.5, 2.0));
    float gaps = pow(0.5 + 0.5 * cos(ang * 8.0), 2.0);
    col += arc * gaps * (vec3(40.0, 120.0, 160.0) / 255.0) * glow;

    // --- the eyes ---
    float eye_dx = W * 0.17;
    float eye_y  = H * 0.40;
    float eye_rx = W * 0.075;
    float eye_ry = H * 0.14;
    float ry = eye_ry * max(openness, 0.04);
    float gx = gazeX * eye_rx * 0.5;
    float gy = gazeY * eye_ry * 0.4;

    for (int i = 0; i < 2; i++) {
        vec2 eye = vec2(W * 0.5 + (i == 0 ? -eye_dx : eye_dx), eye_y);
        vec2 q = p - eye;
        // Drawn only within the box face.py's Region covers, or the almond's
        // tail would run on across the panel where numpy simply stopped.
        if (abs(q.x) >= eye_rx * 1.8 || abs(q.y) >= eye_ry * 1.8) continue;
        float e = exp(-(pow(q.x / eye_rx, 2.0) + pow(q.y / ry, 2.0)));
        float pupil = exp(-(pow((q.x - gx) / (eye_rx * 0.35), 2.0)
                          + pow((q.y - gy) / (ry * 0.45), 2.0)));
        float inten = clamp(e * 1.1 - pupil * 0.9, 0.0, 1.0);
        col += inten * CYAN * glow;
        col += pupil * e * WHITE * 0.5;
    }

    // --- the mouth: his real speech envelope, or a fake one with --talk ---
    vec2 m = vec2(W * 0.5, H * 0.74);
    vec2 mq = p - m;
    if (abs(mq.x) < W * 0.22 && abs(mq.y) < H * 0.12) {
        float mx = mq.x / (W * 0.20);           // normalised across the mouth
        float idle_amp = 0.18 + 0.06 * sin(t * 2.0);
        float amp = talk > 0.5
            ? (0.4 + 0.6 * abs(sin(t * 6.0))) * (0.6 + 0.4 * sin(t * 11.0))
            : idle_amp + level * 0.95;
        float wave = amp * (sin(mx * 7.0 + t * 9.0) * 0.6
                          + sin(mx * 13.0 - t * 5.0) * 0.4);
        float wy = wave * (H * 0.09);
        float line = exp(-pow((mq.y - wy) / 4.0, 2.0));
        col += line * CYAN * (0.7 + 0.3 * glow);
    }

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0) * qt_Opacity;
}
