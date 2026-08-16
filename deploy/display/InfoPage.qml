import QtQuick
import QtQuick.Layouts

// The INFO page. Every line — including which of them is a warning — is decided
// by page_info.py and arrives through P.infoRows. That page knows things worth
// knowing, like inference having fallen back to the CPU, and that judgement
// should exist once.
Item {
    clip: true          // never draw past the panel, even mid-port

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Text {
            text: "WHAT THIS ROBOT IS"
            color: Th.ink
            font.pixelSize: Th.px["3"]; font.family: Th.font
            font.letterSpacing: Th.tracking
            Layout.bottomMargin: 10
        }

        // No brain, no facts: the names and versions all come from the NUC.
        ColumnLayout {
            visible: !P.brainReachable
            spacing: 4
            Text {
                text: "NO LINK TO THE BRAIN"; color: Th.badInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
            Text {
                text: "NAMES AND VERSIONS COME FROM THE NUC"; color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
            }
        }

        Repeater {
            model: P.brainReachable ? P.infoRows : []
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 26
                spacing: 8
                Text {
                    text: (modelData.label || "").toUpperCase(); color: Th.dimInk
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.preferredWidth: 250
                    elide: Text.ElideRight
                }
                Text {
                    text: (modelData.value || "").toUpperCase(); color: modelData.ink
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
            }
        }
        Item { Layout.fillHeight: true }

        // Only when there is more than a screenful. The numpy page simply
        // stopped drawing at the bottom edge and said nothing about it.
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            visible: (P.infoPaging.pages || 1) > 1
            spacing: 12
            Btn { label: "<"; implicitWidth: 60; onTapped: P.turnInfoPage(-1) }
            Text {
                text: ((P.infoPaging.page || 0) + 1) + " / " + (P.infoPaging.pages || 1)
                color: Th.dimInk
                font.pixelSize: Th.px["2"]; font.family: Th.font
                verticalAlignment: Text.AlignVCenter
            }
            Btn { label: ">"; implicitWidth: 60; onTapped: P.turnInfoPage(1) }
        }
    }
}
