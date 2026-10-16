#include "Scanner.hpp"
#include "helpers.hpp"

Scanner::Scanner(QObject *parent) : QObject(parent) {
    connect(this, &Scanner::found, this, &Scanner::onFound);
}

void Scanner::scan() {
    normalize_id(7);
    refresh();
    emit found(1);
    cache_->store(1);
}

void Scanner::refresh() {
    qDebug("refresh");
}

void Scanner::onFound(int id) {
    log_event(id);
}
