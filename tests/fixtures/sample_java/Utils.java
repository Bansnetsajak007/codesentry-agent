package com.example.sample;

/** Small string helpers. */
public class Utils {
    public static String slugify(String value) {
        return normalize(value).replace(" ", "-");
    }

    private static String normalize(String value) {
        return value.trim().toLowerCase();
    }
}
