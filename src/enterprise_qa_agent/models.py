"""Domain data structures for the enterprise QA agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    TOOL_ERROR = "tool_error"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    INVALID_INPUT = "invalid_input"
    NOT_APPLICABLE = "not_applicable"


class RouteType(StrEnum):
    KNOWLEDGE_QUERY = "knowledge_query"
    CLARIFICATION_RESPONSE = "clarification_response"
    NON_QUESTION = "non_question"
    INVALID_INPUT = "invalid_input"


class ActionType(StrEnum):
    SEARCH_DOCS = "search_docs"
    READ_DOC = "read_doc"
    ASK_USER = "ask_user"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class QueryRequest:
    question: str
    history: tuple[ChatMessage, ...] = ()
    requester_departments: tuple[str, ...] = ("public",)
    as_of_date: str = field(default_factory=lambda: date.today().isoformat())

    def __post_init__(self) -> None:
        if not self.requester_departments or any(
            not isinstance(item, str) or not item.strip()
            for item in self.requester_departments
        ):
            raise ValueError("requester_departments 必须包含至少一个非空部门")
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in self.requester_departments))
        object.__setattr__(self, "requester_departments", normalized)
        try:
            date.fromisoformat(self.as_of_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("as_of_date 必须是 ISO YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class ResolvedQuery:
    original_question: str
    standalone_query: str
    route: RouteType
    resolution_note: str
    memory_strategy: str = "none"
    retrieved_history_indexes: tuple[int, ...] = ()
    recent_history_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentScope:
    employee_types: tuple[str, ...] = ("all",)
    regions: tuple[str, ...] = ("all",)
    business_types: tuple[str, ...] = ("all",)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    document_id: str
    chunk_id: str


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    chunk_id: str
    section: str
    rule_key: str | None
    keywords: tuple[str, ...]
    required_evidence_refs: tuple[EvidenceRef, ...]
    text: str


@dataclass(frozen=True, slots=True)
class RuleOverride:
    source_chunk_id: str
    target_document_id: str
    target_chunk_id: str
    rule_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    document_id: str
    title: str
    version_date: str
    effective_from: str
    effective_to: str | None
    topic: str
    scope: DocumentScope
    priority: int | None
    supersedes: tuple[str, ...]
    rule_overrides: tuple[RuleOverride, ...]
    keywords: tuple[str, ...]
    allowed_departments: tuple[str, ...]
    chunks: tuple[DocumentChunk, ...]


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: str
    title: str
    version_date: str
    effective_from: str
    effective_to: str | None
    topic: str
    scope_summary: str
    matched_chunk_ids: tuple[str, ...]
    matched_rule_keys: tuple[str, ...]
    score: float
    match_reasons: tuple[str, ...]
    preview: str


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    document_id: str
    chunk_id: str
    title: str
    version_date: str
    effective_from: str
    effective_to: str | None
    topic: str
    scope: DocumentScope
    rule_key: str | None
    required_evidence_refs: tuple[EvidenceRef, ...]
    blocked_dependency_count: int
    text: str


@dataclass(frozen=True, slots=True)
class AgentAction:
    action: ActionType
    arguments: dict[str, Any]
    rationale: str


@dataclass(slots=True)
class ToolObservation:
    tool_name: str
    ok: bool
    arguments: dict[str, Any]
    result_summary: str
    search_hits: list[SearchHit] = field(default_factory=list)
    evidence: Evidence | None = None
    error_code: str | None = None
    retryable: bool = False
    injected_failure: bool = False


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimSupportCertificate:
    claim_index: int
    claim_text: str
    evidence_ids: tuple[str, ...]
    proof_methods: tuple[str, ...]
    matched_source_clauses: tuple[str, ...]
    normalized_claim: str


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    document_id: str
    version_date: str
    effective_from: str
    effective_to: str | None
    chunk_id: str
    quote: str


@dataclass(frozen=True, slots=True)
class Conflict:
    conflict_id: str
    rule_key: str
    evidence_ids: tuple[str, ...]
    values: tuple[str, ...]
    description: str
    resolution_status: str
    resolution_basis: str | None = None
    selected_evidence_ids: tuple[str, ...] = ()
    scope_summaries: tuple[str, ...] = ()


@dataclass(slots=True)
class TraceStep:
    step: int
    action: str
    arguments: dict[str, Any]
    rationale: str
    observation_summary: str
    next_action: str | None
    decision_steps_remaining: int
    tool_calls_remaining: int


@dataclass(slots=True)
class AgentResult:
    status: TaskStatus
    answer: str
    claims: list[Claim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    support_certificates: list[ClaimSupportCertificate] = field(default_factory=list)
    tool_calls: int = 0
    decision_steps: int = 0
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentState:
    request: QueryRequest
    resolved_query: ResolvedQuery
    search_history: list[ToolObservation] = field(default_factory=list)
    candidate_hits: dict[str, SearchHit] = field(default_factory=dict)
    evidence_store: dict[str, Evidence] = field(default_factory=dict)
    tool_observations: list[ToolObservation] = field(default_factory=list)
    detected_conflicts: list[Conflict] = field(default_factory=list)
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    decision_steps_used: int = 0
    tool_calls_used: int = 0
    action_signatures: list[str] = field(default_factory=list)
    trace_steps: list[TraceStep] = field(default_factory=list)
