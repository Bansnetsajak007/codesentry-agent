"""Domain models for the sample app."""


class User:
    """A user account."""

    def __init__(self, name: str, email: str) -> None:
        self.name = name
        self.email = email

    def display_name(self) -> str:
        """Return a friendly, title-cased name."""
        return self.name.title()


class AdminUser(User):
    """A user with administrative rights."""

    def is_admin(self) -> bool:
        return True
