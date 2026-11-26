from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .schemas import Opportunity, SourceAttribution


def _normal_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _normal_url(value: str) -> str:
    parsed = urlparse(value)
    query = urlencode([(key, val) for key, val in parse_qsl(parsed.query) if not key.casefold().startswith("utm_")])
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), "", query, ""))


def _identity(item: Opportunity) -> tuple[str, ...]:
    company = _normal_text(item.company_name)
    title = _normal_text(item.title)
    location = _normal_text(item.location)
    return ("company-title-location", company, title, location) if company and title and location else ("url", _normal_url(item.listing_url))


def deduplicate(items: list[Opportunity]) -> list[Opportunity]:
    by_identity: dict[tuple[str, ...], Opportunity] = {}
    for candidate in items:
        key = _identity(candidate)
        existing = by_identity.get(key)
        if existing is None:
            by_identity[key] = candidate.model_copy(deep=True)
            continue

        references = {(ref.source_name, ref.listing_url) for ref in existing.source_attributions}
        for ref in candidate.source_attributions:
            if (ref.source_name, ref.listing_url) not in references:
                existing.source_attributions.append(ref.model_copy(deep=True))
        existing.match_reasons = list(dict.fromkeys(existing.match_reasons + candidate.match_reasons))
        if not existing.description and candidate.description:
            existing.description = candidate.description
        if not existing.compensation_text and candidate.compensation_text:
            existing.compensation_text = candidate.compensation_text
            existing.compensation_min = candidate.compensation_min
            existing.compensation_max = candidate.compensation_max
            existing.compensation_currency = candidate.compensation_currency
            existing.compensation_estimated = candidate.compensation_estimated
    return list(by_identity.values())
