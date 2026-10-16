#pragma once
#include <QObject>

class Cache : public QObject {
    Q_OBJECT
public:
    explicit Cache(QObject *parent = nullptr);
    void store(int id) {
        total_ += id;
    }

private:
    int total_ = 0;
};
