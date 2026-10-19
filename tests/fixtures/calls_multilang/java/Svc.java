import com.x.Util;

public class Svc extends Base {
    private Repo repo;

    public void run(Repo r) {
        step();
        this.step();
        repo.save();
        Util.help();
        Svc.make();
        Foo x = new Foo();
        x.m();
        var y = new Bar();
        y.n();
        r.save();
        new Baz().q();
        inheritedOp();
        super.inheritedOp();
    }

    public void step() {
    }

    public static void make() {
    }
}
