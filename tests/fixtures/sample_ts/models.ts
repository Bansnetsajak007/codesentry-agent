/** Domain models for the sample app. */

export type UserId = string;

export interface Named {
  name: string;
  displayName(): string;
}

@sealed
export class User implements Named {
  constructor(public name: string, public email: string) {}

  displayName(): string {
    return this.name;
  }
}

export class AdminUser extends User implements Named {
  isAdmin(): boolean {
    return true;
  }
}

export class Box<T> {
  constructor(private value: T) {}
}
