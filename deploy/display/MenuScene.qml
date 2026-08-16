import QtQuick
import QtQuick.Layouts

// The settings menu, in the panel app. Geometry matches settings_menu.py's
// chrome — 56px title bar, an 8px gap, a 30px tab strip — because this has to
// look like the panel people already know, not like a new one.
//
// What is different is that none of it is placed by pixel arithmetic. The tab
// row cannot fuse with the header and a label cannot overflow its button:
// spacing and fontSizeMode make those unrepresentable rather than fixed, which
// is the whole reason this port is worth doing.
Item {
    id: root
    anchors.fill: parent

    readonly property var titles: ["STATUS", "VOICE", "SERVOS", "CART",
                                   "DISPLAY", "WIFI", "INFO"]

    Rectangle { anchors.fill: parent; color: Th.bg }

    ColumnLayout {
        anchors.fill: parent
        spacing: 8

        // ---- title bar ---------------------------------------------------
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            color: Th.panel
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 24; anchors.rightMargin: 8
                anchors.topMargin: 8; anchors.bottomMargin: 8
                spacing: 8
                Text {
                    text: "FRED SETTINGS"; color: Th.ink
                    font.pixelSize: Th.px["3"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.fillWidth: true
                    verticalAlignment: Text.AlignVCenter
                }
                Btn { label: "POWER"; implicitWidth: 96 }
                Btn { label: "X"; implicitWidth: 92; big: true }
            }
        }

        // ---- tabs --------------------------------------------------------
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 24; Layout.rightMargin: 24
            spacing: 8
            Repeater {
                model: root.titles
                Btn {
                    objectName: "tab"
                    fontPx: Th.tabPx
                    label: modelData
                    on: index === P.page
                    Layout.fillWidth: true
                    implicitHeight: 30
                    onTapped: P.page = index
                }
            }
        }

        // ---- the page ----------------------------------------------------
        Loader {
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.leftMargin: 24; Layout.rightMargin: 24
            Layout.bottomMargin: 12
            // Only STATUS exists so far. The rest still live in the numpy menu,
            // which is what the cog opens until they are all here.
            sourceComponent: P.page === 0 ? statusPage : notYet
        }
    }

    Component {
        id: statusPage
        StatusPage {}
    }

    Component {
        id: notYet
        Item {
            Text {
                anchors.centerIn: parent
                text: "NOT PORTED YET"
                color: Th.dimInk
                font.pixelSize: Th.px["3"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
        }
    }
}
