#include "left.hpp"
#include "right.hpp"

class Duo : public Left, public Right {
public:
    int go() {
        return name();
    }
};
