import QtQuick
import QtQuick.Layouts

// The on-screen keyboard. The one place on this panel that shows text a person
// typed, which is why it is the one place that does not upper-case: a
// passphrase displayed as MYPASS while MyPass is what gets stored would be a
// lie told by the only screen that could have caught it.
Item {
    id: kb
    property string title: ""
    property string seed: ""
    property int maxLen: 32
    property int minLen: 1
    property bool shift: false
    property string text: ""

    signal accepted(string text)
    signal cancelled()

    onSeedChanged: text = seed
    onVisibleChanged: if (visible) { text = seed; shift = false }

    readonly property var rows: [
        "1234567890",
        "qwertyuiop",
        "asdfghjkl",
        "zxcvbnm-_."
    ]

    Rectangle { anchors.fill: parent; color: Th.bg }

    ColumnLayout {
        anchors.fill: parent
        spacing: 6

        Text {
            text: kb.title; color: Th.dimInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
        }

        // What was typed, exactly as typed.
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            radius: Th.radius
            color: Th.readout
            border.color: Th.readoutEdge; border.width: 1
            Text {
                anchors.fill: parent
                anchors.leftMargin: 12
                verticalAlignment: Text.AlignVCenter
                text: kb.text
                color: kb.text.length >= kb.minLen ? Th.ink : Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
            }
            Text {
                anchors.right: parent.right; anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                text: kb.text.length + "/" + kb.maxLen
                color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
            }
        }

        Repeater {
            model: kb.rows
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 4
                property string keys: modelData
                Repeater {
                    model: keys.length
                    Btn {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        preserveCase: true
                        label: kb.shift ? keys[index].toUpperCase() : keys[index]
                        onTapped: if (kb.text.length < kb.maxLen)
                            kb.text += kb.shift ? keys[index].toUpperCase() : keys[index]
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 4
            Btn {
                Layout.preferredWidth: 110; Layout.fillHeight: true
                label: "SHIFT"; on: kb.shift; onTapped: kb.shift = !kb.shift
            }
            Btn {
                Layout.fillWidth: true; Layout.fillHeight: true
                label: "SPACE"; preserveCase: true
                onTapped: if (kb.text.length < kb.maxLen) kb.text += " "
            }
            Btn {
                Layout.preferredWidth: 90; Layout.fillHeight: true
                label: "DEL"
                onTapped: kb.text = kb.text.slice(0, -1)
            }
            Btn {
                Layout.preferredWidth: 110; Layout.fillHeight: true
                label: "CANCEL"; onTapped: kb.cancelled()
            }
            Btn {
                Layout.preferredWidth: 110; Layout.fillHeight: true
                label: "OK"
                // Too short is not a save. The minimum is the brain's rule (a
                // WPA2 passphrase is 8 characters), enforced here so the panel
                // does not send something that will simply be refused.
                enabled: kb.text.length >= kb.minLen
                opacity: enabled ? 1.0 : 0.5
                onTapped: if (kb.text.length >= kb.minLen) kb.accepted(kb.text)
            }
        }
    }
}
