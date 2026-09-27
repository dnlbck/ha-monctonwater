"""Exceptions for the Moncton Water integration."""

from __future__ import annotations


class MonctonWaterError(Exception):
    """Base exception for Moncton Water errors."""


class MonctonWaterAuthError(MonctonWaterError):
    """Raised when logging in to the portal fails or the session expires."""


class MonctonWaterApiError(MonctonWaterError):
    """Raised when the portal returns an unexpected response."""
