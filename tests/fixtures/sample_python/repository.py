"""In-memory user repository."""

from models import User


class UserRepository:
    """Stores users keyed by name."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, user: User) -> None:
        self._users[user.name] = user

    def get(self, name: str) -> User:
        return self._users[name]

    def count(self) -> int:
        # BUG: off-by-one; should return len(self._users).
        return len(self._users) + 1
