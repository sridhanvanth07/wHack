from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from backend.app.discovery.schemas import Opportunity, SearchRequest
from .base import SourceOutcome
from .html_text import html_to_text


logger = logging.getLogger("carryflow.discovery.adzuna")
API_BASE = "https://api.adzuna.com/v1/api/jobs"
MAX_RESPONSE_BYTES = 4_000_000
MAX_RESULTS_PER_PAGE = 20


def _safe_adzuna_listing_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").casefold()
    # The India API may return an HTTP click-through. Upgrade only the fixed
    # allowlisted publisher host; never accept an arbitrary redirect target.
    if host != "adzuna.in" and not host.endswith(".adzuna.in"):
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        return None
    return urlunparse(("https", parsed.netloc, parsed.path, "", parsed.query, ""))


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0 <= number <= 100_000_000 else None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        # Adzuna returns ISO timestamps (usually UTC), while some fixtures and
        # cached feeds contain a date only. The listing date is the first 10 chars.
        return date.fromisoformat(value.strip()[:10])
    except (ValueError, TypeError):
        return None


def _eligibility_evidence(description: str | None) -> str | None:
    if not description:
        return None
    for sentence in re.split(r"(?<=[.!?])\s+", description):
        if re.search(r"\b(student|undergraduate|college|university|enrolled)\b", sentence, re.I):
            return sentence.strip()[:400]
    return None
def normalize_adzuna(raw: dict[str, Any], retrieved_at: datetime) -> Opportunity | None:
    title = raw.get("title")
    contract_type = str(raw.get("contract_type") or "")
    if not isinstance(title, str) or not re.search(r"\bintern(ship)?\b", f"{title} {contract_type}", re.I):
        return None

    listing_url = _safe_adzuna_listing_url(raw.get("redirect_url"))
    if not listing_url:
        return None
    listing_id = str(raw.get("id")) if raw.get("id") is not None else None
    identity = listing_id or listing_url
    internal_id = "adzuna-in-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    company = raw.get("company")
    location = raw.get("location")
    company_name = company.get("display_name") if isinstance(company, dict) else None
    location_name = location.get("display_name") if isinstance(location, dict) else None
    category = raw.get("category")
    category_label = category.get("label") if isinstance(category, dict) else None
    compensation_min = _as_number(raw.get("salary_min"))
    compensation_max = _as_number(raw.get("salary_max"))
    compensation_text = None
    if compensation_min is not None or compensation_max is not None:
        low = f"{compensation_min:,.0f}" if compensation_min is not None else None
        high = f"{compensation_max:,.0f}" if compensation_max is not None else None
        amount = f"{low}–{high}" if low and high and low != high else low or high
        compensation_text = f"INR {amount}" if amount else None

    description = html_to_text(raw.get("description"))
    return Opportunity(
        id=internal_id,
        title=title.strip()[:240],
        company_name=str(company_name).strip()[:200] if company_name else None,
        description=description,
        location=str(location_name).strip()[:200] if location_name else None,
        # This endpoint does not provide a documented remote/hybrid/onsite field.
        work_mode="unknown",
        eligibility_evidence=_eligibility_evidence(description),
        categories=[str(category_label).strip()[:80]] if category_label else [],
        compensation_text=compensation_text,
        compensation_min=compensation_min,
        compensation_max=compensation_max,
        compensation_currency="INR" if compensation_text else None,
        compensation_estimated=bool(raw.get("salary_is_predicted")) if raw.get("salary_is_predicted") is not None else None,
        posted_date=_as_date(raw.get("created")),
        listing_url=listing_url,
        source_name="Adzuna",
        source_listing_id=listing_id,
        retrieved_at=retrieved_at,
    )


class AdzunaAdapter:
    source_name = "Adzuna"

    def search(self, request: SearchRequest) -> SourceOutcome:
        if request.country_code is None:
            return SourceOutcome(
                source_name=self.source_name,
                status="unsupported",
                message="Adzuna search is currently configured for India only.",
            )
        app_id = os.getenv("ADZUNA_APP_ID", "").strip()
        app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
        if not app_id or not app_key:
            return SourceOutcome(
                source_name=self.source_name,
                status="not_configured",
                message="Add Adzuna credentials to the backend environment to enable this source.",
            )

        query_terms = ["internship"]
        if request.keywords:
            query_terms.append(request.keywords)
        query_terms.extend(request.skills)
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": min(request.result_count, MAX_RESULTS_PER_PAGE),
            "what": " ".join(dict.fromkeys(query_terms))[:180],
            "content-type": "application/json",
        }
        if request.location:
            params["where"] = request.location[:100]
        endpoint = f"{API_BASE}/in/search/1?{urlencode(params)}"
        http_request = Request(endpoint, headers={"Accept": "application/json", "User-Agent": "CarryFlow internship discovery"})
        try:
            with urlopen(http_request, timeout=8) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded size limit")
            payload = json.loads(body.decode("utf-8"))
            records = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise ValueError("missing results array")
        except HTTPError as exc:
            if exc.code == 429:
                return SourceOutcome(self.source_name, "rate_limited", message="Adzuna temporarily rate-limited this search.")
            logger.warning("Adzuna request failed with HTTP %s", exc.code)
            return SourceOutcome(self.source_name, "failed", message="Adzuna could not complete this search.")
        except (URLError, TimeoutError, OSError):
            logger.warning("Adzuna request timed out or could not connect")
            return SourceOutcome(self.source_name, "failed", message="Adzuna could not be reached right now.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Adzuna returned an invalid or oversized response")
            return SourceOutcome(self.source_name, "failed", message="Adzuna returned an unreadable response.")

        retrieved_at = datetime.now(timezone.utc)
        opportunities: list[Opportunity] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                item = normalize_adzuna(record, retrieved_at)
            except Exception:
                item = None
            if item:
                opportunities.append(item)
        return SourceOutcome(source_name=self.source_name, status="success", opportunities=opportunities)
