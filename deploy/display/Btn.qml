import QtQuick

// A button in the panel's style, defined once. A press animation and a label
// that fits are properties of "button" here rather than of each caller — which
// is what stopped labels overflowing their borders in the numpy version only
// after three separate reports.
Rectangle {
    id: btn
    property string label: ""
    property bool on: false
    property bool big: false
    property int fontPx: big ? Th.px["3"] : Th.px["2"]
    // menu_ui.Button upper-cases unless a caller asks otherwise, and almost
    // none do. The exception is anything showing text a person typed, where
    // MYPASS instead of MyPass is a lie about what was stored.
    property bool preserveCase: false
    signal tapped()

    implicitHeight: 40
    implicitWidth: Math.max(60, txt.implicitWidth + 24)
    radius: Th.radius
    color: on ? Th.panelOn : Th.panel
    border.color: Th.edge
    border.width: Th.style === "soft" ? 0 : (on ? 2 : 1)
    scale: ma.pressed ? 0.96 : 1.0
    Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutQuad } }
    Behavior on color { ColorAnimation { duration: 120 } }

    // The HUD theme's halo, drawn as a sibling *behind* the button rather than
    // bleeding out of it, so the layout's spacing is what keeps it off the
    // neighbours. Bleeding 12px outward is what lit up adjacent tabs before.
    Rectangle {
        z: -1
        visible: Th.style === "hud"
        anchors.centerIn: parent
        width: parent.width + 8; height: parent.height + 8
        radius: parent.radius + 4
        color: "transparent"
        border.color: Th.edge
        border.width: 2
        opacity: btn.on ? 0.35 : 0.12
        Behavior on opacity { NumberAnimation { duration: 150 } }
    }

    Text {
        id: txt
        anchors.fill: parent
        anchors.margins: 8
        text: btn.preserveCase ? btn.label : btn.label.toUpperCase()
        color: btn.on ? Th.ink : Th.dimInk
        font.pixelSize: btn.fontPx
        font.family: Th.font
        font.letterSpacing: Th.tracking
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        fontSizeMode: Text.HorizontalFit
        minimumPixelSize: 9
    }
    MouseArea { id: ma; anchors.fill: parent; onClicked: btn.tapped() }
}
