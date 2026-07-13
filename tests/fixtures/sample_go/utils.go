package sample

import "strings"

// Slugify turns a display string into a slug.
func Slugify(value string) string {
	return strings.ReplaceAll(normalize(value), " ", "-")
}

func normalize(value string) string {
	return strings.ToLower(strings.TrimSpace(value))
}
