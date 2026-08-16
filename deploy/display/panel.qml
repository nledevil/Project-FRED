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

    // The menu is a scene beside the animation rather than a second process
    // taking the screen. Switching is a property, so there is no DRM-to-fbdev
    // handoff and no start-up cost to open the settings.
    Loader {
        anchors.fill: parent
        active: P.scene === "menu"
        visible: active
        z: 10
        sourceComponent: MenuScene {}
    }

    ShaderEffect {
        anchors.fill: parent
        // Rebuilt when the preset changes; empty until the first one loads.
        fragmentShader: P.shader
        visible: P.shader !== "" && P.scene !== "menu"

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

    // The cog is a control here, not just a picture: this app owns the screen
    // and its input now, so the tap that opens the settings does not have to go
    // out to the daemon and come back as a new process.
    MouseArea {
        visible: P.scene !== "menu"
        enabled: visible
        x: P.cogHotspot[0]; y: P.cogHotspot[1]
        width: P.cogHotspot[2] - P.cogHotspot[0]
        height: P.cogHotspot[3] - P.cogHotspot[1]
        z: 5
        onClicked: P.openMenu()
    }

    // The cog and the sensor readings, drawn by the same numpy code the
    // framebuffer renderers use and handed over as a texture. Image caching is
    // by URL, so the URL has to change or Qt serves the first one forever.
    Image {
        anchors.fill: parent
        visible: P.scene !== "menu"
        source: "image://overlay/o" + root.overlayGeneration
        cache: false
        smooth: false
        fillMode: Image.Pad
    }
}
