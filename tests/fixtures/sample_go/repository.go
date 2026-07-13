package sample

// UserRepository stores users keyed by name.
type UserRepository struct {
	users map[string]*User
}

// NewUserRepository builds an empty repository.
func NewUserRepository() *UserRepository {
	return &UserRepository{users: make(map[string]*User)}
}

func (r *UserRepository) Add(u *User) {
	r.users[u.Name] = u
}

func (r *UserRepository) Get(name string) *User {
	return r.users[name]
}

// Count returns the number of stored users.
func (r *UserRepository) Count() int {
	// BUG: off-by-one; should return len(r.users).
	return len(r.users) + 1
}
