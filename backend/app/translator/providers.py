from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Optional

from dotenv import load_dotenv

from .canonical import CANONICAL_FIELDS, CanonicalFieldDefinition
from .schema import RawLLMResponse, TranslationRequest

# Ensure .env is loaded
load_dotenv()

logger = logging.getLogger(__name__)


class BaseMappingProvider(ABC):
    @abstractmethod
    def translate(self, request: TranslationRequest) -> RawLLMResponse:
        """Translate a field label and optional description into a RawLLMResponse."""
        pass


class MockMappingProvider(BaseMappingProvider):
    """
    Deterministic rule- and semantic-alias mapper for mock mode.
    Maps known synonyms, detects ambiguity, and rejects unknown fields.
    """

    def translate(self, request: TranslationRequest) -> RawLLMResponse:
        raw_label = request.label.strip()
        desc = (request.description or "").strip()
        combined = f"{raw_label} {desc}".casefold()
        norm = re.sub(r"[_\W]+", " ", combined).strip()

        # 1. Address Ambiguity Check
        # If the label has "location" or "address" without "current", "present", "permanent", or "hometown"
        has_location_kw = any(w in norm for w in ["location", "address", "residence", "city"])
        has_current_kw = any(w in norm for w in ["current", "present", "living", "local"])
        has_permanent_kw = any(w in norm for w in ["permanent", "hometown", "native", "domicile"])

        if has_location_kw and not has_current_kw and not has_permanent_kw:
            return RawLLMResponse(
                canonical_field=None,
                confidence=0.42,
                explanation=f"'{raw_label}' is ambiguous; could mean current or permanent address.",
                requires_user_confirmation=True,
            )

        # 2. Permanent Address
        if has_permanent_kw or (has_location_kw and has_permanent_kw):
            return RawLLMResponse(
                canonical_field="permanent_address",
                confidence=0.96,
                explanation=f"'{raw_label}' refers to permanent legal residence.",
                requires_user_confirmation=False,
            )

        # 3. Current Address
        if (has_location_kw and has_current_kw) or norm in {"current location", "current address", "present address", "city of residence"}:
            return RawLLMResponse(
                canonical_field="current_address",
                confidence=0.96,
                explanation=f"'{raw_label}' refers to current residence location.",
                requires_user_confirmation=False,
            )

        if re.search(r"\b(first|given)\s+name\b", norm):
            return RawLLMResponse(canonical_field="first_name", confidence=0.94, explanation="First name is derived from the verified full name and should be reviewed.", requires_user_confirmation=True)
        if re.search(r"\b(last|family|sur)\s*name\b|\bsurname\b", norm):
            return RawLLMResponse(canonical_field="last_name", confidence=0.94, explanation="Last name is derived from the verified full name and should be reviewed.", requires_user_confirmation=True)

        # 4. Full Name (synonyms: full name, candidate name, applicant's legal name, name of candidate, etc.)
        name_patterns = [
            r"\b(candidate|applicant|student|legal|person|full)?\s*name\b",
            r"\bname\s+of\s+(candidate|applicant|student)\b",
            r"\bfull\s*name\b",
            r"\blegal\s*name\b",
        ]
        if any(re.search(p, norm) for p in name_patterns):
            return RawLLMResponse(
                canonical_field="full_name",
                confidence=0.99,
                explanation=f"'{raw_label}' refers to the candidate's full legal name.",
                requires_user_confirmation=False,
            )

        # 5. Email
        if any(w in norm for w in ["email", "e mail", "mail address"]):
            return RawLLMResponse(
                canonical_field="email",
                confidence=0.99,
                explanation=f"'{raw_label}' refers to primary contact email address.",
                requires_user_confirmation=False,
            )

        # 6. Phone
        if any(w in norm for w in ["phone", "mobile", "telephone", "contact number", "cell", "cellphone"]):
            return RawLLMResponse(
                canonical_field="phone",
                confidence=0.99,
                explanation=f"'{raw_label}' refers to primary contact phone number.",
                requires_user_confirmation=False,
            )

        if "linkedin" in norm:
            return RawLLMResponse(canonical_field="linkedin_url", confidence=0.98, explanation="LinkedIn profile URL.", requires_user_confirmation=False)
        if "github" in norm:
            return RawLLMResponse(canonical_field="github_url", confidence=0.98, explanation="GitHub profile URL.", requires_user_confirmation=False)
        if any(w in norm for w in ["portfolio", "personal website", "website url"]):
            return RawLLMResponse(canonical_field="portfolio_url", confidence=0.94, explanation="Professional website or portfolio URL.", requires_user_confirmation=False)
        if any(w in norm for w in ["university", "institution", "college", "school"]):
            return RawLLMResponse(canonical_field="education_institution", confidence=0.94, explanation="Educational institution.", requires_user_confirmation=False)
        if re.search(r"\bmajor\b|\bfield of study\b|\bconcentration\b", norm):
            return RawLLMResponse(canonical_field="education_major", confidence=0.91, explanation="Academic major or field of study.", requires_user_confirmation=False)

        # 7. Degree / Academic background
        degree_patterns = [
            r"\b(academic|educational)\s*(background|qualification|history)\b",
            r"\b(highest\s+)?qualification\b",
            r"\bdegree\b",
            r"\bcourse\s+of\s+study\b",
            r"\bprogram\b",
        ]
        if any(re.search(p, norm) for p in degree_patterns):
            return RawLLMResponse(
                canonical_field="degree",
                confidence=0.97,
                explanation=f"'{raw_label}' refers to the academic degree or qualification.",
                requires_user_confirmation=False,
            )

        # 8. Graduation / Passing year
        grad_patterns = [
            r"\b(expected\s+)?completion(\s+year)?\b",
            r"\bpassing\s+year\b",
            r"\bgraduation\s+year\b",
            r"\byear\s+of\s+(graduation|passing|completion)\b",
            r"\bgrad\s+year\b",
            r"\bcompletion\s+date\b",
        ]
        if any(re.search(p, norm) for p in grad_patterns):
            return RawLLMResponse(
                canonical_field="graduation_year",
                confidence=0.98,
                explanation=f"'{raw_label}' refers to the graduation or completion year.",
                requires_user_confirmation=False,
            )

        # 9. Technical Skills / Competencies
        skills_patterns = [
            r"\b(technical|core|key)?\s*competenc(y|ies)\b",
            r"\b(technical|key|programming|relevant)?\s*skills\b",
            r"\btech\s+stack\b",
            r"\btools\s+and\s+technologies\b",
        ]
        if any(re.search(p, norm) for p in skills_patterns):
            return RawLLMResponse(
                canonical_field="skills",
                confidence=0.96,
                explanation=f"'{raw_label}' refers to technical skills and competencies.",
                requires_user_confirmation=False,
            )

        # 10. Unknown / Unmapped field
        return RawLLMResponse(
            canonical_field=None,
            confidence=0.0,
            explanation=f"Field '{raw_label}' does not match any profile attribute.",
            requires_user_confirmation=False,
        )


class LLMMappingProvider(BaseMappingProvider):
    """
    LLM-backed provider supporting configurable models via environment variables.
    Falls back safely to MockMappingProvider if credentials are missing or model fails.
    """

    def __init__(self, fallback: Optional[BaseMappingProvider] = None):
        self.fallback = fallback or MockMappingProvider()

    def _get_config(self) -> tuple[str, str, str, str]:
        load_dotenv()
        provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip()
        return provider, api_key, model, base_url

    def is_available(self) -> bool:
        provider, api_key, _, _ = self._get_config()
        return bool(api_key and provider != "mock")

    def translate(self, request: TranslationRequest) -> RawLLMResponse:
        if not self.is_available():
            logger.debug("LLM not configured or provider is mock; using MockMappingProvider.")
            return self.fallback.translate(request)

        # If LLM credentials are provided, attempt structured API call
        try:
            return self._call_llm(request)
        except Exception as e:
            logger.warning(f"LLM translation failed ({e}); falling back to mock provider.")
            fallback_res = self.fallback.translate(request)
            fallback_res.explanation = f"[LLM fallback] {fallback_res.explanation}"
            return fallback_res

    def _call_llm(self, request: TranslationRequest) -> RawLLMResponse:
        import urllib.request
        import urllib.error

        provider, api_key, model, base_url = self._get_config()

        # Determine endpoint and model defaults by provider
        if provider == "gemini":
            endpoint = f"{base_url.rstrip('/') if base_url else 'https://generativelanguage.googleapis.com/v1beta/openai'}/chat/completions"
            active_model = model or "gemini-2.5-flash"
        elif provider == "anthropic":
            endpoint = f"{base_url.rstrip('/') if base_url else 'https://api.anthropic.com/v1'}/messages"
            active_model = model or "claude-3-5-haiku-latest"
        else:
            # Default: OpenAI or OpenAI-compatible endpoint
            endpoint = f"{base_url.rstrip('/') if base_url else 'https://api.openai.com/v1'}/chat/completions"
            active_model = model or "gpt-4o-mini"

        canonical_names = list(CANONICAL_FIELDS.keys())
        prompt = (
            f"You are a web form field mapper for a job application assistant. "
            f"Map the following form field label to one of the canonical profile fields: "
            f"{canonical_names}. "
            f"If the label does not match or is ambiguous (such as 'Location' without current/permanent), "
            f"set canonical_field to null and requires_user_confirmation to true.\n"
            f"Field Label: \"{request.label}\"\n"
            f"Field Description: \"{request.description or ''}\"\n"
            f"Respond ONLY with a valid JSON object in this exact format:\n"
            f'{{"canonical_field": "<field_or_null>", "confidence": <float_between_0_and_1>, '
            f'"explanation": "<short_reason>", "requires_user_confirmation": <bool>}}'
        )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        # Build payload for OpenAI-compatible endpoint
        payload = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": "You are a specialized field mapping model. You output valid JSON only, without markdown fences."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            raw_text = content.strip()
            # Strip markdown code blocks if the model wrapped output in ```json ... ```
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL).strip()
            parsed = json.loads(raw_text)
            return RawLLMResponse(**parsed)
