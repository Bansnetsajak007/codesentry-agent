import { User } from "./models.js";

export class UserRepository {
  constructor() {
    this.users = new Map();
  }

  add(user) {
    this.users.set(user.name, user);
  }

  get(name) {
    return this.users.get(name);
  }

  count() {
    // BUG: off-by-one; should return this.users.size.
    return this.users.size + 1;
  }
}
