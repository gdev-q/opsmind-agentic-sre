"""Pydantic models (API + LLM structured output) and the LangGraph state."""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

Severity = Literal["P1", "P2", "P3"]
Service = Literal[
    "Kafka", "Kubernetes", "Redis", "Elasticsearch", "MySQL", "SpringBoot", "Other"
]


class TriageRequest(BaseModel):
    log: str = Field(..., max_length=20000, description="Raw error log / stack trace")

    @field_validator("log")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("log must contain at least 10 non-blank characters")
        return v


# --- LLM structured outputs (no defaults: keeps the JSON schema Gemini-friendly) ---
class TriageResult(BaseModel):
    affected_service: Service = Field(description="Primary failing technology")
    error_type: str = Field(description="Short error class, e.g. 'Consumer group rebalance loop'")
    severity: Severity = Field(
        description="P1=customer-facing outage/data loss, P2=degraded, P3=minor/no user impact"
    )
    search_query: str = Field(
        description="8-15 keyword runbook search query: technology, symptom, key error tokens"
    )


class CommandStep(BaseModel):
    description: str = Field(description="What the step does / what to look for")
    command: str = Field(description="Exact CLI command; use <PLACEHOLDERS> for unknown values")


class RemediationPlan(BaseModel):
    runbook_found: bool = Field(description="True only if retrieved runbooks match this failure")
    root_cause_hypothesis: str = Field(description="Grounded in runbooks; say so if none match")
    steps: list[CommandStep] = Field(description="Ordered: diagnostics first, then mitigation")
    escalation: str = Field(description="Who/when to escalate, from the runbooks")
    rollback: str = Field(description="Rollback guidance, from the runbooks")
    citations: list[str] = Field(description="Exact runbook filenames used")


# --- Retrieval / API models ---
class RunbookChunk(BaseModel):
    source: str
    heading: str
    services: list[str]
    content: str
    score: float = 0.0


class TriageResponse(BaseModel):
    triage: TriageResult
    retrieved: list[RunbookChunk]
    retrieval_backend: str
    plan: RemediationPlan
    trace: list[str]


class IngestResponse(BaseModel):
    files: int
    chunks: int
    backend: str
    es_available: bool


class HealthResponse(BaseModel):
    status: str
    backend: str
    es_available: bool
    chunks_indexed: int
    llm_configured: bool


class AgentState(TypedDict, total=False):
    raw_log: str
    triage: TriageResult
    retrieved: list[RunbookChunk]
    retrieval_backend: str
    plan: RemediationPlan
    trace: Annotated[list[str], operator.add]
