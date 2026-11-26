from __future__ import annotations

import re
from datetime import date, timedelta

from .schemas import MatchCategory, Opportunity, SearchRequest


def _terms(value: str) -> list[str]:
    return re.findall(r"[a-z0-9+#.]+", value.casefold())


def _paid_status(item: Opportunity) -> bool | None:
    text = " ".join(filter(None, [item.compensation_text, item.description, item.title])).casefold()
    if re.search(r"\b(unpaid|no stipend|without pay|volunteer)\b", text):
        return False
    if (item.compensation_min is not None and item.compensation_min > 0) or (item.compensation_max is not None and item.compensation_max > 0):
        return True
    if item.compensation_text and (re.search(r"\d", item.compensation_text) or re.search(r"\bpaid\b", item.compensation_text, re.I)):
        return True
    return None


def classify(item: Opportunity, request: SearchRequest, today: date | None = None) -> MatchCategory | None:
    today = today or date.today()
    unknowns: list[str] = []
    listing_text = " ".join(filter(None, [
        item.title,
        item.company_name,
        item.description,
        item.location,
        *item.skills,
        *item.categories,
    ])).casefold()

    if request.keywords:
        tokens = _terms(request.keywords)
        matched_tokens = [token for token in tokens if re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", listing_text)]
        if tokens and not matched_tokens:
            return None
        if len(matched_tokens) < len(tokens):
            unknowns.append("partial keyword evidence")
    if request.categories:
        categories_text = " ".join(item.categories + [item.title]).casefold()
        if not any(category.casefold() in categories_text for category in request.categories):
            return None
    if request.company_preferences:
        company = (item.company_name or "").casefold()
        if not any(preference.casefold() in company for preference in request.company_preferences):
            return None
    if request.skills:
        searchable = " ".join(item.skills + [item.title, item.description or ""]).casefold()
        absent = [skill for skill in request.skills if not re.search(rf"(?<![\w]){re.escape(skill.casefold())}(?![\w])", searchable)]
        if absent:
            unknowns.append("some requested skills are not confirmed")
    if request.student_eligibility == "college_student":
        if not item.eligibility_evidence:
            unknowns.append("student eligibility not disclosed")
    if request.student_year and not item.eligibility_evidence:
        unknowns.append("student year eligibility not disclosed")
    if request.remote_preference != "any":
        if item.work_mode == "unknown":
            unknowns.append("work mode not disclosed")
        elif item.work_mode != request.remote_preference:
            return None
    if request.location and not item.location:
        unknowns.append("location not disclosed")
    elif request.location and item.location:
        location = item.location.casefold()
        wanted = request.location.casefold()
        if wanted not in location and not (item.work_mode == "remote" and re.search(r"\b(worldwide|anywhere|global)\b", location)):
            return None
    if request.paid_preference != "any":
        paid = _paid_status(item)
        if paid is None:
            unknowns.append("compensation not disclosed")
        elif paid != (request.paid_preference == "paid"):
            return None
    if request.minimum_compensation is not None and item.compensation_currency == request.compensation_currency:
        observed_min = item.compensation_min
        if observed_min is None and item.compensation_max is not None:
            observed_min = item.compensation_max
        # Keep unknown or estimated values visible; a known low value is excluded.
        if observed_min is not None and observed_min < request.minimum_compensation:
            return None
    elif request.minimum_compensation is not None:
        unknowns.append("comparable compensation not disclosed")
    if request.posted_within_days is not None:
        if item.posted_date is None:
            unknowns.append("posting date unavailable")
        elif item.posted_date < today - timedelta(days=request.posted_within_days):
            return None
    if unknowns:
        # Distinguish incomplete key constraints from potentially relevant
        # listings whose core role matches but need source-page review.
        return "insufficient_information" if any("not disclosed" in x or "unavailable" in x for x in unknowns) else "potential_match"
    return "verified_match"


def matches(item: Opportunity, request: SearchRequest, today: date | None = None) -> bool:
    return classify(item, request, today) is not None


def ranking(item: Opportunity, request: SearchRequest, today: date | None = None) -> tuple[int, list[str]]:
    today = today or date.today()
    score = 0
    reasons: list[str] = []
    title = item.title.casefold()
    listing_text = " ".join(filter(None, [title, item.description, item.company_name, *item.categories])).casefold()

    if request.keywords:
        if request.keywords.casefold() in title:
            score += 5
            reasons.append("Role title matches your search")
        else:
            matched = [term for term in _terms(request.keywords) if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", listing_text)]
            if matched:
                score += min(4, len(matched))
                reasons.append("Listing mentions " + ", ".join(dict.fromkeys(matched[:3])))
    if request.categories and any(cat.casefold() in " ".join(item.categories + [item.title]).casefold() for cat in request.categories):
        score += 3
        reasons.append("Role category matches your preference")
    matched_skills = [skill for skill in request.skills if re.search(rf"(?<![\w]){re.escape(skill.casefold())}(?![\w])", " ".join(item.skills + [item.title, item.description or ""]).casefold())]
    if matched_skills:
        score += 2 * len(matched_skills)
        reasons.append("Listing mentions " + ", ".join(matched_skills[:3]))
    if request.student_eligibility == "college_student" and item.eligibility_evidence:
        score += 2
        reasons.append("Listing includes student-related eligibility text; verify it on the source page")
    if request.student_year:
        reasons.append(f"Student year {request.student_year} is not confirmed unless stated in source eligibility")
    if request.location and item.location and request.location.casefold() in item.location.casefold():
        score += 3
        reasons.append(f"Location matches {request.location}")
    elif item.work_mode == "remote" and item.location and re.search(r"\b(worldwide|anywhere|global)\b", item.location, re.I):
        score += 2
        reasons.append("Remote listing says applicants may be worldwide")
    if request.remote_preference != "any" and item.work_mode == request.remote_preference:
        score += 3
        reasons.append(f"Work mode is {item.work_mode}")
    if request.paid_preference == "paid" and _paid_status(item) is True:
        score += 2
        reasons.append("Compensation is disclosed")
    if item.posted_date and (today - item.posted_date).days <= 30:
        score += 1
        reasons.append("Recently posted")
    if not reasons:
        reasons.append("Closest available listing in the supported sources")
    return score, reasons
