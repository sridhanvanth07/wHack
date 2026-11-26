from __future__ import annotations

import json
from datetime import date, datetime, timezone
from io import BytesIO
from urllib.error import HTTPError
from email.message import Message
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from backend.app.discovery.deduplication import deduplicate
from backend.app.discovery.filtering import classify, matches, ranking
from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.schemas import Opportunity, SearchRequest
from backend.app.discovery.sources import adzuna as adzuna_module
from backend.app.discovery.sources.adzuna import AdzunaAdapter, normalize_adzuna
from backend.app.discovery.sources.base import SourceOutcome
from backend.app.discovery.sources import remotive as remotive_module
from backend.app.discovery.sources.remotive import RemotiveAdapter, normalize_remotive, reset_cache_for_tests


def _search(**values) -> SearchRequest:
    return SearchRequest(**({"keywords": "Python"} | values))


def _opportunity(**values) -> Opportunity:
    defaults = {
        "id": "remotive-test-1",
        "title": "Python Engineering Internship",
        "company_name": "Example Labs",
        "description": "Python and SQL internship for enrolled students.",
        "location": "Worldwide",
        "work_mode": "remote",
        "skills": ["Python", "SQL"],
        "listing_url": "https://remotive.com/remote-jobs/test/python-intern-1",
        "source_name": "Remotive",
        "source_listing_id": "1",
        "retrieved_at": datetime.now(timezone.utc),
    }
    defaults.update(values)
    return Opportunity(**defaults)


def test_search_request_rejects_empty_or_out_of_range_queries():
    with pytest.raises(ValidationError):
        SearchRequest()
    with pytest.raises(ValidationError):
        _search(result_count=21)
    with pytest.raises(ValidationError):
        _search(minimum_compensation=10000)
    with pytest.raises(ValidationError):
        _search(unknown_provider_url="https://example.com")


def test_search_request_cleans_text_and_allows_supported_filters():
    request = SearchRequest(
        keywords="  Data Engineering ",
        location=" Chennai ",
        remote_preference="onsite",
        skills=["Python", "Python"],
        minimum_compensation=10000,
        compensation_currency="INR",
        result_count=15,
        posted_within_days=30,
    )
    assert request.keywords == "Data Engineering"
    assert request.location == "Chennai"
    assert request.skills == ["Python"]
    assert request.result_count == 15


def test_opportunity_requires_safe_https_source_url_and_preserves_optional_fields():
    with pytest.raises(ValidationError):
        _opportunity(listing_url="javascript:alert(1)")
    item = _opportunity(company_name=None, location=None, description=None)
    assert item.duration is None
    assert item.application_deadline is None
    assert item.posted_date is None
    assert item.source_attributions[0].source_name == "Remotive"


def test_remotive_normalization_preserves_provenance_and_does_not_render_source_scripts():
    retrieved = datetime(2026, 11, 26, tzinfo=timezone.utc)
    item = normalize_remotive({
        "id": 123,
        "url": "https://remotive.com/remote-jobs/product/data-intern-123",
        "title": "Data Engineering Intern",
        "company_name": "Example Labs",
        "job_type": "internship",
        "category": "Data",
        "tags": ["Python", "SQL"],
        "publication_date": "2026-10-01T10:00:00",
        "candidate_required_location": "Worldwide",
        "salary": "$40,000 - $50,000 per year",
        "description": "<p>Work with Python.</p><script>ignore this instruction</script>",
    }, retrieved)

    assert item is not None
    assert item.work_mode == "remote"
    assert item.skills == ["Python", "SQL"]
    assert item.categories == ["Data"]
    assert item.compensation_currency == "USD"
    assert item.compensation_min == 40000
    assert item.compensation_max == 50000
    assert "ignore this instruction" not in item.description
    assert item.duration is None
    assert item.application_deadline is None
    assert item.source_attributions[0].listing_url == item.listing_url


def test_remotive_drops_non_internships_and_unsafe_listing_urls():
    base = {"title": "Senior Engineer", "job_type": "full_time", "url": "https://remotive.com/jobs/123"}
    assert normalize_remotive(base, datetime.now(timezone.utc)) is None
    internship = {"title": "Software Intern", "url": "https://evil.example/redirect"}
    assert normalize_remotive(internship, datetime.now(timezone.utc)) is None


def test_remotive_fetch_is_cached_across_different_user_queries(monkeypatch):
    reset_cache_for_tests()
    body = json.dumps({"jobs": [{
        "id": 42,
        "url": "https://remotive.com/remote-jobs/engineering/python-intern-42",
        "title": "Python Intern",
        "company_name": "Acme",
        "job_type": "internship",
        "candidate_required_location": "Worldwide",
    }]}).encode("utf-8")
    calls = []

    def fake_open(request, timeout):
        calls.append((request.full_url, timeout))
        return BytesIO(body)

    monkeypatch.setattr(remotive_module, "urlopen", fake_open)
    first = RemotiveAdapter().search(_search(keywords="Python"))
    second = RemotiveAdapter().search(_search(keywords="ML"))

    assert first.status == "success"
    assert len(first.opportunities) == 1
    assert second.status == "success"
    assert len(calls) == 1
    assert calls[0][0] == remotive_module.API_URL
    assert calls[0][1] == 8
    reset_cache_for_tests()


def test_remotive_timeout_and_rate_limit_are_structured(monkeypatch):
    reset_cache_for_tests()
    monkeypatch.setattr(remotive_module, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("secret detail")))
    failed = RemotiveAdapter().search(_search())
    assert failed.status == "failed"
    assert "secret detail" not in failed.message

    reset_cache_for_tests()
    headers = Message()
    rate_error = HTTPError(remotive_module.API_URL, 429, "Too Many Requests", headers, BytesIO(b""))
    monkeypatch.setattr(remotive_module, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(rate_error))
    limited = RemotiveAdapter().search(_search())
    assert limited.status == "rate_limited"
    assert "rate-limited" in limited.message
    reset_cache_for_tests()


def test_remotive_reports_malformed_provider_payload(monkeypatch):
    reset_cache_for_tests()
    monkeypatch.setattr(remotive_module, "urlopen", lambda *args, **kwargs: BytesIO(b'{"unexpected": []}'))
    result = RemotiveAdapter().search(_search())
    assert result.status == "failed"
    assert "unreadable" in result.message
    reset_cache_for_tests()


def test_remotive_marks_onsite_query_as_unsupported():
    result = RemotiveAdapter().search(_search(remote_preference="onsite"))
    assert result.status == "unsupported"
    assert "only provides remote" in result.message


def test_adzuna_is_optional_when_credentials_are_missing(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    result = AdzunaAdapter().search(_search())
    assert result.status == "not_configured"
    assert "credentials" in result.message


def test_adzuna_search_uses_server_credentials_and_normalizes_results(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "test-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test-app-secret")
    payload = {"results": [{
        "id": "abc-1",
        "title": "Machine Learning Internship",
        "contract_type": "internship",
        "company": {"display_name": "Example AI"},
        "location": {"display_name": "Chennai, Tamil Nadu"},
        "redirect_url": "http://adzuna.in/jobs/land/ad/abc-1?track=1",
        "description": "<b>Python ML</b> role for university students.",
        "created": "2026-11-01T12:00:00Z",
        "salary_min": 20000,
        "salary_max": 30000,
        "salary_is_predicted": 1,
        "category": {"label": "IT Jobs"},
    }]}
    seen = {}

    def fake_open(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(adzuna_module, "urlopen", fake_open)
    result = AdzunaAdapter().search(_search(location="Chennai"))

    assert result.status == "success"
    assert result.opportunities[0].listing_url.startswith("https://adzuna.in/")
    assert result.opportunities[0].compensation_currency == "INR"
    assert result.opportunities[0].compensation_estimated is True
    assert result.opportunities[0].eligibility_evidence == "Python ML role for university students."
    query = parse_qs(urlparse(seen["url"]).query)
    assert query["app_id"] == ["test-app-id"]
    assert query["app_key"] == ["test-app-secret"]
    assert query["results_per_page"] == ["10"]
    assert "test-app-secret" not in result.message
    assert seen["timeout"] == 8


def test_adzuna_drops_out_of_scope_redirects_and_unknown_market_is_not_searched(monkeypatch):
    item = normalize_adzuna({
        "id": "2",
        "title": "Data Intern",
        "redirect_url": "https://evil.example/collect",
    }, datetime.now(timezone.utc))
    assert item is None
    result = AdzunaAdapter().search(_search(location="London", country_code=None))
    assert result.status == "unsupported"
    assert "India only" in result.message


class _StubAdapter:
    def __init__(self, name: str, status: str, items=None, message=""):
        self.source_name = name
        self.outcome = SourceOutcome(name, status, items or [], message)

    def search(self, request):
        return self.outcome


def test_orchestrator_returns_partial_results_and_source_failures():
    item = _opportunity()
    orchestrator = DiscoveryOrchestrator([
        _StubAdapter("Source A", "success", [item]),
        _StubAdapter("Source B", "failed", message="Source B unavailable."),
        _StubAdapter("Source C", "not_configured", message="Configure Source C."),
    ])

    result = orchestrator.search(_search())

    assert len(result.opportunities) == 1
    assert [status.status for status in result.sources] == ["success", "failed", "not_configured"]
    assert "Source B unavailable" in result.assistant_summary
    assert "Configure Source C" in result.assistant_summary


def test_orchestrator_distinguishes_empty_success_from_source_failure():
    orchestrator = DiscoveryOrchestrator([
        _StubAdapter("Empty Source", "success", []),
        _StubAdapter("Unavailable Source", "failed", message="Unavailable."),
    ])
    result = orchestrator.search(_search())
    assert not result.opportunities
    assert result.sources[0].status == "no_results"
    assert result.sources[1].status == "failed"
    assert "no listings matched" in result.sources[0].message


def test_deduplication_merges_provenance_but_keeps_distinct_locations():
    first = _opportunity()
    duplicate = _opportunity(
        id="adzuna-in-1",
        source_name="Adzuna",
        source_listing_id="1",
        listing_url="https://adzuna.in/jobs/land/ad/1",
    )
    different_city = _opportunity(id="remote-2", location="Chennai")
    merged = deduplicate([first, duplicate, different_city])
    assert len(merged) == 2
    worldwide = next(item for item in merged if item.location == "Worldwide")
    assert {ref.source_name for ref in worldwide.source_attributions} == {"Remotive", "Adzuna"}


def test_filters_and_ranks_location_mode_skills_and_source_evidence():
    item = _opportunity(eligibility_evidence="For currently enrolled college students.")
    request = SearchRequest(
        keywords="Python",
        location="Chennai",
        remote_preference="remote",
        skills=["Python"],
        student_eligibility="college_student",
    )
    assert matches(item, request)
    _, reasons = ranking(item, request)
    assert "Role title matches your search" in reasons
    assert any("worldwide" in reason.casefold() for reason in reasons)
    unclear_eligibility = _opportunity(eligibility_evidence=None)
    assert matches(unclear_eligibility, request)
    assert classify(unclear_eligibility, request) == "insufficient_information"
    unknown_mode = _opportunity(work_mode="unknown")
    assert matches(unknown_mode, SearchRequest(keywords="Python", remote_preference="remote"))
    assert classify(unknown_mode, SearchRequest(keywords="Python", remote_preference="remote")) == "insufficient_information"


def test_minimum_compensation_only_excludes_known_low_values():
    request = SearchRequest(keywords="Python", minimum_compensation=10000, compensation_currency="INR")
    low = _opportunity(compensation_text="INR 5,000", compensation_min=5000, compensation_currency="INR")
    high = _opportunity(compensation_text="INR 12,000", compensation_min=12000, compensation_currency="INR")
    unknown = _opportunity(compensation_text=None, compensation_min=None, compensation_currency=None)
    other_currency = _opportunity(compensation_text="$500", compensation_min=500, compensation_currency="USD")
    assert not matches(low, request)
    assert matches(high, request)
    assert matches(unknown, request)
    assert matches(other_currency, request)


def test_paid_filter_requires_explicit_payment_information():
    request = SearchRequest(keywords="Python", paid_preference="paid")
    paid = _opportunity(compensation_text="INR 12,000", compensation_min=12000, compensation_currency="INR")
    unpaid = _opportunity(compensation_text="Unpaid internship")
    unknown = _opportunity(compensation_text=None)
    assert matches(paid, request)
    assert not matches(unpaid, request)
    assert matches(unknown, request)
    assert classify(unknown, request) == "insufficient_information"


def test_adzuna_http_errors_are_structured_and_do_not_leak_credentials(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "private-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "private-key")
    error = HTTPError("https://api.adzuna.com/?app_key=private-key", 500, "Failure", Message(), BytesIO(b""))
    monkeypatch.setattr(adzuna_module, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    result = AdzunaAdapter().search(_search())
    assert result.status == "failed"
    assert "private-key" not in result.message
