#pragma once
#include <QObject>
class Widget : public QObject {
    Q_OBJECT
    QML_NAMED_ELEMENT(Widget)
    Q_PROPERTY(int value READ value NOTIFY valueChanged)
public:
    Q_INVOKABLE void update();
signals:
    void valueChanged();
};
