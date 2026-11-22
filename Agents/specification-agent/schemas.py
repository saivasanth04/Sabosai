
from typing import Literal
from pydantic import BaseModel, Field


class AcceptanceCriterion(BaseModel):
    given: str
    when: str
    then: str


class Requirement(BaseModel):
    id: str
    kind: Literal["functional", "non_functional"]
    description: str
    source: str
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class Specification(BaseModel):
    title: str
    goal: str
    requirements: list[Requirement]
    constraints: list[str]
    assumptions: list[str]
    out_of_scope: list[str]
    open_questions: list[str]


class Review(BaseModel):
    blocking_questions: list[str]
    defects: list[str]
