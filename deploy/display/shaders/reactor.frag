#version 440
// The arc reactor. A transliteration of reactor.build_geometry(), not a
// reinterpretation: the same nine layers, the same radii, the same pulse gains.
// It reads almost identically because the numpy version was already a per-pixel
// field computation — radial distance, Gaussian bands, angular sharpening.
//
// Checked against reactor.py by tools/verify_shaders.py.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

// The same block in every shader here, so the host can hand all of them the
// same set of properties and each takes what it needs.
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec4 deep;          // theme.Ramp.deep
    vec4 accent;        // theme.Ramp.accent
    vec4 ok;            // listening
    vec4 warn;          // thinking
    vec2 res;
    float t;            // seconds since the animation started
    float level;        // current mic level, 0..1
    float voiceState;   // 0 idle, 1 listening, 2 thinking, 3 speaking
    float copper;       // 0 themed coils, 1 the copper preset
};

float band(float d, float r, float w)
{
    float x = (d - r) / w;
    return exp(-x * x);
}

// theme.Ramp.at(): rim -> accent at 0.5 -> white at 1.
vec3 lvl(float u)
{
    return u <= 0.5 ? mix(deep.rgb, accent.rgb, u * 2.0)
                    : mix(accent.rgb, vec3(1.0), (u - 0.5) * 2.0);
}

void main()
{
    // The breathing pulse reactor.py computes per frame in Python. Done here
    // so the whole animation is one uniform and one draw.
    float pulse = 0.55 + 0.45 * (0.5 + 0.5 * sin(t * 2.2));
    float flicker = 1.0 + 0.04 * sin(t * 37.0);
    float glow = pulse * flicker;

    vec2 p = qt_TexCoord0 * res;
    vec2 d = p - res * 0.5;
    float dist = length(d);
    float ang = atan(d.y, d.x);
    float R = min(res.x, res.y) * 0.5 * 0.92;

    vec3 col = vec3(0.0);

    // background radial vignette (static)
    float vign = clamp(1.0 - dist / (R * 1.6), 0.0, 1.0);
    col += vign * vign * 0.12 * lvl(0.0);

    // outer steel rings
    col += band(dist, R * 0.95, R * 0.03) * lvl(0.34) * mix(1.0, glow, 0.15);
    col += band(dist, R * 0.78, R * 0.02) * lvl(0.41) * mix(1.0, glow, 0.15);

    // the ten wound posts, and the wire highlights across them
    float ring = band(dist, R * 0.62, R * 0.10);
    float seg  = pow(0.5 + 0.5 * cos(10.0 * ang), 6.0);
    vec3 coil  = mix(lvl(0.5), vec3(210.0, 150.0, 70.0) / 255.0, copper);
    col += ring * seg * coil * mix(1.0, glow, 0.25);
    float wire = pow(0.5 + 0.5 * cos(30.0 * ang), 8.0);
    col += ring * wire * 0.5 * (vec3(255.0, 240.0, 210.0) / 255.0) * mix(1.0, glow, 0.2);

    // inner ring, tri-emitter, core, and the white-hot centre
    col += band(dist, R * 0.40, R * 0.03) * lvl(0.51) * mix(1.0, glow, 0.3);
    float tri = pow(0.5 + 0.5 * cos(3.0 * ang), 3.0);
    col += band(dist, R * 0.26, R * 0.05) * tri * lvl(0.63) * mix(1.0, glow, 0.5);
    float core = dist / (R * 0.22);
    col += exp(-core * core) * lvl(0.72) * glow;
    float hot = dist / (R * 0.09);
    col += exp(-hot * hot) * vec3(1.0) * glow;

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0) * qt_Opacity;
}
