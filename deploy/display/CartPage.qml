import QtQuick
import QtQuick.Layouts

// The CART page: who may drive the base, and the stop. Which mode hands a
// moving machine to a hand controller, and why a command would be refused, are
// page_cart.py's judgements arriving through P.cartView.
Item {
    function inkOf(name) {
        return name === "ok" ? Th.okInk
             : name === "bad" ? Th.badInk
             : name === "warn" ? Th.warnInk
             : name === "dim" ? Th.dimInk : Th.ink
    }

    RowLayout {
        anchors.fill: parent
        spacing: 20

        // ---- the modes, and what is stopping the cart --------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 10

            Repeater {
                model: P.cartView.modes || []
                Btn {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    label: modelData.label
                    on: modelData.on
                    onTapped: P.pickCartMode(modelData.mode)
                }
            }

            Item { Layout.fillHeight: true }

            Text {
                text: P.cartView.hint || ""; color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                Layout.fillWidth: true; elide: Text.ElideRight
            }
            Text {
                visible: !(P.cartView.reachable) || (P.cartView.saving || false)
                text: P.cartView.reachable ? "SAVING..." : "NO CART DRIVER ON THIS PI"
                color: P.cartView.reachable ? Th.dimInk : Th.badInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
            Text {
                text: P.cartView.padLine || ""; color: inkOf(P.cartView.padInk)
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                Layout.fillWidth: true; elide: Text.ElideRight
            }
            Text {
                text: P.cartView.why || ""; color: inkOf(P.cartView.whyInk)
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                Layout.fillWidth: true; elide: Text.ElideRight
            }
        }

        // ---- the stop ----------------------------------------------------
        ColumnLayout {
            Layout.preferredWidth: 286
            Layout.fillHeight: true
            spacing: 10

            Rectangle {
                id: stop
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: Th.radius
                color: P.cartView.armed ? Th.stopPanelArm : Th.stopPanel
                border.color: P.cartView.armed ? Th.warnInk : Th.badInk
                border.width: P.cartView.armed ? 3 : 2
                scale: ma.pressed ? 0.97 : 1.0
                Behavior on scale { NumberAnimation { duration: 90 } }
                Behavior on color { ColorAnimation { duration: 120 } }

                Text {
                    anchors.centerIn: parent
                    width: parent.width - 32
                    horizontalAlignment: Text.AlignHCenter
                    text: P.cartView.stopLabel || "STOP"
                    color: "#ffffff"
                    font.pixelSize: Th.px["8"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    fontSizeMode: Text.HorizontalFit
                    minimumPixelSize: 20
                }
                MouseArea { id: ma; anchors.fill: parent; onClicked: P.cartStop() }
            }

            // Battery and board temperature, next to the controls that spend
            // them. Shown, not judged: this pack has read 23% low before, and a
            // panel that cries wolf stops being read.
            RowLayout {
                Layout.fillWidth: true
                spacing: 16
                Text {
                    text: P.cartView.volts !== undefined && P.cartView.volts !== null
                          ? P.cartView.volts.toFixed(1) + "V" : "-- V"
                    color: Th.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                }
                Text {
                    text: P.cartView.tempC !== undefined && P.cartView.tempC !== null
                          ? Math.round(P.cartView.tempC) + "C" : "-- C"
                    color: Th.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                }
            }
        }
    }
}
