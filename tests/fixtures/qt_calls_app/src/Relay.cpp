#include <QObject>
#include "helpers.hpp"

class Relay : public QObject {
    Q_OBJECT
public:
    explicit Relay(QObject *parent = nullptr);

public slots:
    void onFound(int id);
};

void Relay::onFound(int id) {
    log_event(id);
}
