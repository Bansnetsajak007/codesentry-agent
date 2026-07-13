/** Domain models for the sample app. */

export class User {
  constructor(name, email) {
    this.name = name;
    this.email = email;
  }

  /** Return a friendly, title-cased name. */
  displayName() {
    return this.name;
  }
}

export class AdminUser extends User {
  isAdmin() {
    return true;
  }
}
