import QtQuick
import QtQuick.Layouts

// The visitor's card: what FRED is, who built him, how to talk to him — and a
// page to draw on. Reached by swiping up on the animation; it is the one thing
// on this screen a child can open without a PIN, because everything on it is
// read-only. Closes on the X, on a tap outside the text, or by itself after
// ABOUT_IDLE_S without a touch (panel.py keeps that clock, so no page has to
// remember to report activity).
//
// Nothing here is a fact the brain also knows in a different form: the text
// matches the system prompt in inmoov/brain.py — three machines, one builder —
// so the card and FRED cannot disagree about what he is.
Item {
    id: card
    anchors.fill: parent

    property bool drawing: false

    Rectangle { anchors.fill: parent; color: Th.bg; opacity: 0.96 }

    // A tap on the backdrop closes the card. The content sits above this and
    // swallows its own taps, so reading it is not the same as leaving it.
    MouseArea { anchors.fill: parent; onClicked: P.closeAbout() }

    // ---- the card ---------------------------------------------------------
    Rectangle {
        anchors.fill: parent
        anchors.margins: 24
        radius: Th.radius
        color: Th.panel
        border.color: Th.edge
        border.width: Th.style === "soft" ? 0 : 1
        visible: !card.drawing
        MouseArea { anchors.fill: parent }          // swallow: taps here don't close

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 8

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Text {
                    text: "FRED"
                    color: Th.ink
                    font.pixelSize: Th.px["3"] * 1.6; font.family: Th.font
                    font.letterSpacing: Th.tracking * 2
                }
                Text {
                    text: "FACIAL RECOGNITION AND EXPRESSION DROID"
                    color: Th.dimInk
                    font.pixelSize: Th.px["1"]; font.family: Th.font
                    font.letterSpacing: Th.tracking
                    Layout.fillWidth: true
                    verticalAlignment: Text.AlignVCenter
                    fontSizeMode: Text.HorizontalFit
                    minimumPixelSize: 8
                }
                Btn { label: "DRAW"; implicitWidth: 92; onTapped: card.drawing = true }
                Btn { label: "X"; implicitWidth: 64; big: true; onTapped: P.closeAbout() }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 20

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 10
                    Repeater {
                        model: [
                            "An InMoov robot head, designed, built and coded by Ryan Schultz.",
                            "Three computers run him. A NUC in the base is his brain: speech, "
                            + "vision and conversation. A Raspberry Pi in his head drives the "
                            + "servos and his eye camera. A second Pi in his chest runs the "
                            + "sensors and this screen.",
                            "Say \"Fred\" to wake him, then ask him anything. Tap the screen to "
                            + "change the look. Swipe up for this card."
                        ]
                        Text {
                            Layout.fillWidth: true
                            text: modelData
                            color: Th.ink
                            font.pixelSize: Th.px["2"]; font.family: Th.font
                            wrapMode: Text.WordWrap
                            lineHeight: 1.15
                        }
                    }
                    Item { Layout.fillHeight: true }
                }

                // A build photo, if one has been dropped in beside the panel as
                // about.png. Nothing is shipped: the file is the operator's.
                Image {
                    id: photo
                    visible: status === Image.Ready
                    Layout.preferredWidth: visible ? 260 : 0
                    Layout.fillHeight: true
                    fillMode: Image.PreserveAspectFit
                    source: AboutImage
                    smooth: true
                }
            }
        }
    }

    // ---- the doodle page ----------------------------------------------------
    // A finger draws; nothing is kept. The page clears itself after a short
    // idle so the next visitor starts blank, and the card's own idle close
    // takes the whole thing down after that.
    Item {
        anchors.fill: parent
        visible: card.drawing

        Canvas {
            id: paper
            anchors.fill: parent
            property real lastX: -1
            property real lastY: -1
            property color pen: Accent

            function clear() {
                var ctx = getContext("2d")
                ctx.fillStyle = "black"
                ctx.fillRect(0, 0, width, height)
                requestPaint()
            }
            function stroke(x, y, down) {
                var ctx = getContext("2d")
                ctx.strokeStyle = pen
                ctx.lineWidth = 6
                ctx.lineCap = "round"
                ctx.beginPath()
                if (down || lastX < 0) ctx.moveTo(x, y)
                else ctx.moveTo(lastX, lastY)
                ctx.lineTo(x, y)
                ctx.stroke()
                lastX = x; lastY = y
                requestPaint()
                idle.restart()
            }
            onAvailableChanged: if (available) clear()

            MultiPointTouchArea {
                anchors.fill: parent
                maximumTouchPoints: 1
                onPressed: (points) => { paper.stroke(points[0].x, points[0].y, true) }
                onUpdated: (points) => { paper.stroke(points[0].x, points[0].y, false) }
                onReleased: { paper.lastX = -1; paper.lastY = -1 }
            }
            // The mouse path too, so the harness's synthetic clicks and a
            // desktop run behave like a finger.
            MouseArea {
                anchors.fill: parent
                onPressed: (m) => { paper.stroke(m.x, m.y, true) }
                onPositionChanged: (m) => { if (pressed) paper.stroke(m.x, m.y, false) }
                onReleased: { paper.lastX = -1; paper.lastY = -1 }
            }
            Timer { id: idle; interval: 20000; onTriggered: paper.clear() }
        }

        RowLayout {
            anchors.top: parent.top; anchors.right: parent.right
            anchors.margins: 12
            spacing: 8
            Btn { label: "CLEAR"; implicitWidth: 92; onTapped: paper.clear() }
            Btn { label: "DONE"; implicitWidth: 92; onTapped: { paper.clear(); card.drawing = false } }
        }
        Text {
            anchors.left: parent.left; anchors.top: parent.top; anchors.margins: 16
            text: "DRAW SOMETHING FOR FRED"
            color: Th.dimInk
            font.pixelSize: Th.px["1"]; font.family: Th.font
            font.letterSpacing: Th.tracking
        }
    }
}
