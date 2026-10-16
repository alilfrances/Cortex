require_relative "util"

class Svc < Base
  include Mixin

  def initialize
    @repo = Repo.new
  end

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
    inherited_op
    mixed
    r.other
  end

  def step
  end
end
