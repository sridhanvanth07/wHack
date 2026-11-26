from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.discovery.schemas import Opportunity, SearchRequest
from .base import SourceOutcome
from .html_text import html_to_text


logger = logging.getLogger("carryflow.discovery.remotive")
API_URL = "https://remotive.com/api/remote-jobs?limit=1000"
CACHE_TTL_SECONDS = 8 * 60 * 60  # Remotive advises at most four fetches per day.
MIN_REQUEST_INTERVAL_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000
_lock = threading.Lock()
_cache: tuple[float, datetime, list[Opportunity]] | None = None
_last_request_at: float | None = None


def reset_cache_for_tests() -> None:
    global _cache, _last_request_at
    with _lock:
        _cache = None
        _last_request_at = None


def _safe_listing_url(value: Any) -> str | None:
    from urllib.parse import urlparse

    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (host == "remotive.com" or host.endswith(".remotive.com")):
        return None
    if parsed.username or parsed.password:
        return None
    return parsed.geturl()


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _skills(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip()[:80] for item in raw if isinstance(item, str) and item.strip()][:30]


def _salary_range(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    matches = re.findall(r"(?<![\w])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k)?\b", value, re.I)
    amounts: list[float] = []
    for number, thousands in matches[:2]:
        try:
            amount = float(number.replace(",", "")) * (1000 if thousands else 1)
            if amount <= 100_000_000:
                amounts.append(amount)
        except ValueError:
            continue
    return (amounts[0], amounts[1]) if len(amounts) > 1 else ((amounts[0], amounts[0]) if amounts else (None, None))


def _eligibility_evidence(description: str | None) -> str | None:
    if not description:
        return None
    for sentence in re.split(r"(?<=[.!?])\s+", description):
        if re.search(r"\b(student|undergraduate|college|university|enrolled)\b", sentence, re.I):
            return sentence.strip()[:400]
    return None


def normalize_remotive(raw: dict[str, Any], retrieved_at: datetime) -> Opportunity | None:
    title = raw.get("title")
    listing_url = _safe_listing_url(raw.get("url"))
    job_type = str(raw.get("job_type") or "")
    # Return only roles explicitly identified as internships by the source.
    if not isinstance(title, str) or not re.search(r"\bintern(ship)?\b", f"{title} {job_type}", re.I) or not listing_url:
        return None

    listing_id = str(raw.get("id")) if raw.get("id") is not None else None
    identity = listing_id or listing_url
    internal_id = "remotive-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    salary = raw.get("salary")
    salary_text = str(salary).strip()[:160] if salary else None
    compensation_min, compensation_max = _salary_range(salary_text)
    currency = None
    if salary_text:
        if "₹" in salary_text or re.search(r"\bINR\b|rupees?", salary_text, re.I):
            currency = "INR"
        elif "$" in salary_text or re.search(r"\bUSD\b", salary_text, re.I):
            currency = "USD"
        elif "€" in salary_text or re.search(r"\bEUR\b", salary_text, re.I):
            currency = "EUR"
        elif "£" in salary_text or re.search(r"\bGBP\b", salary_text, re.I):
            currency = "GBP"

    description = html_to_text(raw.get("description"))
    return Opportunity(
        id=internal_id,
        title=title.strip()[:240],
        company_name=(str(raw.get("company_name")).strip()[:200] or None) if raw.get("company_name") else None,
        description=description,
        location=(str(raw.get("candidate_required_location")).strip()[:200] or None) if raw.get("candidate_required_location") else None,
        work_mode="remote",
        eligibility_evidence=_eligibility_evidence(description),
        skills=_skills(raw.get("tags")),
        categories=[str(raw.get("category")).strip()[:80]] if raw.get("category") else [],
        compensation_text=salary_text,
        compensation_min=compensation_min,
        compensation_max=compensation_max,
        compensation_currency=currency,
        posted_date=_date(raw.get("publication_date")),
        listing_url=listing_url,
        source_name="Remotive",
        source_listing_id=listing_id,
        retrieved_at=retrieved_at,
    )


class RemotiveAdapter:
    source_name = "Remotive"

    def search(self, request: SearchRequest) -> SourceOutcome:
        if request.remote_preference in {"hybrid", "onsite"}:
            return SourceOutcome(
                source_name=self.source_name,
                status="unsupported",
                message="Remotive only provides remote listings; it cannot verify this work-mode preference.",
            )

        opportunities = self._get_snapshot()
        if isinstance(opportunities, SourceOutcome):
            return opportunities
        return SourceOutcome(source_name=self.source_name, status="success", opportunities=opportunities)

    def _get_snapshot(self) -> list[Opportunity] | SourceOutcome:
        global _cache, _last_request_at
        now_mono = time.monotonic()
        with _lock:
            if _cache and now_mono - _cache[0] < CACHE_TTL_SECONDS:
                return [item.model_copy(deep=True) for item in _cache[2]]
            if _last_request_at is not None and now_mono - _last_request_at < MIN_REQUEST_INTERVAL_SECONDS:
                return SourceOutcome(
                    source_name=self.source_name,
                    status="rate_limited",
                    message="The public feed was fetched recently; please retry later.",
                )
            _last_request_at = now_mono
            request = Request(API_URL, headers={"Accept": "application/json", "User-Agent": "CarryFlow internship discovery"})
            try:
                with urlopen(request, timeout=8) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeded size limit")
                payload = json.loads(body.decode("utf-8"))
                records = payload.get("jobs") if isinstance(payload, dict) else None
                if not isinstance(records, list):
                    raise ValueError("missing jobs array")
            except HTTPError as exc:
                if exc.code == 429:
                    return SourceOutcome(self.source_name, "rate_limited", message="Remotive temporarily rate-limited this search.")
                logger.warning("Remotive request failed with HTTP %s", exc.code)
                return SourceOutcome(self.source_name, "failed", message="Remotive could not be reached right now.")
            except (URLError, TimeoutError, OSError):
                logger.warning("Remotive request timed out or could not connect")
                return SourceOutcome(self.source_name, "failed", message="Remotive could not be reached right now.")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                logger.warning("Remotive returned an invalid or oversized response")
                return SourceOutcome(self.source_name, "failed", message="Remotive returned an unreadable response.")

            retrieved_at = datetime.now(timezone.utc)
            normalized: list[Opportunity] = []
            malformed = 0
            for record in records:
                if not isinstance(record, dict):
                    malformed += 1
                    continue
                try:
                    item = normalize_remotive(record, retrieved_at)
                except Exception:
                    item = None
                if item is None:
                    # Non-internship listings are expected in the general feed.
                    continue
                normalized.append(item)
            if not records:
                logger.info("Remotive feed returned no listings")
            if malformed:
                logger.warning("Remotive feed contained %s malformed records", malformed)
            _cache = (time.monotonic(), retrieved_at, normalized)
            return [item.model_copy(deep=True) for item in normalized]
