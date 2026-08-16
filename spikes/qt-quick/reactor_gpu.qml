import QtQuick

// The reactor, drawn by the GPU. The whole animation is one ShaderEffect and
// one animated float — the pulse — which is what reactor.py already computes;
// it just does the other 384,000 multiply-adds per frame on the CPU as well.
Rectangle {
    id: root
    width: 800; height: 480
    color: "black"

    property real glow: 1.0
    property color deepCol: "#000000"
    property color accentCol: "#78d2ff"
    property real copper: 0.0

    // Same breathing pulse as reactor.py: a slow sine with a faint fast
    // flicker over it. Driven off a plain time value so the shader stays pure.
    property real t: 0.0
    NumberAnimation on t {
        from: 0; to: 1000; duration: 1000000; loops: Animation.Infinite
    }
    onTChanged: {
        var pulse = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(t * 2.2))
        var flicker = 1.0 + 0.04 * Math.sin(t * 37.0)
        glow = pulse * flicker
    }

    ShaderEffect {
        anchors.fill: parent
        fragmentShader: "reactor.frag.qsb"
        // Names must match the uniform block in reactor.frag.
        property color deep: root.deepCol
        property color accent: root.accentCol
        property vector2d res: Qt.vector2d(root.width, root.height)
        property real glow: root.glow
        property real copper: root.copper
    }
}
