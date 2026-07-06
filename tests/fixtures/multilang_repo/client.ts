import { start } from './app';

export class Client {
  connect(): string {
    return start();
  }
}
