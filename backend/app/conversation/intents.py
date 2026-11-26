from __future__ import annotations

import re

from pydantic import ValidationError

from backend.app.discovery.schemas import SearchRequest
from backend.app.opportunities.schemas import (
    ApplicationPreparationLookupArguments, ApplicationPreviewArguments, HistoryChatArguments, ListChatArguments, NoteChatArguments, RemoveChatArguments,
    SaveChatArguments, StatusChatArguments,
)
from backend.app.resumes.schemas import PassportQueryArguments, ResumeLookupArguments
from .schemas import Intent


_STATUS = re.compile(r"\b(status|progress|workflow)\b", re.IGNORECASE)
_START = re.compile(r"\b(start|begin|initialize|initialise|kick\s+off)\b", re.IGNORECASE)
_PREPARE = re.compile(r"\b(prepare|prep|set\s+up|get\s+ready)\b", re.IGNORECASE)
_DISCOVER = re.compile(r"\b(find|search|look\s+for|look\s+up|show\s+me|discover|fetch|get|retrieve|look\s+up)\b", re.IGNORECASE)
_INTERNSHIP = re.compile(r"\b(internship|internships|intern|interns)\b", re.IGNORECASE)
_FORBIDDEN_SIDE_EFFECT = re.compile(r"\b(submit|approve|send\s+application)\b", re.IGNORECASE)

_INDIA_ALIASES = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "chennai": "Chennai",
    "hyderabad": "Hyderabad", "mumbai": "Mumbai", "bombay": "Mumbai",
    "new delhi": "Delhi", "delhi": "Delhi", "pune": "Pune", "kolkata": "Kolkata",
    "calcutta": "Kolkata", "ahmedabad": "Ahmedabad", "jaipur": "Jaipur",
    "coimbatore": "Coimbatore", "salem": "Salem", "madurai": "Madurai",
    "tiruchirappalli": "Tiruchirappalli", "trichy": "Tiruchirappalli",
    "kochi": "Kochi", "cochin": "Kochi", "thiruvananthapuram": "Thiruvananthapuram",
    "trivandrum": "Thiruvananthapuram", "kozhikode": "Kozhikode", "calicut": "Kozhikode",
    "mysuru": "Mysuru", "mysore": "Mysuru", "mangaluru": "Mangaluru", "mangalore": "Mangaluru",
    "visakhapatnam": "Visakhapatnam", "vizag": "Visakhapatnam", "vijayawada": "Vijayawada",
    "tirupati": "Tirupati", "warangal": "Warangal", "noida": "Noida", "gurugram": "Gurugram",
    "gurgaon": "Gurugram", "faridabad": "Faridabad", "ghaziabad": "Ghaziabad",
    "lucknow": "Lucknow", "kanpur": "Kanpur", "varanasi": "Varanasi", "agra": "Agra",
    "indore": "Indore", "bhopal": "Bhopal", "nagpur": "Nagpur", "nashik": "Nashik",
    "aurangabad": "Aurangabad", "chandigarh": "Chandigarh", "amritsar": "Amritsar",
    "ludhiana": "Ludhiana", "patiala": "Patiala", "dehradun": "Dehradun", "ranchi": "Ranchi",
    "patna": "Patna", "bhubaneswar": "Bhubaneswar", "cuttack": "Cuttack", "guwahati": "Guwahati",
    "shimla": "Shimla", "srinagar": "Srinagar", "pondicherry": "Puducherry", "puducherry": "Puducherry",
    "india": "India",
}
_OTHER_MARKETS = ("United States", "USA", "United Kingdom", "Canada", "Germany", "France", "Australia", "Singapore", "South Africa")
_SEARCH_VERBS = r"(?:find|search|look\s+for|look\s+up|show\s+me|discover|fetch|retrieve|get)"


def _extract_location(text: str) -> tuple[str | None, str | None]:
    for candidate in sorted((*_INDIA_ALIASES, *_OTHER_MARKETS), key=len, reverse=True):
        match = re.search(rf"\b(?:in|near|around)\s+({re.escape(candidate)})\b", text, re.I)
        if match:
            if candidate.casefold() in _INDIA_ALIASES:
                return _INDIA_ALIASES[candidate.casefold()], "in"
            return match.group(1), None
    return None, None


def _extract_compensation(text: str) -> tuple[float | None, str | None]:
    match = re.search(
        r"(?:at\s+least|minimum(?:\s+of)?|above|over)\s*(?:₹|INR\s*|Rs\.?\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k)?\s*(rupees?|INR|₹|dollars?|USD|\$|euros?|EUR|€|pounds?|GBP|£)?",
        text,
        re.I,
    )
    if not match:
        return None, None
    try:
        amount = float(match.group(1).replace(",", "")) * (1000 if match.group(2) else 1)
    except ValueError:
        return None, None
    denomination = (match.group(3) or "").casefold()
    if denomination in {"rupee", "rupees", "inr", "₹"}:
        return amount, "INR"
    if denomination in {"dollar", "dollars", "usd", "$"}:
        return amount, "USD"
    if denomination in {"euro", "euros", "eur", "€"}:
        return amount, "EUR"
    if denomination in {"pound", "pounds", "gbp", "£"}:
        return amount, "GBP"
    return None, None


def _parse_discovery_request(text: str) -> SearchRequest:
    location, country_code = _extract_location(text)
    minimum_compensation, currency = _extract_compensation(text)
    lowered = text.casefold()

    count_match = re.search(rf"\b{_SEARCH_VERBS}\s+(?:me\s+)?(\d{{1,2}})\b", text, re.I)
    result_count = min(int(count_match.group(1)), 20) if count_match else 10
    paid_preference = "unpaid" if re.search(r"\b(unpaid|without\s+pay|no\s+stipend)\b", lowered) else "any"
    if paid_preference == "any" and re.search(r"\bpaid\b", lowered):
        paid_preference = "paid"
    remote_preference = "any"
    for mode in ("remote", "hybrid", "onsite"):
        if re.search(rf"\b{mode}\b", lowered):
            remote_preference = mode
            break
    student_eligibility = "college_student" if re.search(r"\b(college\s+students?|university\s+students?|undergraduates?|students?)\b", lowered) else "any"
    year_words = {"first": 1, "freshman": 1, "freshmen": 1, "second": 2, "sophomore": 2, "sophomores": 2, "third": 3, "junior": 3, "juniors": 3, "fourth": 4, "senior": 4, "seniors": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
    year_match = re.search(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|freshman|freshmen|sophomore|sophomores|junior|juniors|senior|seniors|[1-8](?:st|nd|rd|th)?)\s*[- ]?year(?:\s+(?:college\s+)?students?)?\b|\b(?:year|year of study)\s*([1-8])\b", lowered)
    student_year = None
    if year_match:
        word = year_match.group(1)
        if word:
            student_year = year_words.get(word, int(re.match(r"\d+", word).group()) if re.match(r"\d+", word) else None)
        elif year_match.group(2):
            student_year = int(year_match.group(2))
        student_eligibility = "college_student"

    company_preferences: list[str] = []
    company_match = re.search(r"\b(?:at|from)\s+([A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,2})", text)
    if company_match and company_match.group(1).casefold() not in {"least", "chennai", "india", "bengaluru", "bangalore"}:
        company_preferences = [company_match.group(1).strip()]

    keywords = text
    keywords = re.sub(r"^\s*(?:please\s+)?(?:find|search|look\s+for|show\s+me|discover)\s+(?:me\s+)?", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:internships?|interns?)\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:suitable|appropriate)\s+for\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:college|university)\s+students?\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:at\s+least|minimum(?:\s+of)?|above|over)\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*k?\s*(?:rupees?|INR|₹|dollars?|USD|\$|euros?|EUR|€|pounds?|GBP|£)?", " ", keywords, flags=re.I)
    keywords = re.sub(r"\bwith\s+(?:a\s+)?stipend\b.*$", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:salary information|pay information)\s+(?:is\s+)?available\b", " ", keywords, flags=re.I)
    for candidate in (*_INDIA_ALIASES, *_OTHER_MARKETS):
        keywords = re.sub(rf"\b(?:in|near|around)\s+{re.escape(candidate)}\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|freshman|freshmen|sophomore|sophomores|junior|juniors|senior|seniors|[1-8](?:st|nd|rd|th)?)\s*[- ]?year(?:\s+(?:college\s+)?students?)?\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:students?|undergraduates?)\b", " ", keywords, flags=re.I)
    keywords = re.sub(rf"^\s*(?:please\s+)?{_SEARCH_VERBS}\s+(?:me\s+)?", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:paid|unpaid|remote|hybrid|onsite|on-site|stipend|internship|internships)\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:last|past)\s+\d+\s+days?\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b(?:find|search|look|show|fetch|get|retrieve|me|for|the|a|an|in|at|and|with|if|where|is|of|rupees?)\b", " ", keywords, flags=re.I)
    keywords = re.sub(r"\b\d+\b", " ", keywords)
    keywords = " ".join(re.sub(r"[^\w+#.-]", " ", keywords).split())[:120] or None

    age_match = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+days?\b", text, re.I)
    posted_within_days = int(age_match.group(1)) if age_match else None

    return SearchRequest(
        keywords=keywords,
        location=location,
        country_code=country_code if location else "in",
        remote_preference=remote_preference,
        company_preferences=company_preferences,
        student_eligibility=student_eligibility,
        student_year=student_year,
        paid_preference=paid_preference,
        minimum_compensation=minimum_compensation,
        compensation_currency=currency,
        result_count=result_count,
        posted_within_days=posted_within_days,
    )


def interpret(message: str, previous_search: dict | None = None) -> Intent:
    """Interpret only the small, supported local workflow vocabulary."""
    text = " ".join(message.strip().split())
    lowered = text.casefold()

    if _FORBIDDEN_SIDE_EFFECT.search(text):
        return Intent(
            reply=("I can help prepare a local demo portal, but I can’t approve or submit an application. "
                   "You can ask me for the workflow status, to start the demo, or to prepare Northstar or Summit Talent."),
        )

    if re.search(r"\b(upload|attach)\b", lowered) and re.search(r"\bresume\b", lowered):
        return Intent(reply="Open the Resume tab and choose a PDF or DOCX file. I’ll extract reviewable details locally; nothing is added to your Passport until you accept it.")
    if "resume" in lowered and "passport" in lowered and re.search(r"\b(compare|comparison|check against)\b", lowered):
        filename_match = re.search(r"\b([\w .()-]+\.docx|[\w .()-]+\.pdf)\b", text, re.I)
        args = ResumeLookupArguments(filename=filename_match.group(1).strip() if filename_match else None)
        return Intent(tool_name="compare_resume", arguments=args.model_dump(), reply="I’ll compare the extracted resume information with your Passport without changing any facts.")
    if re.search(r"\b(resumes?|resume records)\b", lowered) and re.search(r"\b(list|show|what|view|my)\b", lowered) and not re.search(r"\b(extracted|extract|skills|education|projects)\b", lowered):
        return Intent(tool_name="list_resumes", reply="I’ll check your local resume records.")
    if "resume" in lowered and "passport" in lowered and re.search(r"\b(add|accept|approve)\b", lowered):
        return Intent(reply="I won’t approve resume facts through chat. Open the Resume tab to review the evidence and accept the specific items you want in your Passport; conflicts stay pending for your choice.")
    if (re.search(r"\b(resume|extracted information|extracted details)\b", lowered) or re.search(r"\binformation did you extract\b", lowered) or ("education" in lowered and "review" in lowered)) and re.search(r"\b(show|list|what|review|extract|skills|education|projects|certifications)\b", lowered):
        category = next((name for name, pattern in (("skills", r"\bskills?\b"), ("education", r"\beducation\b"), ("project", r"\bprojects?\b"), ("experience", r"\bexperience\b"), ("certification", r"\bcertifications?\b"), ("achievement", r"\bachievements?\b")) if re.search(pattern, lowered)), None)
        filename_match = re.search(r"\b([\w .()-]+\.docx|[\w .()-]+\.pdf)\b", text, re.I)
        args = ResumeLookupArguments(category=category, filename=filename_match.group(1).strip() if filename_match else None)
        return Intent(tool_name="get_resume_extraction", arguments=args.model_dump(), reply="I’ll show the extracted information and its current review status.")
    passport_query = re.search(r"\b(?:does|do)\s+(?:my\s+)?passport\s+(?:contain|have|include)\s+(.+?)[?.!]*$", text, re.I)
    if passport_query:
        return Intent(tool_name="search_passport", arguments=PassportQueryArguments(query=passport_query.group(1)).model_dump(), reply="I’ll check the saved Passport facts for that field or value.")

    if re.search(r"\bwhat\b.{0,60}\b(?:filled|couldn.t be filled|could not be filled|failed to fill)\b|\bwhich fields?\b.{0,40}\b(?:filled|couldn.t be filled|could not be filled)\b", lowered):
        ordinal_match = re.search(r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:saved\s+)?(?:internship|opportunity|job|one)\b", lowered)
        ordinal = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}.get(ordinal_match.group(1)) if ordinal_match else None
        company_match = re.search(r"\b(?:for|at|from)\s+(.+?)(?=\s+(?:application|internship|job|form|page)\b|[,.;?]|$)", text, re.I)
        company = company_match.group(1).strip(" .") if company_match and not ordinal else None
        args = ApplicationPreparationLookupArguments(ordinal=ordinal, company=company)
        return Intent(tool_name="get_application_preparation", arguments=args.model_dump(), reply="I’ll read the latest preparation receipt. It contains field names and outcomes, not Passport values.")

    if previous_search and re.search(r"\b(show|list|open|see)\b", lowered) and re.search(r"\b(previous|earlier|recent|last|found)\b", lowered) and re.search(r"\b(internships?|opportunities|results?)\b", lowered):
        return Intent(tool_name="get_recent_search", reply="I’ll show the latest internship results from this chat.")

    if re.search(r"\b(leave|keep)\b", lowered) and re.search(r"\b(blank|empty)\b", lowered) and re.search(r"\b(field|address|location)\b", lowered):
        return Intent(reply="Open the current preparation in Saved opportunities and choose Leave blank on that field. Chat won’t change a field review decision.")
    if re.search(r"\b(map|use|choose)\b", lowered) and re.search(r"\b(passport|github|linkedin|project|field)\b", lowered):
        return Intent(reply="Open the preparation in Saved opportunities, use the field’s Passport selector, and review the value and destination permission before approving it.")

    application_action = re.search(r"\b(application|application page|application form)\b", lowered) and re.search(r"\b(prepare|open|inspect|map|fields?)\b", lowered)
    saved_application_action = re.search(r"\b(prepare|open|inspect)\b", lowered) and re.search(r"\b(saved|first|second|third|this|that|current)\b", lowered) and re.search(r"\b(internship|opportunity|job)\b", lowered)
    if application_action or saved_application_action:
        if re.search(r"\b(approved fields|fill|filled)\b", lowered):
            return Intent(reply="I won’t fill fields directly from chat. Open Saved opportunities, review each proposed mapping, then use the separate preparation approval and fill controls.")
        ordinal_match = re.search(r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:saved\s+)?(?:internship|opportunity|job|one)\b", lowered)
        ordinal = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}.get(ordinal_match.group(1)) if ordinal_match else None
        company_match = re.search(r"\b(?:for|at|from)\s+(.+?)(?=\s+(?:application|internship|job|form|page)\b|[,.;]|$)", text, re.I)
        company = company_match.group(1).strip(" .") if company_match and not ordinal else None
        if not ordinal and not company and re.search(r"\b(first|top|saved)\b", lowered):
            ordinal = 1
        if not ordinal and not company and re.search(r"\b(this|that|current)\b", lowered):
            ordinal = 1
        if not ordinal and not company:
            return Intent(reply="Which saved opportunity should I inspect? Give its number or company name. I’ll show the destination first; you must approve it before the browser opens.")
        args = ApplicationPreviewArguments(ordinal=ordinal, company=company)
        return Intent(tool_name="preview_application", arguments=args.model_dump(), reply="I’ll check the saved listing’s destination only. Opening it and reviewing any fields still requires your confirmation in Saved opportunities.")

    # Opportunity management is handled before discovery so phrases like
    # "show my saved internships" never trigger a fresh external search.
    if re.search(r"\b(history|status history|application history)\b", lowered) and re.search(r"\b(application|internship|saved|my)\b", lowered):
        return Intent(tool_name="get_application_history", reply="I’ll show the saved application status history.")

    if re.search(r"\b(show|list|what|view|display)\b", lowered) and re.search(r"\b(saved|bookmarked)\b", lowered):
        status = None
        if re.search(r"\b(applied|already applied)\b", lowered):
            status = "APPLIED"
        elif re.search(r"\bin interview\b", lowered):
            status = "INTERVIEW"
        sort_by_deadline = bool(re.search(r"\b(sort|order)\b.*\bdeadline\b", lowered)) or "by deadline" in lowered
        return Intent(tool_name="list_saved_opportunities", arguments=ListChatArguments(status=status, sort_by_deadline=sort_by_deadline).model_dump(), reply="I’ll open your saved opportunities.")
    if re.search(r"\b(show|list|what|view|display)\b", lowered) and re.search(r"\b(have\s+i\s+applied|already\s+applied)\b|\bapplications?.{0,35}\bin\s+interview\b", lowered):
        status = "INTERVIEW" if "interview" in lowered else "APPLIED"
        return Intent(tool_name="list_saved_opportunities", arguments={"status": status}, reply=f"I’ll show opportunities marked {status.lower()}.")
    if re.search(r"\b(sort|order)\b", lowered) and re.search(r"\b(saved|opportunities|jobs|internships)\b", lowered) and "deadline" in lowered:
        return Intent(tool_name="list_saved_opportunities", arguments={"sort_by_deadline": True}, reply="I’ll sort your saved opportunities by their source-provided deadlines.")

    ref_match = re.search(r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:saved\s+)?(?:one|internship|opportunity|job)\b", lowered)
    ordinal = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
               "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}.get(ref_match.group(1)) if ref_match else None
    company_match = re.search(r"\b(?:at|from)\s+(.+?)(?=\s+(?:as|to|is|was|with|on|today)\b|[,.;]|$)", text, re.I)
    company = company_match.group(1).strip(" .") if company_match else None

    if re.search(r"\b(save|bookmark|keep)\b", lowered):
        top_match = re.search(r"\btop\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b", lowered)
        if top_match:
            count_text = top_match.group(1)
            count = int(count_text) if count_text.isdigit() else {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}[count_text]
            args = SaveChatArguments(ordinals=list(range(1, count + 1)))
        elif ordinal:
            args = SaveChatArguments(ordinals=[ordinal])
        elif re.search(r"\b(this|that|current|first)\b", lowered) and not company:
            args = SaveChatArguments(ordinals=[ordinal or 1])
        else:
            title_company = re.search(r"\bsave\s+(?:the\s+)?(.+?)\s+(?:internship|intern|job)\b", text, re.I)
            target = company or (title_company.group(1).strip() if title_company else None)
            if not target:
                return Intent(reply="Which recent search result should I save? Give its number or company name.")
            args = SaveChatArguments(company=target)
        return Intent(tool_name="save_opportunity", arguments=args.model_dump(), reply="I’ll save the matching listing from your recent search results.")

    if re.search(r"\b(remove|unsave|delete)\b", lowered) and re.search(r"\b(saved|bookmark|internship|opportunity|job)\b", lowered):
        if not ordinal and not company:
            return Intent(reply="Which saved opportunity should I remove? Give its number or company name.")
        args = RemoveChatArguments(ordinal=ordinal, company=company)
        return Intent(tool_name="remove_saved_opportunity", arguments=args.model_dump(), reply="I’ll remove that saved opportunity.")

    status_patterns = (("INTERVIEW", r"\b(interview|invited to interview)\b"), ("OFFER", r"\b(offer|offered)\b"),
                       ("REJECTED", r"\b(rejected|rejection)\b"), ("WITHDRAWN", r"\b(withdrawn|withdrew)\b"),
                       ("EXPIRED", r"\b(expired|closed)\b"), ("WITHDRAWN", r"\b(withdraw|withdrawn|withdrew)\b"),
                       ("PREPARING", r"\b(preparing|prepare application)\b"),
                       ("APPLIED", r"\b(applied|application sent)\b"))
    status = next((name for name, pattern in status_patterns if re.search(pattern, lowered)), None)
    if status and re.search(r"\b(mark|set|update|change|i applied|i got|i received|i was|i have|invited to)\b", lowered):
        from_search = bool(re.search(r"\b(this|that|current|search result)\b", lowered)) or ordinal is not None
        if not ordinal and not company and not from_search:
            return Intent(reply="Which saved opportunity should I update? Give its number or company name.")
        args = StatusChatArguments(ordinal=ordinal or (1 if from_search else None), company=None if from_search else company, status=status, from_search=from_search)
        return Intent(tool_name="update_application_status", arguments=args.model_dump(), reply=f"I’ll record the status as {status.lower()} for the opportunity you named.")

    if re.search(r"\b(add|write)\s+(?:a\s+)?note\b", lowered):
        note_match = re.search(r"\bnote\b.*?[:,-]\s*[\"“']?(.+?)[\"”']?$", text, re.I)
        note_target = re.search(r"\bnote\s+to\s+(.+?)\s*[:,-]", text, re.I)
        note_company = company or (note_target.group(1).strip() if note_target and not re.search(r"\b(this|that|first|second|third|\d+|one)\b", note_target.group(1), re.I) else None)
        note_ordinal = ordinal or (1 if re.search(r"\b(this|that|current)\b", lowered) else None)
        from_search = note_ordinal is not None
        if not (note_ordinal or note_company) or not note_match or not note_match.group(1).strip():
            return Intent(reply="Tell me which saved listing the note belongs to and the note text.")
        args = NoteChatArguments(ordinal=note_ordinal, company=None if from_search else note_company, from_search=from_search, note=note_match.group(1).strip())
        return Intent(tool_name="add_opportunity_note", arguments=args.model_dump(), reply="I’ll add that private note to the saved opportunity.")

    followup = bool(previous_search and re.fullmatch(
        r"(?:only\s+)?(?:show\s+)?(?:me\s+)?(?:the\s+)?(?:(?:only\s+)?(?:paid|unpaid|remote|hybrid|onsite|on-site)(?:\s+(?:ones?|internships?|opportunities))?(?:\s+instead)?|in\s+[A-Za-z .'-]+(?:\s+instead)?|for\s+(?:first|second|third|fourth|\d+(?:st|nd|rd|th)?)[ -]?year(?: students?)?|show\s+me\s+more|more\s+please)\??",
        text, re.I))
    asks_discovery = bool((_DISCOVER.search(text) and _INTERNSHIP.search(text)) or followup)
    if asks_discovery:
        if _START.search(text) or _PREPARE.search(text) or re.search(r"\b(status|progress)\b", text, re.I):
            return Intent(reply="Would you like me to search internships, check workflow status, or prepare a demo portal? Please choose one.")
        try:
            if followup:
                request = SearchRequest.model_validate(previous_search)
                if re.search(r"\bremote\b", text, re.I): request.remote_preference = "remote"
                elif re.search(r"\bhybrid\b", text, re.I): request.remote_preference = "hybrid"
                elif re.search(r"\b(on-?site)\b", text, re.I): request.remote_preference = "onsite"
                if re.search(r"\bunpaid\b", text, re.I): request.paid_preference = "unpaid"
                elif re.search(r"\bpaid\b", text, re.I): request.paid_preference = "paid"
                location, country = _extract_location(text)
                if location: request.location, request.country_code = location, country
                year_match = re.search(r"\b(first|second|third|fourth|\d+)(?:st|nd|rd|th)?[ -]?year", text, re.I)
                if year_match:
                    request.student_year = {"first":1,"second":2,"third":3,"fourth":4}.get(year_match.group(1).casefold(), int(year_match.group(1)) if year_match.group(1).isdigit() else None)
                    request.student_eligibility = "college_student"
                if re.search(r"\bmore\b", text, re.I): request.result_count = 20
            else:
                request = _parse_discovery_request(text)
        except ValidationError:
            return Intent(reply="What role, skill, or location should I search for? I can search internship listings from the supported sources.")
        if not request.keywords and not request.location and request.paid_preference == "any" and request.remote_preference == "any" and request.minimum_compensation is None and request.student_eligibility == "any":
            return Intent(reply="What role, skill, or location should I search for? I can search internship listings from the supported sources.")
        return Intent(
            tool_name="search_internships",
            arguments=request.model_dump(mode="json"),
            reply="I’ll search the supported internship sources and show where each listing came from.",
        )

    asks_start = bool(_START.search(text))
    asks_prepare = bool(_PREPARE.search(text))
    explicit_status = bool(re.search(r"\b(status|progress)\b", lowered)) or bool(
        re.search(r"\bwhat(?:'s| is)\s+(?:the\s+)?current\s+status\b", lowered)
    )
    asks_status = explicit_status or (bool(_STATUS.search(text)) and not (asks_start or asks_prepare))
    if (explicit_status and (asks_start or asks_prepare)) or sum((asks_status, asks_start, asks_prepare)) > 1:
        return Intent(reply="Would you like me to check status, start the demo, or prepare one portal? Please choose one.")

    if asks_status:
        return Intent(
            tool_name="get_workflow_status",
            reply="I’ll check the current workflow status.",
        )

    if asks_start:
        return Intent(
            tool_name="start_demo",
            reply="I’ll start the local demo workflow.",
        )

    if asks_prepare:
        mentions_northstar = "northstar" in lowered or re.search(r"\bportal\s+a\b", lowered)
        mentions_summit = "summit" in lowered or re.search(r"\bportal\s+b\b", lowered)
        if mentions_northstar and mentions_summit:
            return Intent(reply="Which one should I prepare: Northstar or Summit Talent? I can prepare one portal at a time.")
        if mentions_northstar:
            return Intent(
                tool_name="prepare_local_portal",
                arguments={"portal": "northstar"},
                reply="I’ll prepare the Northstar demo portal using the approved Passport facts.",
            )
        if mentions_summit:
            return Intent(
                tool_name="prepare_local_portal",
                arguments={"portal": "summit_talent"},
                reply="I’ll prepare the Summit Talent demo portal using the approved Passport facts.",
            )
        return Intent(reply="Which local portal should I prepare: Northstar or Summit Talent?")

    return Intent(
        reply=("I can check your workflow status, start the local demo, or prepare Northstar or Summit Talent. "
               "Which would you like?"),
    )
