#pragma once
#include <QObject>
#include <QPointer>
#include "Cache.hpp"

class Scanner : public QObject {
    Q_OBJECT
public:
    explicit Scanner(QObject *parent = nullptr);
    void scan();
    void refresh();

signals:
    void found(int id);

public slots:
    void onFound(int id);

private:
    QPointer<Cache> cache_;
};
