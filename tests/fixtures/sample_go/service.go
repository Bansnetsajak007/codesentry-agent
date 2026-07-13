package sample

// UserService coordinates user operations.
type UserService struct {
	repo *UserRepository
}

func (s *UserService) Register(name, email string) *User {
	u := &User{Name: name, Email: email}
	s.repo.Add(u)
	return u
}

func (s *UserService) Headcount() int {
	return s.repo.Count()
}

// MakeDefaultService builds a service with a fresh repository.
func MakeDefaultService() *UserService {
	return &UserService{repo: NewUserRepository()}
}
