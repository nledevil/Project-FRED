#version 440
// The arc reactor, as a fragment shader.
//
// A transliteration of reactor.build_geometry(), not a reinterpretation: the
// same nine layers, the same radii, the same pulse gains. It reads almost
// identically because the numpy version was already a per-pixel field
// computation — radial distance, Gaussian bands, angular sharpening — which is
// what a fragment shader is. That is the argument for moving it: the CPU is
// currently evaluating, 240,000 times a frame, something the GPU is built to do.
//
// Colours come from the theme's accent and rim, so the ramp lives here exactly
// once, the same way theme.Ramp.at() defines it in Python.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec4 deep;        // theme.Ramp.deep,   0..1
    vec4 accent;      // theme.Ramp.accent, 0..1
    vec2 res;         // panel size in pixels
    float glow;       // the breathing pulse; 1.0 == fully lit
    float copper;     // 0 = themed coils, 1 = the copper preset
};

// band(): a smooth glowing ring centred at radius r. numpy's
//   np.exp(-(((dist - r) / width) ** 2))
float band(float d, float r, float w)
{
    float x = (d - r) / w;
    return exp(-x * x);
}

// theme.Ramp.at(): rim -> accent at 0.5 -> white at 1.
vec3 lvl(float t)
{
    return t <= 0.5 ? mix(deep.rgb, accent.rgb, t * 2.0)
                    : mix(accent.rgb, vec3(1.0), (t - 0.5) * 2.0);
}

// Each layer is  intensity * colour * ((1 - gain) + gain * glow), and
// (1 - gain) + gain * glow is just mix(1, glow, gain).
void main()
{
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
