require_relative "util"

class Svc < Base
  def run(r)
    step
    self.step
    @repo.save
    Util.help
    Util::Inner.go
    x = Foo.new
    x.m
    helper(1)
    Widget.new
    r.p
  end
end
