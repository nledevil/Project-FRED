import QtQuick
import QtQuick.Layouts

// The DISPLAY page: what the panel is showing, and which of the three looks it
// wears. Both lists and every judgement about them — including that "off" lit
// is a blank screen on purpose and should not be coloured like a healthy
// animation — come from page_display.py through P.displayView.
Item {
    function inkOf(name) {
        return name === "ok" ? Th.okInk
             : name === "bad" ? Th.badInk
             : name === "warn" ? Th.warnInk
             : name === "dim" ? Th.dimInk : Th.ink
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        Text {
            visible: P.displayView.empty || false
            text: "NO ANIMATION LIST FROM THIS PI"
            color: Th.dimInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.alignment: Qt.AlignHCenter
        }

        // The animation grid, two rows of four, paged.
        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: 4
            rowSpacing: 12
            columnSpacing: 12
            Repeater {
                model: P.displayView.animations || []
                Btn {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    label: modelData.label
                    on: modelData.on
                    onTapped: P.pickAnimation(modelData.id)
                }
            }
        }

        // Pager, only when there is somewhere to page to.
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            visible: (P.displayView.pages || 1) > 1
            spacing: 12
            Btn { label: "<"; implicitWidth: 60; onTapped: P.turnPage(-1) }
            Text {
                text: ((P.displayView.page || 0) + 1) + " / " + (P.displayView.pages || 1)
                color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                verticalAlignment: Text.AlignVCenter
            }
            Btn { label: ">"; implicitWidth: 60; onTapped: P.turnPage(1) }
        }

        // The look of the menu: a local choice, and available even when the Pi
        // cannot say what it is running.
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            spacing: 12
            Repeater {
                model: P.displayView.themes || []
                Btn {
                    Layout.fillWidth: true
                    label: modelData.label
                    on: modelData.on
                    onTapped: P.pickTheme(modelData.name)
                }
            }
        }

        Text {
            text: P.displayView.status || ""
            color: inkOf(P.displayView.statusInk)
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
        }
    }
}
