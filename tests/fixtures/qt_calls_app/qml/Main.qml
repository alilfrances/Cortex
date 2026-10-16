import QtQuick 2.15

Item {
    Scanner {
        onFound: console.log("found", id)
    }
}
