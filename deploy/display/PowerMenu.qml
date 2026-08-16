import QtQuick
import QtQuick.Layouts

// The power menu: shut the robot down from the panel, at the end of an event.
//
// Every control arms on the first tap and fires on the second. The order is
// power_menu.py's and is not negotiable here — head, then the brain, then this
// screen last, because the chest taking itself down first would leave nothing
// able to ask the others.
Item {
    id: pm
    visible: P.powerView.open || false

    // Swallows everything while it is up, so a stray tap cannot reach the page
    // underneath and change something on the way past.
    MouseArea { anchors.fill: parent }

    Rectangle {
        anchors.fill: parent
        color: Th.bg
        opacity: 0.92
    }

    ColumnLayout {
        anchors.centerIn: parent
        width: parent.width - 120
        spacing: 12

        Text {
            text: P.powerView.doing ? "SHUTTING DOWN - " + P.powerView.doing.toUpperCase()
                                    : "SHUT DOWN"
            color: P.powerView.doing ? Th.warnInk : Th.ink
            font.pixelSize: Th.px["3"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
        }

        Btn {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            fontPx: Th.px["3"]
            label: P.powerView.allArmed ? "TAP AGAIN TO SHUT DOWN ALL" : "SHUT DOWN ALL"
            on: P.powerView.allArmed || false
            enabled: !(P.powerView.doing)
            onTapped: P.powerTap("all")
        }

        Repeater {
            model: P.powerView.machines || []
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Btn {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 46
                    label: modelData.armed ? "TAP AGAIN - " + modelData.label
                                           : modelData.label
                    on: modelData.armed
                    enabled: !(P.powerView.doing)
                    onTapped: P.powerTap(modelData.key)
                }
                Text {
                    text: modelData.why.toUpperCase(); color: Th.dimInk
                    font.pixelSize: Th.px["1"]; font.family: Th.font
                    Layout.preferredWidth: 240
                    elide: Text.ElideRight
                }
            }
        }

        // What would not take it. The chest is spared from this list on
        // purpose: it is the machine drawing the message.
        Text {
            visible: (P.powerView.failed || []).length > 0
            text: "WOULD NOT SHUT DOWN: " + (P.powerView.failed || []).join(", ").toUpperCase()
            color: Th.badInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
        }

        Btn {
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: 200
            Layout.preferredHeight: 52
            label: "CANCEL"
            enabled: !(P.powerView.doing)
            onTapped: P.powerTap("cancel")
        }
    }
}
