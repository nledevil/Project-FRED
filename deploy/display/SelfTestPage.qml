import QtQuick
import QtQuick.Layouts

// The TEST tab. Every word and colour comes from page_selftest.py through
// P.selftestView; this file lays it out. Problems are sorted to the top there,
// so the first screenful is the one worth reading.
Item {
    clip: true
    readonly property var v: P.selftestView

    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // ---- the buttons, and the verdict beside them ----------------------
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            spacing: 10
            Btn {
                label: "RUN SELF TEST"; big: true; on: true
                implicitWidth: 250; implicitHeight: 56
                onTapped: P.runSelfTest()
            }
            Btn {
                label: v.motionLabel || "MOTION TEST"; on: !!v.armed
                implicitWidth: 190; implicitHeight: 56
                onTapped: P.tapMotionTest()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text {
                    text: v.headline || ""; color: v.ink || Th.ink
                    font.pixelSize: Th.px["3"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.fillWidth: true
                    fontSizeMode: Text.HorizontalFit; minimumPixelSize: 12
                }
                Text {
                    text: v.armed ? "EACH JOINT MOVES A FEW DEGREES - STAND CLEAR"
                                  : (v.sub || "")
                    color: v.armed ? Th.warnInk : Th.dimInk
                    font.pixelSize: Th.px["1"]; font.family: Th.font
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
            }
        }

        // ---- one row per check, fails first --------------------------------
        Repeater {
            model: v.rows || []
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                radius: Th.radius
                color: Th.readout
                border.color: Th.readoutEdge
                border.width: 1
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14; anchors.rightMargin: 14
                    spacing: 12
                    Text {
                        text: modelData.state; color: modelData.ink
                        font.pixelSize: Th.px["2"]; font.family: Th.font
                        font.letterSpacing: Th.tracking
                        Layout.preferredWidth: 76
                    }
                    Text {
                        text: modelData.name; color: Th.ink
                        font.pixelSize: Th.px["2"]; font.family: Th.font
                        font.letterSpacing: Th.tracking
                        Layout.preferredWidth: 236
                        fontSizeMode: Text.HorizontalFit; minimumPixelSize: 12
                    }
                    Text {
                        text: modelData.detail; color: Th.dimInk
                        font.pixelSize: Th.px["1"]; font.family: Th.font
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap; maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }
            }
        }
        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            visible: (v.pages || 1) > 1
            spacing: 12
            Btn { label: "<"; implicitWidth: 60; onTapped: P.turnSelfTestPage(-1) }
            Text {
                text: ((v.page || 0) + 1) + " / " + (v.pages || 1)
                color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                verticalAlignment: Text.AlignVCenter
            }
            Btn { label: ">"; implicitWidth: 60; onTapped: P.turnSelfTestPage(1) }
        }
    }
}
