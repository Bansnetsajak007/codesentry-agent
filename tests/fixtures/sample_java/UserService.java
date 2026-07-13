package com.example.sample;

/** Coordinates user operations. */
public class UserService {
    private UserRepository repo = new UserRepository();

    public User register(String name, String email) {
        User user = new User(name, email);
        this.repo.add(user);
        return user;
    }

    public int headcount() {
        return this.repo.count();
    }

    /** Aggregated counts for a service. */
    static class Stats {
        private int total;

        public int total() {
            return this.total;
        }
    }
}
