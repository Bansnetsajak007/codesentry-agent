import { User } from "./models";
import { UserRepository } from "./repository";

export class UserService {
  private repo = new UserRepository();

  register(name: string, email: string): User {
    const user = new User(name, email);
    this.repo.add(user);
    return user;
  }

  headcount(): number {
    return this.repo.count();
  }
}

/** Build a default service instance. */
export const makeDefaultService = (): UserService => new UserService();
