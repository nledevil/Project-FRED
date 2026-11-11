import QtQuick

// One shader, one overlay. Everything that varies per pixel is on the GPU;
// everything that is text or an icon is drawn by the same numpy code the CPU
// animations use and handed over as a texture, so the cog and the sensor panel
// look exactly as they always have.
Item {
    id: root
    width: 800; height: 480

    // Bumped by the host whenever it has redrawn the overlay. Image caching is
    // by URL, so the URL has to change or Qt serves the first one forever.
    property int overlayGeneration: 0

    // The animation clock, interpolated by Qt rather than pushed from Python.
    // A QTimer emitting a signal 60 times a second so the host could set `t`
    // cost about a quarter of a core and held the panel to 40fps; this costs
    // nothing and runs at the refresh rate. Seconds, to match reactor.py.
    property real animT: 0
    NumberAnimation on animT {
        from: 0; to: 1000000; duration: 1000000000; loops: Animation.Infinite
    }
    // FrozenT >= 0 stops the clock so --grab produces the same frame every
    // time, which is what makes the comparison against numpy reproducible.
    readonly property real t: FrozenT >= 0 ? FrozenT : animT

    Rectangle { anchors.fill: parent; color: "black" }

    ShaderEffect {
        anchors.fill: parent
        fragmentShader: Sh          // absolute URL from the host

        // Names must match the uniform block in the .frag. The ramp is passed
        // as two colours and reconstructed there, so theme.py stays the one
        // place a theme is defined.
        property color deep: Deep
        property color accent: Accent
        property color ok: OkCol
        property color warn: WarnCol
        property vector2d res: Qt.vector2d(root.width, root.height)
        property real t: root.t
        property real level: St.level
        property real voiceState: St.voiceState
        property real copper: Copper
        property real talk: Talk
        property real gazeX: St.gazeX
        property real gazeY: St.gazeY
        property real openness: St.openness
        property real glow: St.glow
    }

    Image {
        anchors.fill: parent
        visible: !HideOverlay
        source: "image://overlay/o" + root.overlayGeneration
        cache: false
        smooth: false
        fillMode: Image.Pad
    }
}
