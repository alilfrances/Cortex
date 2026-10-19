mod util;
use crate::util::helper;

struct Svc {
    repo: Repo,
}

impl Svc {
    fn run(&self, r: &Repo) {
        self.step();
        self.repo.save();
        Svc::make();
        util::free();
        helper();
        let x: Foo = Foo::new();
        x.m();
        let y = Bar::default();
        y.n();
        r.save();
        local();
    }

    fn step(&self) {
    }

    fn make() {
    }
}

fn local() {
}

fn amb(w: Widget) {
    w.draw();
}
