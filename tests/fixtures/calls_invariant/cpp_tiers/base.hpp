#pragma once

class Base {
public:
    void ping() {
        // inherited member reached by implicit-this and this->
    }
    virtual void tick() {
    }
};
