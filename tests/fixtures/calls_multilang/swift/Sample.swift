class Svc: Base {
    var repo: Repo

    func run(r: Repo) {
        step()
        self.step()
        repo.save()
        Svc.make()
        let x: Foo = Foo()
        x.m()
        let y = Bar()
        y.n()
        r.save()
        free()
        inheritedOp()
    }

    func step() {
    }

    static func make() {
    }
}

extension Svc {
    func ext() {
        step()
    }
}

func amb(p: Panel) {
    p.draw()
}
