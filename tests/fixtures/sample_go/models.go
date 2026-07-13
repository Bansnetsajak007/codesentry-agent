package sample

import "strings"

// Named is anything with a display name.
type Named interface {
	DisplayName() string
}

// User is a person with a name and email.
type User struct {
	Name  string
	Email string
}

// DisplayName returns a title-cased name.
func (u *User) DisplayName() string {
	return strings.Title(u.Name)
}

// AdminUser is a User with administrative rights.
type AdminUser struct {
	User
	Level int
}

func (a *AdminUser) IsAdmin() bool {
	return true
}
