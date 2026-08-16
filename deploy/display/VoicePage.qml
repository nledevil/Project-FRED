import QtQuick
import QtQuick.Layouts

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
