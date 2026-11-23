"""Contracts shared by the nodes, providers and exported research reports."""
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResearchCategory(str, Enum):
    WEB = 'web'
    DOCUMENTATION = 'documentation'
    API = 'api'
    REPOSITORY = 'repository'
    LIBRARY = 'library'
    ACADEMIC = 'academic'


class ResearchClassification(BaseModel):
    summary: Text
    categories: list[ResearchCategory] = Field(min_length=1)
    technologies: list[str]
    requires_code_inspection: bool
    requires_version_verification: bool


class ResearchQuestion(BaseModel):
    question_id: str = Field(default_factory=lambda: uuid4().hex)
    question: Text
    categories: list[ResearchCategory] = Field(min_length=1)
    subject: str | None
    package_name: str | None
    package_ecosystem: Literal['pypi', 'npm'] | None
    repository_url: HttpUrl | None
    documentation_url: HttpUrl | None
    api_spec_url: HttpUrl | None
    code_symbol: str | None


class Decomposition(BaseModel):
    questions: list[ResearchQuestion] = Field(min_length=1)


class CollectionJob(ResearchQuestion):
    selected_sources: list[str]
    selected_skills: list[str]
    unavailable_sources: list[str]


class Evidence(BaseModel):
    evidence_id: str
    question_id: str
    source: str
    source_type: str
    title: Text
    url: HttpUrl
    content: Text
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: str | None = None
    authors: list[str] = Field(default_factory=list)
    version: str | None = None
    provenance: str
    metadata: dict = Field(default_factory=dict)


class SourceFailure(BaseModel):
    question_id: str
    source: str
    message: str


class InspectionResult(BaseModel):
    evidence_id: str
    excerpt: Text
    observation: Text


class Inspections(BaseModel):
    observations: list[InspectionResult]


class Contradiction(BaseModel):
    evidence_ids: list[str] = Field(min_length=2)
    explanation: Text


class CrossValidation(BaseModel):
    contradictions: list[Contradiction]
    unrelated_evidence_ids: list[str]


class ResearchFinding(BaseModel):
    finding_id: Text
    claim: Text
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal['high', 'medium', 'low'] = 'low'


class DraftFinding(BaseModel):
    claim: Text
    evidence_ids: list[str] = Field(min_length=1)
    section: Literal['finding', 'comparison', 'recommendation']


class ReportDraft(BaseModel):
    findings: list[DraftFinding] = Field(min_length=1, max_length=14)
    limitations: list[str]


class ResearchReport(BaseModel):
    executive_summary: list[str] = Field(min_length=1)
    findings: list[ResearchFinding] = Field(min_length=1, max_length=40)
    comparisons: list[str]
    recommendations: list[str]
    limitations: list[str]


class ClaimAudit(BaseModel):
    finding_id: Text
    supported: bool
    explanation: Text


class CitationValidation(BaseModel):
    audits: list[ClaimAudit]


class SearchPlan(BaseModel):
    queries: list[Text] = Field(min_length=1, max_length=3)


class CrawlPlan(BaseModel):
    indices: list[int] = Field(max_length=3)
