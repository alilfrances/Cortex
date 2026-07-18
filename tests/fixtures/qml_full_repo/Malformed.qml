import QtQuick 2.15
Item {
    property int retained: 1
    function ok() { return retained }
    // missing closing brace is recoverable
