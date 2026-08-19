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
            // The count, not the rows. servosView is rebuilt every tick, and
            // the moment a drag moves a servo the rows *change*, so a model
            // bound to them resets and destroys the very Slider the finger is
            // on — one onMoved per touch, then nothing. A count only changes
            // when servos appear or the page turns, so the delegate survives
            // its own drag; each row reads the live view by index instead.
            model: (P.servosView.rows || []).length
            RowLayout {
                property var row: (P.servosView.rows || [])[index] || ({})
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                spacing: 12
                Text {
                    text: row.label || ""; color: Th.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.preferredWidth: 190
                    elide: Text.ElideRight
                }
                Slider {
                    id: slider
                    Layout.fillWidth: true
                    // Replacing background with a size-less Item collapses the
                    // control's implicit height to zero. The track and knob
                    // still draw (nothing clips), so the page looks right while
                    // the touchable area is a zero-height line — every drag
                    // falls straight through to nothing. The row's height is
                    // the real hit target, so claim it.
                    Layout.preferredHeight: 46
                    from: row.lo !== undefined ? row.lo : 0
                    to: row.hi !== undefined ? row.hi : 180
                    // Follows the robot except while a finger is on it, which
                    // is what page_servos does with its "held" window.
                    value: pressed ? value : (row.angle || 0)
                    enabled: !(P.servosView.blocked)
                    onMoved: P.moveServo(row.name, value, false)
                    onPressedChanged: if (!pressed)
                        P.moveServo(row.name, value, true)

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
                            x: (row.hi > row.lo
                                ? (row.rest - row.lo)
                                  / (row.hi - row.lo) : 0) * parent.width - 1
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
                    text: Math.round(row.angle || 0)
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
            // "Reset", not "REST": the servos' *rest angles* are what it sends,
            // but nobody standing at the robot reads it that way — and the
            // brain's own tool for this is already called reset_pose. The API
            // underneath is still /api/rest; only the word on the button moved.
            Btn { label: "Reset"; implicitWidth: 120; onTapped: P.restServos() }
            // Relax, next to Reset, same as the web panel — the two are used
            // in the same session and a control that exists on one screen and
            // not the other reads as a fault. Reset parks the head and keeps
            // holding it; Relax cuts the pulses, so the head goes limp and can
            // sag under its own weight. Different enough to be its own button
            // rather than a second meaning for Reset.
            Btn { label: "Relax"; implicitWidth: 120; onTapped: P.relaxServos() }
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
                  || "DRAG TO MOVE - RELAX CUTS TORQUE, THE HEAD GOES LIMP"
            color: P.servosView.blocked ? Th.badInk : Th.dimInk
            font.pixelSize: P.servosView.blocked ? Th.px["2"] : Th.px["1"]
            font.family: Th.font
        }
    }
}
