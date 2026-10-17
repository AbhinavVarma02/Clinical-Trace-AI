"""Domain exceptions shared by services and mapped to HTTP responses in ``api.main``."""

from __future__ import annotations


class NotFoundError(LookupError):
    """A requested resource does not exist (or is not visible to the caller)."""
