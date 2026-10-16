import { helper } from "./lib/helpers";
import * as utils from "./lib/utils";
import Service from "./lib/service";
import { Repo } from "./lib/repo";
import { Panel } from "./lib/panel_a";
import { useState } from "react";
const legacy = require("./lib/legacy");

export class Controller {
  private repo: Repo;

  constructor(private svc: Service) {}

  start(p: Panel) {
    helper();
    utils.helper();
    this.stop();
    this.repo.save();
    this.svc.run();
    const s = new Service();
    s.run();
    new Service().run();
    p.render();
    legacy.go();
    render();
    useState(0);
    p?.render();
    this.cache?.get();
    foo.bar.baz();
  }

  stop() {}
}

export function unimported(q: Panel, r: Repo) {
  q.render();
  r.save();
}
