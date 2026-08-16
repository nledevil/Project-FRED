import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

// The SERVOS page. Which servos exist, what order they come in and whether a
// drag would reach the hardware are all page_servos.py's answers, arriving
// through P.servosView. The slider is Qt's; the limits are the calibrated ones.
Item {
    ColumnLayout {
        anchors.fill: parent
        spacing: 6

        ColumnLayout {
            visible: P.servosView.empty || false
            spacing: 4
            Text {
                text: "NO SERVOS REPORTED"; color: Th.badInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
            Text {
                text: P.servosView.blocked || ""; color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
        }

        Repeater {
            model: P.servosView.rows || []
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                spacing: 12
                Text {
                    text: modelData.label; color: Th.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.preferredWidth: 190
                    elide: Text.ElideRight
                }
                Slider {
                    id: slider
                    Layout.fillWidth: true
                    from: modelData.lo
                    to: modelData.hi
                    // Follows the robot except while a finger is on it, which
                    // is what page_servos does with its "held" window.
                    value: pressed ? value : modelData.angle
                    enabled: !(P.servosView.blocked)
                    onMoved: P.moveServo(modelData.name, value, false)
                    onPressedChanged: if (!pressed)
                        P.moveServo(modelData.name, value, true)

                    background: Item {
                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: parent.width; height: 10
                            radius: 4
                            color: Th.panel
                            border.color: Th.edge; border.width: 1
                            Rectangle {
                                width: slider.visualPosition * parent.width
                                height: parent.height; radius: parent.radius
                                color: Th.panelOn
                            }
                        }
                        // The tick at rest, so "where should this be" is
                        // answerable at a glance.
                        Rectangle {
                            x: (modelData.hi > modelData.lo
                                ? (modelData.rest - modelData.lo)
                                  / (modelData.hi - modelData.lo) : 0) * parent.width - 1
                            anchors.verticalCenter: parent.verticalCenter
                            width: 2; height: 28; color: Th.edge
                        }
                    }
                    handle: Rectangle {
                        x: slider.visualPosition * (slider.availableWidth - width)
                        anchors.verticalCenter: parent.verticalCenter
                        width: 16; height: 32; radius: 4
                        color: P.servosView.blocked ? Th.dimInk : Th.ink
                    }
                }
                Text {
                    text: Math.round(modelData.angle)
                    color: P.servosView.blocked ? Th.dimInk : Th.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    horizontalAlignment: Text.AlignRight
                    Layout.preferredWidth: 60
                }
            }
        }

        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Btn { label: "REST"; implicitWidth: 120; onTapped: P.restServos() }
            Item { Layout.fillWidth: true }
            Btn {
                visible: (P.servosView.pages || 1) > 1
                label: "<"; implicitWidth: 60; onTapped: P.turnServoPage(-1)
            }
            Text {
                visible: (P.servosView.pages || 1) > 1
                text: ((P.servosView.page || 0) + 1) + " / " + (P.servosView.pages || 1)
                color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                verticalAlignment: Text.AlignVCenter
            }
            Btn {
                visible: (P.servosView.pages || 1) > 1
                label: ">"; implicitWidth: 60; onTapped: P.turnServoPage(1)
            }
        }

        Text {
            text: P.servosView.blocked
                  || "DRAG TO MOVE - LIMITS ARE THE CALIBRATED ONES"
            color: P.servosView.blocked ? Th.badInk : Th.dimInk
            font.pixelSize: P.servosView.blocked ? Th.px["2"] : Th.px["1"]
            font.family: Th.font
        }
    }
}
