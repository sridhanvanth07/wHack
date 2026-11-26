from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CanonicalFieldDefinition:
    name: str
    semantic_type: str
    fact_id: str
    label: str
    description: str
    example: str


CANONICAL_FIELDS: dict[str, CanonicalFieldDefinition] = {
    "full_name": CanonicalFieldDefinition(
        name="full_name",
        semantic_type="person.name",
        fact_id="full_name",
        label="Full name",
        description="Legal name or candidate full name",
        example="Aarav Mehta",
    ),
    "first_name": CanonicalFieldDefinition(
        name="first_name", semantic_type="person.name", fact_id="full_name", label="First name",
        description="Given name derived from a verified full name; requires explicit review", example="Aarav",
    ),
    "last_name": CanonicalFieldDefinition(
        name="last_name", semantic_type="person.name", fact_id="full_name", label="Last name",
        description="Family name derived from a verified full name; requires explicit review", example="Mehta",
    ),
    "email": CanonicalFieldDefinition(
        name="email",
        semantic_type="person.email",
        fact_id="email",
        label="Email address",
        description="Primary contact email address",
        example="aarav.mehta@example.test",
    ),
    "phone": CanonicalFieldDefinition(
        name="phone",
        semantic_type="person.phone",
        fact_id="phone",
        label="Phone number",
        description="Mobile or primary contact telephone number",
        example="9876543210",
    ),
    "degree": CanonicalFieldDefinition(
        name="degree",
        semantic_type="education.degree",
        fact_id="education",
        label="Degree",
        description="Highest academic qualification or degree program",
        example="B.Tech Information Technology",
    ),
    "graduation_year": CanonicalFieldDefinition(
        name="graduation_year",
        semantic_type="education.graduation_year",
        fact_id="graduation_year",
        label="Graduation year",
        description="Year of graduation, passing year, or expected completion year",
        example="2027",
    ),
    "skills": CanonicalFieldDefinition(
        name="skills",
        semantic_type="skills.technical",
        fact_id="skills",
        label="Technical skills",
        description="Technical competencies, programming skills, or core abilities",
        example="Python, React, SQL",
    ),
    "current_address": CanonicalFieldDefinition(
        name="current_address",
        semantic_type="address.current",
        fact_id="current_address",
        label="Current address",
        description="Present residential address, current city or location of residence",
        example="12 Lake View Road, Bengaluru",
    ),
    "permanent_address": CanonicalFieldDefinition(
        name="permanent_address",
        semantic_type="address.permanent",
        fact_id="permanent_address",
        label="Permanent address",
        description="Permanent hometown address or permanent legal residence",
        example="44 Park Street, Mysuru",
    ),
    "education_institution": CanonicalFieldDefinition(
        name="education_institution", semantic_type="education.institution", fact_id="education_institution",
        label="Educational institution", description="University, college or school explicitly listed in the profile", example="Example University",
    ),
    "education_major": CanonicalFieldDefinition(
        name="education_major", semantic_type="education.major", fact_id="education_major",
        label="Course or major", description="Academic major or course of study", example="Computer Science",
    ),
    "linkedin_url": CanonicalFieldDefinition(
        name="linkedin_url", semantic_type="professional.linkedin_url", fact_id="linkedin_url",
        label="LinkedIn URL", description="Professional LinkedIn profile URL", example="https://www.linkedin.com/in/example",
    ),
    "github_url": CanonicalFieldDefinition(
        name="github_url", semantic_type="professional.github_url", fact_id="github_url",
        label="GitHub URL", description="GitHub profile URL", example="https://github.com/example",
    ),
    "portfolio_url": CanonicalFieldDefinition(
        name="portfolio_url", semantic_type="professional.portfolio_url", fact_id="portfolio_url",
        label="Portfolio URL", description="Professional portfolio or personal website URL", example="https://example.dev",
    ),
}

# Reverse index: semantic_type -> CanonicalFieldDefinition
SEMANTIC_TO_CANONICAL: dict[str, CanonicalFieldDefinition] = {
    cf.semantic_type: cf for cf in CANONICAL_FIELDS.values()
}

# Reverse index: fact_id -> CanonicalFieldDefinition
FACT_ID_TO_CANONICAL: dict[str, CanonicalFieldDefinition] = {
    cf.fact_id: cf for cf in CANONICAL_FIELDS.values()
}
