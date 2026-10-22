#pragma once
#include "base.hpp"

class Worker : public Base {
public:
    void work() {
        ping();
    }
};
