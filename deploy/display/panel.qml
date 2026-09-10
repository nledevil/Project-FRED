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
    //
    // Paused while the menu is up. The ShaderEffect below is hidden then, but
    // a running animation still dirties the scene graph at the refresh rate,
    // so the render thread drew the same menu sixty times a second and the
    // main thread ticked the clock for it: measured at 20-25% of a core with
    // nothing on screen changing. Paused rather than stopped so the clock
    // resumes where it left off instead of snapping the shader back to zero.
    property real animT: 0
    NumberAnimation on animT {
        from: 0; to: 1000000; duration: 1000000000; loops: Animation.Infinite
        paused: P.scene === "menu"
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
        // FrozenT >= 0 stops the clock so --grab produces the same frame every
        // time, which is what makes the comparison against numpy reproducible.
        property real t: FrozenT >= 0 ? FrozenT : root.animT
        property real level: P.level
        property real voiceState: P.voiceState
        property real copper: P.copper
        property real talk: P.talk
        property real gazeX: P.gazeX
        property real gazeY: P.gazeY
        property real openness: P.openness
        property real glow: P.glow
        // The voice HUD's own inputs — see shaders/voice_hud.frag. The other
        // shaders ignore what they don't declare.
        property vector4d win: P.win
        property vector4d meter: P.meter
        property real head: P.head
        property real haveClip: P.haveClip
        property real envLen: P.envLen
        property var envelope: envelopeImage
        property var word: wordImage
    }

    // The two textures the voice HUD reads: the clip's envelope, one texel a
    // sample, and the state word as an intensity map. Both come from
    // voice_hud.py through image providers, so the shader draws the same
    // glyphs and the same samples the reference renderer does. Never shown
    // directly — they exist to be sampled. Nearest filtering, because a texel
    // is a pixel (the word) or a sample (the envelope), never a blend.
    Image {
        id: envelopeImage
        visible: false
        cache: false
        smooth: false
        source: "image://env/e" + P.envGen
    }
    Image {
        id: wordImage
        visible: false
        cache: false
        smooth: false
        source: "image://word/" + P.voiceWord
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
        visible: P.scene !== "menu" && !HideOverlay
        source: "image://overlay/o" + root.overlayGeneration
        cache: false
        smooth: false
        fillMode: Image.Pad
    }
}
