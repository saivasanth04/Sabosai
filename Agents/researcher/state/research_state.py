import operator
from typing import Annotated, TypedDict

from .schemas import (CollectionJob, CrossValidation, Evidence, InspectionResult,
                      ResearchClassification, ResearchFinding, ResearchQuestion,
                      ResearchReport, SourceFailure)


class ResearchState(TypedDict, total=False):
    request: str
    run_id: str
    job: CollectionJob
    classification: ResearchClassification
    questions: list[ResearchQuestion]
    jobs: list[CollectionJob]
    evidence: Annotated[list[Evidence], operator.add]
    errors: Annotated[list[SourceFailure], operator.add]
    inspection_results: list[InspectionResult]
    cross_validation: CrossValidation
    package_versions: dict
    report: ResearchReport
    citation_passed: bool
    citation_feedback: list[str]
    citation_attempts: int
    citation_audits: list[dict]
    coverage: dict
    output_files: dict[str, str]
