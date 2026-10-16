#pragma once
#include <memory>
#include "worker.hpp"

class Engine;

class Gateway : public Worker {
public:
    void send();

private:
    std::shared_ptr<Engine> engine_;
    Worker *worker_;
};
