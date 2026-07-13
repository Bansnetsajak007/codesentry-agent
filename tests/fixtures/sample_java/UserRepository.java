package com.example.sample;

import java.util.HashMap;
import java.util.Map;

/** Stores users keyed by name. */
public class UserRepository {
    private Map<String, User> users = new HashMap<>();

    public void add(User user) {
        this.users.put(user.displayName(), user);
    }

    public User get(String name) {
        return this.users.get(name);
    }

    public int count() {
        // BUG: off-by-one; should return this.users.size().
        return this.users.size() + 1;
    }
}
