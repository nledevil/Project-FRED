import QtQuick
import QtQuick.Layouts

// The PIN gate. Four digits and a stop, and nothing else to get wrong: it
// submits on the fourth rather than offering an ENTER, because the length is
// fixed and an extra tap would only ever be a way to get it wrong.
//
// The lockout after repeated wrong guesses is pin_gate.py's, not this file's.
// It is the only thing between this panel and a four-digit brute force.
Item {
    id: gate
    readonly property var keys: [["1","2","3"], ["4","5","6"],
                                 ["7","8","9"], ["CLEAR","0","DEL"]]

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 4
        spacing: 10

        Text {
            text: "ENTER PIN TO OPEN SETTINGS"; color: Th.dimInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
        }

        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 16
            Repeater {
                model: P.gateView.length || 4
                // Filled boxes rather than asterisks: the bitmap font had no
                // "*" and drew a blank, which read as a lost keypress. The
                // fill is what tells you the tap landed.
                Rectangle {
                    width: 34; height: 34; radius: 6
                    color: index < (P.gateView.filled || 0) ? Th.ink : "transparent"
                    border.color: Th.edge; border.width: 2
                }
            }
        }

        Text {
            text: P.gateView.message || ""
            color: P.gateView.locked ? Th.warnInk : Th.badInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredHeight: 24
        }

        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 2      // two shares of the row
                Layout.fillHeight: true
                spacing: 8
                Repeater {
                    model: gate.keys
                    RowLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        spacing: 8
                        property var row: modelData
                        Repeater {
                            model: row.length
                            Btn {
                                Layout.fillWidth: true; Layout.fillHeight: true
                                label: row[index]
                                fontPx: row[index].length > 1 ? Th.px["2"] : Th.px["4"]
                                // Dead while locked out, and visibly so.
                                enabled: !(P.gateView.locked || false)
                                opacity: enabled ? 1.0 : 0.4
                                onTapped: P.pinKey(row[index])
                            }
                        }
                    }
                }
            }

            // Reachable without the PIN, deliberately: a locked screen must not
            // stand between a person and stopping the cart.
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredWidth: 1      // ...and one for the stop
                Layout.fillHeight: true
                radius: Th.radius
                color: Th.stopPanel
                border.color: Th.badInk; border.width: 2
                scale: sm.pressed ? 0.97 : 1.0
                Behavior on scale { NumberAnimation { duration: 90 } }
                Text {
                    anchors.centerIn: parent
                    width: parent.width - 24
                    horizontalAlignment: Text.AlignHCenter
                    text: "STOP"
                    color: "#ffffff"
                    font.pixelSize: Th.px["8"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    fontSizeMode: Text.HorizontalFit
                    minimumPixelSize: 20
                }
                MouseArea { id: sm; anchors.fill: parent; onClicked: P.pinStop() }
            }
        }
    }
}
