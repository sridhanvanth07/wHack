from __future__ import annotations

from datetime import date, datetime, timezone

from .deduplication import deduplicate
from .filtering import classify, ranking
from .schemas import DiscoveryResponse, SearchRequest, SourceStatus
from .sources import AdzunaAdapter, RemotiveAdapter, SourceAdapter
from .sources.base import SourceOutcome


def _query_summary(request: SearchRequest) -> str:
    parts = [request.keywords, request.location, request.remote_preference if request.remote_preference != "any" else None]
    if request.paid_preference != "any":
        parts.append(request.paid_preference)
    return ", ".join(part for part in parts if part) or "internships"


class DiscoveryOrchestrator:
    def __init__(self, adapters: list[SourceAdapter] | None = None):
        self.adapters = adapters or [RemotiveAdapter(), AdzunaAdapter()]

    def search(self, request: SearchRequest) -> DiscoveryResponse:
        collected = []
        statuses: list[SourceStatus] = []
        today = date.today()

        for adapter in self.adapters:
            try:
                outcome: SourceOutcome = adapter.search(request)
            except Exception:
                # A provider failure must not prevent other configured sources from returning results.
                outcome = SourceOutcome(adapter.source_name, "failed", message=f"{adapter.source_name} could not complete this search.")

            if outcome.status != "success":
                statuses.append(SourceStatus(
                    source_name=outcome.source_name,
                    status=outcome.status,
                    result_count=0,
                    message=outcome.message or f"{outcome.source_name} did not return results.",
                ))
                continue

            classified = [(item, classify(item, request, today)) for item in outcome.opportunities]
            matches_for_source = [(item, category) for item, category in classified if category is not None]
            if not matches_for_source:
                statuses.append(SourceStatus(
                    source_name=outcome.source_name,
                    status="filtered_out" if outcome.opportunities else "no_results",
                    result_count=len(outcome.opportunities),
                    message=(f"{outcome.source_name} returned {len(outcome.opportunities)} candidate listing(s), but no listings matched the requested role or known hard constraints."
                             if outcome.opportunities else f"{outcome.source_name} was searched and returned no listings; no listings matched the search."),
                ))
                continue

            for item, category in matches_for_source:
                score, reasons = ranking(item, request, today)
                item.match_reasons = reasons
                item.match_category = category
                collected.append((score, item.posted_date or date.min, item, category))
            statuses.append(SourceStatus(
                source_name=outcome.source_name,
                status="success",
                result_count=len(matches_for_source),
                message=(f"{outcome.source_name} returned {sum(c == 'verified_match' for _, c in matches_for_source)} verified match(es), "
                         f"{sum(c == 'potential_match' for _, c in matches_for_source)} potential match(es), and "
                         f"{sum(c == 'insufficient_information' for _, c in matches_for_source)} listing(s) with insufficient information."),
            ))

        merged = deduplicate([item for _, _, item, _ in collected])
        scores = {item.id: (score, posted, category) for score, posted, item, category in collected}
        priority = {"verified_match": 2, "potential_match": 1, "insufficient_information": 0}
        merged.sort(key=lambda item: (priority.get(scores.get(item.id, (0, date.min, "insufficient_information"))[2], 0), scores.get(item.id, (0, date.min, "insufficient_information"))[0], scores.get(item.id, (0, date.min, "insufficient_information"))[1], item.title.casefold()), reverse=True)
        opportunities = merged[:request.result_count]
        summary = self._summary(request, opportunities, statuses)
        return DiscoveryResponse(
            opportunities=opportunities,
            sources=statuses,
            retrieved_at=datetime.now(timezone.utc),
            query_summary=_query_summary(request),
            assistant_summary=summary,
        )

    @staticmethod
    def _summary(request: SearchRequest, opportunities, statuses: list[SourceStatus]) -> str:
        active = [status.source_name for status in statuses if status.status == "success"]
        no_results = [status.source_name for status in statuses if status.status == "no_results"]
        unavailable = [status for status in statuses if status.status in {"failed", "not_configured", "unsupported", "rate_limited"}]

        if opportunities:
            verified = sum(item.match_category == "verified_match" for item in opportunities)
            potential = sum(item.match_category == "potential_match" for item in opportunities)
            incomplete = sum(item.match_category == "insufficient_information" for item in opportunities)
            text = f"I found {len(opportunities)} relevant internship listing(s): {verified} verified, {potential} potential, and {incomplete} with insufficient information"
            if active:
                text += " from " + " and ".join(active)
            text += ". Open a source listing to review the original details."
            if potential or incomplete:
                text += " Unknown compensation, eligibility, location, or work mode is identified in each result; check the original listing before relying on it."
        else:
            text = "I didn’t find matching internship listings in the supported sources."
            if no_results:
                text += " " + " ".join(f"{source} returned no matches." for source in no_results)
        if unavailable:
            text += " " + " ".join(status.message for status in unavailable)
        return text


def discover(request: SearchRequest) -> DiscoveryResponse:
    return DiscoveryOrchestrator().search(request)
