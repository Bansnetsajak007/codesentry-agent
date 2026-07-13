import { User } from "./models.js";
import { UserRepository } from "./repository.js";

export class UserService {
  constructor() {
    this.repo = new UserRepository();
  }

  register(name, email) {
    const user = new User(name, email);
    this.repo.add(user);
    return user;
  }

  headcount() {
    return this.repo.count();
  }
}

/** Build a default service instance. */
export const makeDefaultService = () => new UserService();
