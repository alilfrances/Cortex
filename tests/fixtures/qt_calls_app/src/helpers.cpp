#include "helpers.hpp"

int normalize_id(int raw) {
    return raw < 0 ? 0 : raw;
}

void log_event(int id) {
    normalize_id(id);
}
