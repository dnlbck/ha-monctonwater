"""Exceptions for the Moncton Water integration."""

from __future__ import annotations


class MonctonWaterError(Exception):
    """Base exception for Moncton Water errors."""


class MonctonWaterAuthError(MonctonWaterError):
    """Raised when the portal rejects the credentials."""


class MonctonWaterSessionError(MonctonWaterError):
    """Raised when the portal session has expired or gone stale.

    Distinct from MonctonWaterAuthError: a fresh login fixes it, so it
    must never surface as a re-authentication request.
    """


class MonctonWaterApiError(MonctonWaterError):
    """Raised when the portal returns an unexpected response."""
