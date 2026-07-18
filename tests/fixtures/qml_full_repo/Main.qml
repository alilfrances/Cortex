pragma Singleton
import QtQuick 2.15
import QtQuick.Controls 2.15 as Controls
import "helpers.js" as Helpers

ApplicationWindow {
    id: root
    required property string title
    default property list<QtObject> children
    property alias selected: card.value
    property int count: 1
    signal refreshed(string value)
    signal noParentheses
    enum Mode { Idle, Active = 2 }
    component Header: Rectangle { property string text: "Header" }

    Card {
        id: card
        value: root.count
        onValueChanged: refreshed(title)
    }
    Behavior on opacity { NumberAnimation {} }
    Connections {
        target: card
        function onValueChanged(value) { root.count = value; Helpers.log(value) }
    }
}
