import QtQuick 2.15

Item {
    ScannerView {
        onFound: console.log("found", id)
    }

    Scanner {
    }
}
