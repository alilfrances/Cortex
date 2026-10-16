#include <memory>
#include "util.hpp"
#include "worker.hpp"

namespace Net {
int connect_count() {
    return 1;
}
}

class Engine {
public:
    void warm() {
    }
    static Engine make() {
        return Engine();
    }
};

static void free_helper(int level) {
}

class Controller : public Base {
    std::shared_ptr<Engine> engine_;
    Worker *worker_;

public:
    void warm_up() {
    }

    void run(Worker *w, Engine &e) {
        free_helper(1);
        this->warm_up();
        warm_up();
        Engine::make();
        Net::connect_count();
        Worker local;
        local.work();
        w->work();
        e.warm();
        engine_->warm();
        worker_->work();
        auto x = Engine();
        x.warm();
        auto *y = new Worker();
        y->work();
        ping();
        this->tick();
        clamp(3);
        compute_total(4);
        render();
        std::vector<int> values;
        values.push_back(1);
        printf("done");
    }
};
