import QtQuick

// The panel. One scene today — the animation — and a Loader around it so the
// menu can arrive beside it rather than as a second process fighting for the
// screen. Switching animation is a property change, not a respawn.
Item {
    id: root
    width: 800; height: 480

    property int overlayGeneration: 0

    // Interpolated by Qt rather than pushed from Python: driving this from a
    // timer cost about a quarter of a core and held the panel to 40fps.
    property real animT: 0
    NumberAnimation on animT {
        from: 0; to: 1000000; duration: 1000000000; loops: Animation.Infinite
    }

    Rectangle { anchors.fill: parent; color: "black" }

    ShaderEffect {
        anchors.fill: parent
        // Rebuilt when the preset changes; empty until the first one loads.
        fragmentShader: P.shader
        visible: P.shader !== ""

        property color deep: Deep
        property color accent: Accent
        property color ok: OkCol
        property color warn: WarnCol
        property vector2d res: Qt.vector2d(root.width, root.height)
        property real t: root.animT
        property real level: P.level
        property real voiceState: P.voiceState
        property real copper: P.copper
        property real talk: P.talk
        property real gazeX: P.gazeX
        property real gazeY: P.gazeY
        property real openness: P.openness
        property real glow: P.glow
    }

    // The cog and the sensor readings, drawn by the same numpy code the
    // framebuffer renderers use and handed over as a texture. Image caching is
    // by URL, so the URL has to change or Qt serves the first one forever.
    Image {
        anchors.fill: parent
        source: "image://overlay/o" + root.overlayGeneration
        cache: false
        smooth: false
        fillMode: Image.Pad
    }
}
