import { helper } from './helper.js';

export function start() {
  return helper();
}

class AppController {
  run() {
    return start();
  }
}
