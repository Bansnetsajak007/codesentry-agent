package com.example.sample;

/** A user with a name and email. */
public class User implements Named {
    private String name;
    private String email;

    public User(String name, String email) {
        this.name = name;
        this.email = email;
    }

    /** Return a title-cased name. */
    @Override
    public String displayName() {
        return normalize(this.name);
    }

    private String normalize(String value) {
        return value.trim();
    }
}
