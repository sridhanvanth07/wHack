"""Supported internship source adapters."""

from .adzuna import AdzunaAdapter
from .base import SourceAdapter, SourceOutcome
from .remotive import RemotiveAdapter

__all__ = ["AdzunaAdapter", "RemotiveAdapter", "SourceAdapter", "SourceOutcome"]
