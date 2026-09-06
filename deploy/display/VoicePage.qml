import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

// The VOICE page: one big switch for the wake-word listener. Whether it is
// pressable — the brain unreachable, or voice unavailable so a press would
// 503 — is page_voice.py's judgement, arriving through P.voiceView.
Item {
    function inkOf(name) {
        return name === "ok" ? Th.okInk
             : name === "bad" ? Th.badInk
             : name === "dim" ? Th.dimInk : Th.ink
    }

    ColumnLayout {
        anchors.centerIn: parent
        width: parent.width
        spacing: 12

        Text {
            text: "WAKE WORD LISTENER"; color: Th.dimInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
        }

        Btn {
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: 300
            Layout.preferredHeight: 120
            fontPx: Th.px["4"]
            label: P.voiceView.label || ""
            on: P.voiceView.on || false
            enabled: P.voiceView.live || false
            opacity: enabled ? 1.0 : 0.75
            onTapped: if (P.voiceView.live) P.toggleVoice()
        }

        // Whether he starts listening by himself after a reboot — a different
        // question from the big switch above, which is about right now.
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 4
            spacing: 10
            visible: P.voiceView.live || false
            Btn {
                label: (P.voiceView.atBoot ? "[X]" : "[  ]")
                preserveCase: true
                implicitWidth: 64
                on: P.voiceView.atBoot || false
                onTapped: P.setVoiceAtBoot(!P.voiceView.atBoot)
            }
            Text {
                text: "LISTEN AFTER A REBOOT"
                color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                verticalAlignment: Text.AlignVCenter
            }
        }

        // How loud he is. Here rather than on its own tab because the strip is
        // full at seven, and because this is the page you are already on when
        // he is talking too loudly. Shown even when the listener is
        // unavailable — that is the microphone's problem, not the speaker's.
        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 6
            // A null volume means the brain has no settable mixer, so there is
            // nothing honest to draw. undefined covers the NO-LINK view, which
            // carries no volume at all.
            visible: P.voiceView.volume !== undefined && P.voiceView.volume !== null
            spacing: 12
            Text {
                text: "VOLUME"; color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                Layout.preferredWidth: 110
                verticalAlignment: Text.AlignVCenter
            }
            Slider {
                id: vol
                Layout.fillWidth: true
                // Replacing background with a size-less Item collapses the
                // control's implicit height to zero: it still draws, and the
                // touchable area is a zero-height line. Claim the height.
                Layout.preferredHeight: 46
                from: 0; to: 100; stepSize: 1
                // Follows the robot except while a finger is on it, so a poll
                // landing mid-drag cannot yank the knob out from under it.
                value: pressed ? value : (P.voiceView.volume || 0)
                // On release, not onMoved. Every set is an amixer process on
                // the brain, and a drag across this track would fire dozens.
                onPressedChanged: if (!pressed) P.setVolume(value)

                background: Item {
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width; height: 10
                        radius: 4
                        color: Th.panel
                        border.color: Th.edge; border.width: 1
                        Rectangle {
                            width: vol.visualPosition * parent.width
                            height: parent.height; radius: parent.radius
                            color: Th.panelOn
                        }
                    }
                }
                handle: Rectangle {
                    x: vol.visualPosition * (vol.availableWidth - width)
                    anchors.verticalCenter: parent.verticalCenter
                    width: 16; height: 32; radius: 4
                    color: Th.ink
                }
            }
            Text {
                // The finger's number while dragging, the robot's after — same
                // rule as the slider, so the two never disagree on screen.
                text: Math.round(vol.pressed ? vol.value
                                             : (P.voiceView.volume || 0)) + "%"
                color: Th.ink
                font.pixelSize: Th.px["2"]; font.family: Th.font
                horizontalAlignment: Text.AlignRight
                Layout.preferredWidth: 70
                verticalAlignment: Text.AlignVCenter
            }
        }

        Text {
            text: P.voiceView.status || ""
            color: inkOf(P.voiceView.statusInk)
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
        }
        Text {
            text: P.voiceView.hint || ""
            color: Th.dimInk
            font.pixelSize: Th.px["1"]; font.family: Th.font
            Layout.alignment: Qt.AlignHCenter
        }
    }
}
