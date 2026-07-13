package com.example.sample;

/** A user with administrative rights. */
public class AdminUser extends User {
    public AdminUser(String name, String email) {
        super(name, email);
    }

    public boolean isAdmin() {
        return true;
    }
}
