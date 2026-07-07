import QtQuick 2.15

Item {
    signal started()

    function launch() {
        started()
    }
}
