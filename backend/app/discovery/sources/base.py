from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from backend.app.discovery.schemas import Opportunity, SearchRequest


@dataclass
class SourceOutcome:
    source_name: str
    status: Literal["success", "failed", "not_configured", "unsupported", "rate_limited"]
    opportunities: list[Opportunity] = field(default_factory=list)
    message: str = ""


class SourceAdapter(Protocol):
    source_name: str

    def search(self, request: SearchRequest) -> SourceOutcome:
        """Search one documented provider without following user-supplied URLs."""
        ...
