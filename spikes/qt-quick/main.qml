// Spike: does the chest panel's menu port to Qt Quick, and does the class of
// layout bug we keep shipping become structurally impossible?
//
// Deliberately mirrors the real chrome — title bar, seven tabs, a page of live
// rows, and the cart e-stop with its arm/confirm — because a pretty status page
// proves nothing. Every colour comes from `Th`, which is theme.py handed in as
// a context property: no second copy of the palette anywhere in this file.
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    width: 800; height: 480
    color: Th.bg

    property int current: 0
    readonly property var tabs: ["STATUS", "VOICE", "SERVOS", "CART",
                                 "DISPLAY", "WIFI", "INFO"]

    // The whole screen is one column: header, tab strip, page. Nothing here is
    // positioned by pixel arithmetic, so the tab row cannot share an edge with
    // the header and the page cannot ride up under the tabs — the three faults
    // reported this week are unrepresentable rather than fixed.
    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // ---- title bar ---------------------------------------------------
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            color: Th.panel
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 24; anchors.rightMargin: 8
                anchors.topMargin: 8; anchors.bottomMargin: 8
                spacing: 8
                Text {
                    text: "FRED SETTINGS"; color: Th.ink
                    font.pixelSize: 26; font.letterSpacing: Th.tracking
                    font.family: Th.font
                    Layout.fillWidth: true
                }
                Btn { label: "POWER"; implicitWidth: 96 }
                Btn { label: "X"; implicitWidth: 92; big: true }
            }
        }

        // ---- tabs --------------------------------------------------------
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 24; Layout.rightMargin: 24
            spacing: 8
            Repeater {
                model: root.tabs
                Btn {
                    objectName: "tab"          // so a test can find it
                    label: modelData
                    on: index === root.current
                    Layout.fillWidth: true          // tabs divide the strip
                    implicitHeight: 30
                    onTapped: root.current = index
                }
            }
        }

        // ---- the page ----------------------------------------------------
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.margins: 24
            Layout.topMargin: 0
            spacing: 20

            // live rows, as page_status draws them
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                spacing: 10
                Repeater {
                    model: [
                        { name: "NUC",      addr: "10.0.0.1",  state: "NO LINK", detail: "UNREACHABLE", bad: true },
                        { name: "HEAD PI",  addr: "10.0.0.10", state: "DOWN",    detail: "UNREACHABLE", bad: true },
                        { name: "CHEST PI", addr: "10.0.0.11", state: "UP",      detail: "THIS MACHINE", bad: false }
                    ]
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 62
                        radius: Th.radius
                        color: Th.readout
                        border.color: Th.readoutEdge
                        border.width: 1
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 16
                            spacing: 12
                            ColumnLayout {
                                spacing: 0
                                Text { text: modelData.name; color: Th.ink
                                       font.pixelSize: 20; font.family: Th.font
                                       font.letterSpacing: Th.tracking }
                                Text { text: modelData.addr; color: Th.dimInk
                                       font.pixelSize: 13; font.family: Th.font }
                            }
                            Text {
                                text: modelData.state
                                color: modelData.bad ? Th.badInk : Th.okInk
                                font.pixelSize: 20; font.family: Th.font
                                font.letterSpacing: Th.tracking
                                Layout.preferredWidth: 130
                            }
                            Text {
                                text: modelData.detail; color: Th.dimInk
                                font.pixelSize: 18; font.family: Th.font
                                font.letterSpacing: Th.tracking
                                Layout.fillWidth: true
                                elide: Text.ElideRight     // cannot overflow
                            }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
            }

            // ---- the e-stop: the control that decides whether this is worth
            // doing at all. Arm on first tap, fire on the second, and it must
            // look unmistakably different in each state.
            Rectangle {
                id: stop
                objectName: "estop"
                Layout.preferredWidth: 286
                Layout.fillHeight: true
                radius: Th.radius
                property int stage: 0        // 0 idle, 1 armed, 2 latched
                color: stage === 1 ? Th.stopArm : Th.stopPanel
                border.color: stage === 1 ? Th.warnInk : Th.badInk
                border.width: stage === 1 ? 3 : 2
                scale: tap.pressed ? 0.97 : 1.0
                Behavior on scale { NumberAnimation { duration: 90 } }
                Behavior on color { ColorAnimation { duration: 120 } }

                Text {
                    anchors.centerIn: parent
                    width: parent.width - 32
                    horizontalAlignment: Text.AlignHCenter
                    text: stop.stage === 0 ? "STOP"
                        : stop.stage === 1 ? "TAP AGAIN" : "STOPPED"
                    color: "#ffffff"
                    font.pixelSize: 58; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    fontSizeMode: Text.HorizontalFit    // cannot overflow
                    minimumPixelSize: 20
                }
                MouseArea {
                    id: tap
                    anchors.fill: parent
                    onClicked: stop.stage = stop.stage === 1 ? 2 : 1
                }
            }
        }
    }

    // A button in the panel's style. One definition, so a press animation and a
    // label that fits are properties of "button" rather than of each caller.
    component Btn: Rectangle {
        id: btn
        property string label: ""
        property bool on: false
        property bool big: false
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

        // The HUD theme's halo, drawn as a sibling *behind* the button rather
        // than bleeding out of it. Its size is declared, so the layout's
        // spacing is what keeps it off the neighbours.
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
            text: btn.label
            color: btn.on ? Th.ink : Th.dimInk
            font.pixelSize: btn.big ? 26 : 18
            font.family: Th.font
            font.letterSpacing: Th.tracking
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            fontSizeMode: Text.HorizontalFit     // shrinks instead of spilling
            minimumPixelSize: 9
        }
        MouseArea { id: ma; anchors.fill: parent; onClicked: btn.tapped() }
    }
}
