package main

import (
	"fmt"
	helpers "example.com/proj/go/util"
)

type Svc struct {
	repo *Repo
}

func (s *Svc) Run(r *Repo) {
	s.Step()
	s.repo.Save()
	helpers.Help()
	var x Foo
	x.M()
	y := &Foo{}
	y.M()
	local()
	shared()
	fmt.Println("done")
	r.Save()
	var w Worker
	w.Do()
}

func (s *Svc) Step() {
}

func local() {
}
