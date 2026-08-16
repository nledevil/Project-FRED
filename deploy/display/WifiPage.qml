import QtQuick
import QtQuick.Layouts

// The WIFI page: the access point the brain hosts. Editing a name or password
// opens the keyboard; the password is never seeded from the brain, which is
// page_wireless.py's rule and stays its rule.
Item {
    id: page
    function inkOf(name) {
        return name === "ok" ? Th.okInk
             : name === "bad" ? Th.badInk
             : name === "warn" ? Th.warnInk
             : name === "dim" ? Th.dimInk : Th.ink
    }

    property var editing: null
    property int half: StartWifiHalf   // 0 = the AP we host, 1 = the WiFi we join

    // The switch between them, at the top so it reads as one tab with two
    // halves rather than two tabs that happen to be about radios.
    RowLayout {
        id: picker
        anchors.top: parent.top
        anchors.left: parent.left; anchors.right: parent.right
        height: 44
        spacing: 8
        Btn {
            Layout.fillWidth: true; Layout.fillHeight: true
            label: "FRED'S HOTSPOT"; on: page.half === 0
            onTapped: page.half = 0
        }
        Btn {
            Layout.fillWidth: true; Layout.fillHeight: true
            label: "JOIN A NETWORK"; on: page.half === 1
            onTapped: { page.half = 1; if (!P.uplinkView.scanned) P.scanUplink() }
        }
    }

    UplinkView {
        // Scanned on arrival rather than on a timer: it costs a couple of
        // seconds of radio and only matters while somebody is looking.
        Component.onCompleted: if (page.half === 1 && !P.uplinkView.scanned) P.scanUplink()
        anchors.top: picker.bottom; anchors.topMargin: 10
        anchors.left: parent.left; anchors.right: parent.right
        anchors.bottom: parent.bottom
        visible: page.half === 1
    }

    ColumnLayout {
        anchors.top: picker.bottom; anchors.topMargin: 10
        anchors.left: parent.left; anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: 10
        visible: page.half === 0 && page.editing === null

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Text {
                text: "ACCESS POINT"; color: Th.ink
                font.pixelSize: Th.px["3"]; font.family: Th.font
                font.letterSpacing: Th.tracking
            }
            Text {
                text: "HOSTED ON THE BRAIN"; color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
                Layout.alignment: Qt.AlignBottom
                Layout.bottomMargin: 4
            }
            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Btn {
                label: P.wifiView.label || "OFF"
                on: P.wifiView.on || false
                implicitWidth: 160; implicitHeight: 56
                fontPx: Th.px["3"]
                enabled: P.wifiView.live || false
                onTapped: if (P.wifiView.live) P.toggleHotspot()
            }
            Btn {
                label: P.wifiView.ssidLabel || "NAME"
                implicitWidth: 160; implicitHeight: 56
                enabled: P.wifiView.editable || false
                onTapped: if (P.wifiView.editable)
                    page.editing = P.hotspotEditor("ssid")
            }
            Btn {
                label: "PASSWORD"
                implicitWidth: 200; implicitHeight: 56
                enabled: P.wifiView.editable || false
                onTapped: if (P.wifiView.editable)
                    page.editing = P.hotspotEditor("psk")
            }
            Item { Layout.fillWidth: true }
        }

        Repeater {
            model: P.wifiView.rows || []
            Text {
                text: modelData.text; color: page.inkOf(modelData.ink)
                font.pixelSize: Th.px["2"]; font.family: Th.font
                font.letterSpacing: Th.tracking
                Layout.fillWidth: true; elide: Text.ElideRight
            }
        }

        Item { Layout.fillHeight: true }

        Text {
            text: P.wifiView.hint || ""; color: Th.dimInk
            font.pixelSize: Th.px["1"]; font.family: Th.font
        }
        Text {
            text: P.wifiView.note || ""; color: Th.warnInk
            font.pixelSize: Th.px["1"]; font.family: Th.font
        }
    }

    Keyboard {
        anchors.fill: parent
        visible: page.half === 0 && page.editing !== null
        title: page.editing ? page.editing.title : ""
        seed: page.editing ? page.editing.value : ""
        maxLen: page.editing ? page.editing.maxLen : 32
        minLen: page.editing ? page.editing.minLen : 1
        onAccepted: function (text) {
            P.commitHotspot(page.editing.field, text)
            page.editing = null
        }
        onCancelled: page.editing = null
    }
}
