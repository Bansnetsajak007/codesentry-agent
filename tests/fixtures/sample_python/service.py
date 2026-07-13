"""User service layer coordinating models and the repository."""

from functools import lru_cache

from models import User
from repository import UserRepository


class UserService:
    """Coordinates user registration and lookups."""

    def __init__(self) -> None:
        self.repo = UserRepository()

    def register(self, name: str, email: str) -> User:
        user = User(name, email)
        self.repo.add(user)
        return user

    def headcount(self) -> int:
        return self.repo.count()


@lru_cache
def make_default_service() -> UserService:
    """Build a cached default service instance."""
    return UserService()
