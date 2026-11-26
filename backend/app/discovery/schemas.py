from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]
SourceState = Literal["success", "no_results", "filtered_out", "failed", "not_configured", "unsupported", "rate_limited"]
MatchCategory = Literal["verified_match", "potential_match", "insufficient_information"]
Currency = Literal["INR", "USD", "EUR", "GBP", "CAD", "AUD", "SGD", "ZAR"]


class SearchRequest(BaseModel):
    """Validated user preferences for a bounded internship search."""

    model_config = ConfigDict(extra="forbid")

    keywords: str | None = Field(default=None, max_length=120)
    categories: list[str] = Field(default_factory=list, max_length=8)
    location: str | None = Field(default=None, max_length=100)
    country_code: Literal["in"] | None = "in"
    remote_preference: Literal["any", "remote", "hybrid", "onsite"] = "any"
    company_preferences: list[str] = Field(default_factory=list, max_length=8)
    skills: list[str] = Field(default_factory=list, max_length=12)
    internship_type: Literal["internship"] = "internship"
    student_eligibility: Literal["any", "college_student"] = "any"
    student_year: int | None = Field(default=None, ge=1, le=8)
    paid_preference: Literal["any", "paid", "unpaid"] = "any"
    minimum_compensation: float | None = Field(default=None, ge=0, le=10_000_000)
    compensation_currency: Currency | None = None
    result_count: int = Field(default=10, ge=1, le=20)
    posted_within_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("keywords", "location")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("categories", "company_preferences", "skills")
    @classmethod
    def clean_text_lists(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        if any(len(item) > 80 for item in cleaned):
            raise ValueError("Search terms must be 80 characters or fewer")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def require_a_search_criterion(self) -> "SearchRequest":
        if not any((
            self.keywords,
            self.categories,
            self.location,
            self.company_preferences,
            self.skills,
            self.remote_preference != "any",
            self.paid_preference != "any",
            self.minimum_compensation is not None,
            self.posted_within_days is not None,
            self.student_eligibility != "any",
        )):
            raise ValueError("Add a role, skill, location, or other search preference")
        if self.minimum_compensation is not None and self.compensation_currency is None:
            raise ValueError("A currency is required with minimum compensation")
        return self


class SourceAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str
    listing_id: str | None = None
    listing_url: str


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=240)
    company_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1600)
    location: str | None = Field(default=None, max_length=200)
    work_mode: WorkMode = "unknown"
    skills: list[str] = Field(default_factory=list, max_length=30)
    categories: list[str] = Field(default_factory=list, max_length=12)
    compensation_text: str | None = Field(default=None, max_length=160)
    compensation_min: float | None = Field(default=None, ge=0)
    compensation_max: float | None = Field(default=None, ge=0)
    compensation_currency: Currency | None = None
    compensation_estimated: bool | None = None
    duration: str | None = Field(default=None, max_length=120)
    eligibility_evidence: str | None = Field(default=None, max_length=400)
    posted_date: date | None = None
    application_deadline: date | None = None
    listing_url: str
    source_name: str
    source_listing_id: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_attributions: list[SourceAttribution] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    match_category: MatchCategory = "verified_match"

    @field_validator("listing_url")
    @classmethod
    def listing_url_must_be_https(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Listing links must be safe HTTPS URLs")
        return value

    @model_validator(mode="after")
    def validate_compensation_range(self) -> "Opportunity":
        if self.compensation_min is not None and self.compensation_max is not None:
            if self.compensation_max < self.compensation_min:
                raise ValueError("Maximum compensation cannot be less than minimum compensation")
        if not self.source_attributions:
            self.source_attributions = [SourceAttribution(
                source_name=self.source_name,
                listing_id=self.source_listing_id,
                listing_url=self.listing_url,
            )]
        return self


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str
    status: SourceState
    result_count: int = Field(default=0, ge=0)
    message: str


class DiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunities: list[Opportunity]
    sources: list[SourceStatus]
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    query_summary: str
    assistant_summary: str
