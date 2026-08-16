import QtQuick
import QtQuick.Layouts

// The STATUS page. Every row's text and colour is decided by page_status.py and
// arrives as data through P.statusRows — this file lays it out and nothing
// more. Whether the head is "up" is a claim about the robot and must not have
// two implementations; how it is drawn may.
Item {
    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        Repeater {
            model: P.statusRows
            Rectangle {
                Layout.fillWidth: true
                // 52 in a 62 pitch, as page_status draws it: ROW_H is 62 and
                // the readout it paints is ROW_H-10. Using 62 solid pushed the
                // "UPDATED" line off the bottom of the panel.
                Layout.preferredHeight: 52
                radius: Th.radius
                color: Th.readout
                border.color: Th.readoutEdge
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 16; anchors.rightMargin: 16
                    spacing: 12

                    ColumnLayout {
                        spacing: 0
                        Layout.preferredWidth: 150
                        Text {
                            text: modelData.name; color: Th.ink
                            font.pixelSize: Th.px["2"]; font.family: Th.font
                            font.letterSpacing: Th.tracking
                        }
                        Text {
                            text: modelData.where; color: Th.dimInk
                            font.pixelSize: Th.px["1"]; font.family: Th.font
                        }
                    }
                    Text {
                        text: modelData.state
                        color: modelData.ink
                        font.pixelSize: Th.px["2"]; font.family: Th.font
                        font.letterSpacing: Th.tracking
                        Layout.preferredWidth: 170
                    }
                    Text {
                        text: modelData.detail; color: Th.dimInk
                        font.pixelSize: Th.px["2"]; font.family: Th.font
                        font.letterSpacing: Th.tracking
                        Layout.fillWidth: true
                        elide: Text.ElideRight        // cannot overflow
                    }
                }
            }
        }
        Item { Layout.fillHeight: true }
        Text {
            text: "UPDATED " + (P.snap.age === undefined || P.snap.age === null
                                ? "NEVER" : Math.round(P.snap.age) + "S AGO")
            color: Th.dimInk
            font.pixelSize: Th.px["1"]; font.family: Th.font
        }
    }
}
