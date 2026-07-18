import QtQuick 2.15
Item {
    id: cardRoot
    property int value: 0
    signal selected(int value)
    function update(value) { value = value + 1; selected(value) }
}
