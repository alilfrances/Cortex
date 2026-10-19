package main

type Repo struct{}

type Foo struct{}

func (r *Repo) Save() {
}

func (f Foo) M() {
}

func shared() {
}
