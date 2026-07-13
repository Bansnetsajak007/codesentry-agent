"""Small string helpers."""


def slugify(value: str) -> str:
    """Turn a display string into a URL-friendly slug."""
    return _normalize(value).replace(" ", "-")


def _normalize(value: str) -> str:
    return value.strip().lower()
