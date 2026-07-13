"""Python backend exposing a user API. The endpoint name is shared with the
TypeScript frontend by convention (no import crosses the language boundary)."""


def getUser(user_id: str) -> dict[str, str]:
    """Return a user record for the given id."""
    return {"id": user_id}
