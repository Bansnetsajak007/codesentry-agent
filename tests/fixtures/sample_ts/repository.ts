import type { User } from "./models";

export class UserRepository {
  private users: Map<string, User> = new Map();

  add(user: User): void {
    this.users.set(user.name, user);
  }

  get(name: string): User | undefined {
    return this.users.get(name);
  }

  count(): number {
    // BUG: off-by-one; should return this.users.size.
    return this.users.size + 1;
  }
}
