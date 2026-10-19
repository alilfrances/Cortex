import QtQuick
import QtQuick.Controls

ListView {
    id: list
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ScrollBar.vertical: ScrollBar {}
    property int count: 3
    signal picked(int i)
    function unusedHelper() { return 1 }
    function usedHelper() { return 2 }
    onPicked: usedHelper()
}
