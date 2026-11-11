import QtQuick

// A picture the brain sent — FRED painting on request — shown over the
// animation until a tap or its hold runs out. The bytes are a file beside
// state.json; the URL carries the picture's counter so Qt does not serve the
// previous one from its cache. Fitted, never cropped: a 512-square on a
// 800x480 panel is 480 tall with black either side, and the caption sits in
// that margin's bottom edge rather than over the picture.
Item {
    id: card
    anchors.fill: parent

    Rectangle { anchors.fill: parent; color: "black" }

    Image {
        id: pic
        anchors.fill: parent
        anchors.bottomMargin: caption.visible ? caption.height + 8 : 0
        source: P.picture.url || ""
        cache: false
        asynchronous: true
        fillMode: Image.PreserveAspectFit
        smooth: true
        opacity: status === Image.Ready ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 220 } }
    }

    Text {
        id: caption
        visible: (P.picture.caption || "") !== ""
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 6
        anchors.horizontalCenter: parent.horizontalCenter
        width: parent.width - 48
        text: P.picture.caption || ""
        color: Th.dimInk
        font.pixelSize: Th.px["1"]; font.family: Th.font
        font.letterSpacing: Th.tracking
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
        maximumLineCount: 1
    }

    // Any tap takes it down. The cog corner is under this layer on purpose:
    // one tap to clear the picture, then the cog is there as always.
    MouseArea { anchors.fill: parent; onClicked: P.closePicture() }
}
