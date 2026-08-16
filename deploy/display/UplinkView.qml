import QtQuick
import QtQuick.Layouts

// Joining somebody else's WiFi — a school's, at an event. The other half of the
// WIFI tab; the access point FRED hosts is next door.
//
// The list is paged because a school is not a house: a dozen names is normal,
// and a list that runs off the bottom of the panel is how you end up editing
// YAML on the NUC at a venue, which is the thing this exists to stop.
Item {
    id: view
    property var joining: null        // the network being asked for a password

    // dBm to bars the way every phone does it: full at a strong -50, empty
    // by -80. The exact number still rides in a small label beside them.
    function sigLevel(dbm) {
        if (dbm === null || dbm === undefined) return 0
        return dbm >= -50 ? 4 : dbm >= -60 ? 3 : dbm >= -68 ? 2 : dbm >= -76 ? 1 : 0
    }

    component SigBars: Row {
        property int dbm: -100
        property int lit: view.sigLevel(dbm)
        spacing: 3
        anchors.verticalCenter: parent ? parent.verticalCenter : undefined
        Repeater {
            model: 4
            Rectangle {
                width: 5
                height: 7 + index * 5
                anchors.bottom: parent.bottom
                radius: 1
                color: index < lit ? Th.okInk : Th.readoutEdge
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 8
        visible: view.joining === null

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ColumnLayout {
                spacing: 0
                Layout.fillWidth: true
                Text {
                    text: P.uplinkView.ssid
                          ? "ON " + P.uplinkView.ssid.toUpperCase()
                          : "NOT CONNECTED"
                    color: P.uplinkView.ssid ? Th.okInk : Th.warnInk
                    font.pixelSize: Th.px["2"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: (P.uplinkView.address || "")
                          + (P.uplinkView.signal !== null && P.uplinkView.signal !== undefined
                             ? "   " + P.uplinkView.signal + " DBM" : "")
                    color: Th.dimInk
                    font.pixelSize: Th.px["1"]; font.family: Th.font
                }
            }
            Item {
                Layout.preferredWidth: 26; Layout.preferredHeight: 26
                SigBars { dbm: P.uplinkView.signal !== null
                               && P.uplinkView.signal !== undefined
                               ? P.uplinkView.signal : -100
                          anchors.bottom: parent.bottom }
                visible: P.uplinkView.ssid ? true : false
            }
            Btn {
                label: P.uplinkView.busy ? "SCANNING" : "SCAN"
                implicitWidth: 150; implicitHeight: 48
                enabled: !(P.uplinkView.busy)
                onTapped: P.scanUplink()
            }
        }

        Text {
            visible: !(P.uplinkView.scanned) && !(P.uplinkView.busy)
            text: "TAP SCAN TO SEE WHAT IS IN RANGE"
            color: Th.dimInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
            font.letterSpacing: Th.tracking
        }
        Text {
            visible: P.uplinkView.error ? true : false
            text: P.uplinkView.error.toUpperCase()
            color: Th.badInk
            font.pixelSize: Th.px["2"]; font.family: Th.font
        }

        Repeater {
            model: P.uplinkView.rows || []
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                radius: Th.radius
                color: modelData.current ? Th.panelOn : Th.readout
                border.color: modelData.current ? Th.okInk : Th.readoutEdge
                border.width: 1
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14; anchors.rightMargin: 10
                    spacing: 12
                    Item {
                        Layout.preferredWidth: 26; Layout.preferredHeight: 24
                        SigBars { dbm: modelData.signal; anchors.bottom: parent.bottom }
                    }
                    Text {
                        text: modelData.ssid.toUpperCase()
                        color: Th.ink
                        font.pixelSize: Th.px["2"]; font.family: Th.font
                        font.letterSpacing: Th.tracking
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                    }
                    Chip { label: "SAVED"; ok: false
                           visible: modelData.saved && !modelData.current }
                    Chip { label: "OPEN"; ok: false; visible: !modelData.secure }
                    Chip { label: "CONNECTED"; ok: true; visible: modelData.current }
                    Btn {
                        label: "JOIN"
                        implicitWidth: 96; implicitHeight: 36
                        visible: !modelData.current
                        onTapped: {
                            // A saved or open network needs no password: joining
                            // is one tap, which is the point at a venue.
                            if (!modelData.secure || modelData.saved)
                                P.joinUplink(modelData.ssid, "")
                            else
                                view.joining = modelData.ssid
                        }
                    }
                }
            }
        }

        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Text {
                text: "SAVED: " + ((P.uplinkView.saved || []).join(", ").toUpperCase()
                                   || "NONE")
                color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Btn {
                visible: (P.uplinkView.pages || 1) > 1
                label: "<"; implicitWidth: 56; implicitHeight: 40
                onTapped: P.turnUplinkPage(-1)
            }
            Text {
                visible: (P.uplinkView.pages || 1) > 1
                text: ((P.uplinkView.page || 0) + 1) + " / " + (P.uplinkView.pages || 1)
                color: Th.dimInk
                font.pixelSize: Th.px["1"]; font.family: Th.font
                verticalAlignment: Text.AlignVCenter
            }
            Btn {
                visible: (P.uplinkView.pages || 1) > 1
                label: ">"; implicitWidth: 56; implicitHeight: 40
                onTapped: P.turnUplinkPage(1)
            }
        }
    }

    component Chip: Rectangle {
        property string label: ""
        property bool ok: false
        implicitWidth: chipText.implicitWidth + 16
        implicitHeight: 22
        radius: 11
        color: "transparent"
        border.color: ok ? Th.okInk : Th.readoutEdge
        border.width: 1
        Text {
            id: chipText
            anchors.centerIn: parent
            text: parent.label
            color: parent.ok ? Th.okInk : Th.dimInk
            font.pixelSize: Th.px["1"] - 2 > 8 ? Th.px["1"] - 2 : 8
            font.family: Th.font
            font.letterSpacing: Th.tracking
        }
    }

    Keyboard {
        anchors.fill: parent
        visible: view.joining !== null
        title: view.joining ? "PASSWORD FOR " + view.joining.toUpperCase() : ""
        seed: ""
        maxLen: 63
        minLen: 8
        onAccepted: function (text) {
            P.joinUplink(view.joining, text)
            view.joining = null
        }
        onCancelled: view.joining = null
    }
}
