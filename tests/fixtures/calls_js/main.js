import { Worker } from "./lib/worker.js";

export function boot() {
  const w = new Worker();
  w.run();
  w.missing?.();
}
