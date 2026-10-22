#include "gateway.hpp"

void Gateway::send() {
    engine_->warm();
    worker_->work();
    this->engine_->warm();
}
