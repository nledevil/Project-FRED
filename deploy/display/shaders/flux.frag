#version 440
// The flux capacitor. A transliteration of flux.build() plus the spark loop out
// of flux.main(): three arms in a Y, a dim resting tube, a bright bulb at each
// end, a ring at the hub, and a spark running end-to-hub down each arm in turn.
//
// flux.py measured at 100% of a core — saturated, so it was not reaching 30fps.
// The per-pixel part is three point-to-segment distances, which is what this is.
//
// Checked against flux.py by tools/verify_shaders.py.

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
    float level;
    float voiceState;
    float copper;
};

const float SPEED = 1.4;        // flux.py's --speed default, spark cycles/sec

vec3 lvl(float u)
{
    return u <= 0.5 ? mix(deep.rgb, accent.rgb, u * 2.0)
                    : mix(accent.rgb, vec3(1.0), (u - 0.5) * 2.0);
}

void main()
{
    vec2 p = qt_TexCoord0 * res;

    vec2 hub = vec2(res.x * 0.5, res.y * 0.52);
    float R = min(res.x, res.y) * 0.40;          // arm length
    float tube_r = min(res.x, res.y) * 0.055;    // tube half-width
    float bulb_r = tube_r * 1.7;

    vec3 dim_tube = lvl(0.10);                   // resting glass
    vec3 bulb_col = lvl(0.38);                   // end bulbs
    vec3 spark_col = lvl(0.78);
    vec3 col = vec3(0.0);
    float flash = 0.0;

    // Y-shape: two arms up-diagonal, one straight down (the fork points up).
    // Written out rather than indexed from an array: a vec2[3] initialiser here
    // compiled to SPIR-V happily and then failed to build a pipeline on this
    // GPU, which surfaces as a black screen with the overlay still on it.
    for (int i = 0; i < 3; i++) {
        vec2 dir = i == 0 ? vec2(-0.80, -0.60)
                 : i == 1 ? vec2(0.80, -0.60)
                          : vec2(0.0, 1.0);
        vec2 end = hub + dir * R;
        vec2 d = end - hub;
        float L2 = dot(d, d);
        vec2 rel = p - hub;
        float s = clamp(dot(rel, d) / L2, 0.0, 1.0);   // projection param, 0..1
        vec2 closest = hub + s * d;
        float trans = length(p - closest);             // transverse distance
        float g = exp(-pow(trans / (tube_r * 0.6), 2.0));
        float mask = trans < tube_r * 1.6 ? 1.0 : 0.0;

        col += g * mask * dim_tube;                    // dim resting glow

        float bd = length(p - end);                    // bright end bulb
        col += exp(-pow(bd / bulb_r, 2.0)) * bulb_col;

        // The spark, staggered a third of a cycle per arm, running end -> hub.
        float phase = fract(t * SPEED + float(i) / 3.0);
        float pos = 1.0 - phase;
        float spark = exp(-pow((s - pos) / 0.10, 2.0)) * g * mask;
        col += spark * spark_col;
        flash = max(flash, exp(-pow(pos / 0.12, 2.0)));
    }

    // Central hub: a ring, and a core that flashes as each spark arrives.
    float hd = length(p - hub);
    col += exp(-pow((hd - bulb_r * 1.3) / (tube_r * 0.4), 2.0)) * lvl(0.33);
    col += flash * exp(-pow(hd / (bulb_r * 1.5), 2.0)) * vec3(1.0);

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0) * qt_Opacity;
}
