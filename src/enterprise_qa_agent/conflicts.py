"""Conservative conflict detection over normalized facts and rule stances."""

from __future__ import annotations

from itertools import combinations
import re

from .models import Conflict, DocumentScope, Evidence
from .repository import KnowledgeBase
from .semantic_facts import SemanticFact, describe_fact, extract_semantic_facts


_PROHIBITED = re.compile(
    r"不得|禁止|严禁|不适用|不允许|不予|不可|不能|无权|暂不|不再|"
    r"不支持|不接受|不提供|不安排|不批准|拒绝"
)
_NOT_REQUIRED = re.compile(r"无需|不必|不需要|不要求|可免于|免予|免除")
_PERMITTED = re.compile(r"可以|可申请|允许|适用|有权|享有|支持")
_REQUIRED = re.compile(r"必须|应当|应在|应于|须|需要|需在|需于|要求")

_FACT_LABELS = {
    "duration": "期限",
    "amount": "金额",
    "percentage": "比例",
}
_STANCE_LABELS = {
    "permitted": "允许",
    "prohibited": "禁止",
    "required": "必须",
    "not_required": "无需",
}
_OPPOSING_STANCES = {
    frozenset(("permitted", "prohibited")),
    frozenset(("required", "not_required")),
    frozenset(("required", "prohibited")),
}


class ConflictAnalyzer:
    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    def analyze(self, evidence_store: dict[str, Evidence], query: str = "") -> list[Conflict]:
        conflicts: list[Conflict] = []
        evidence = list(evidence_store.values())
        for left, right in combinations(evidence, 2):
            if not left.rule_key or left.rule_key != right.rule_key:
                continue
            if not _scope_overlaps(left.scope, right.scope):
                continue
            details = semantic_conflict_details(left.text, right.text)
            if details is None:
                continue
            dimensions, left_values, right_values = details
            resolved, basis, selected = self._resolve(left, right, query)
            conflicts.append(
                Conflict(
                    conflict_id=f"CF{len(conflicts) + 1}",
                    rule_key=left.rule_key,
                    evidence_ids=(left.evidence_id, right.evidence_id),
                    values=(" / ".join(left_values), " / ".join(right_values)),
                    description=(
                        f"{left.document_id} 与 {right.document_id} 对 {left.rule_key} "
                        f"在{'、'.join(dimensions)}维度给出不一致规则。"
                    ),
                    resolution_status="resolved" if resolved else "unresolved",
                    resolution_basis=basis,
                    selected_evidence_ids=selected,
                    scope_summaries=(
                        _scope_summary(left.scope),
                        _scope_summary(right.scope),
                    ),
                )
            )
        return conflicts

    def _resolve(
        self,
        left: Evidence,
        right: Evidence,
        query: str,
    ) -> tuple[bool, str | None, tuple[str, ...]]:
        left_doc = self.knowledge_base.get_document(left.document_id)
        right_doc = self.knowledge_base.get_document(right.document_id)
        if right.document_id in left_doc.supersedes:
            return True, f"{left.document_id} 明确替代 {right.document_id}", (left.evidence_id,)
        if left.document_id in right_doc.supersedes:
            return True, f"{right.document_id} 明确替代 {left.document_id}", (right.evidence_id,)

        special_relation = self._special_override_resolution(left, right, query)
        if special_relation is not None:
            return special_relation

        if (
            _scopes_equivalent(left.scope, right.scope)
            and
            left_doc.priority is not None
            and right_doc.priority is not None
            and left_doc.priority != right_doc.priority
        ):
            selected = left if left_doc.priority > right_doc.priority else right
            return True, f"文档显式优先级选择 {selected.document_id}", (selected.evidence_id,)
        return False, None, ()

    def _special_override_resolution(
        self,
        left: Evidence,
        right: Evidence,
        query: str,
    ) -> tuple[bool, str | None, tuple[str, ...]] | None:
        for special, general in ((left, right), (right, left)):
            special_doc = self.knowledge_base.get_document(special.document_id)
            general_doc = self.knowledge_base.get_document(general.document_id)
            override = next(
                (
                    item
                    for item in special_doc.rule_overrides
                    if item.source_chunk_id == special.chunk_id
                    and item.target_document_id == general.document_id
                    and item.target_chunk_id == general.chunk_id
                    and item.rule_key == special.rule_key
                ),
                None,
            )
            if override is None:
                continue
            matched_scope = _matched_narrow_scope(query, special.scope, general.scope)
            if matched_scope is None:
                return (
                    False,
                    (
                        f"{special.document_id}/{special.chunk_id} 声明为特例，但问题未明确命中其"
                        "适用范围，不能自动覆盖通用条款"
                    ),
                    (),
                )
            return (
                True,
                (
                    f"{special.document_id}/{special.chunk_id} 显式覆盖 "
                    f"{general.document_id}/{general.chunk_id}；问题命中特例范围 "
                    f"{matched_scope}；依据：{override.reason}"
                ),
                (special.evidence_id,),
            )
        return None


def semantic_conflict_details(
    left_text: str,
    right_text: str,
) -> tuple[list[str], list[str], list[str]] | None:
    """Return only comparable, demonstrably conflicting semantic dimensions."""

    left_groups = _facts_by_kind(extract_semantic_facts(left_text))
    right_groups = _facts_by_kind(extract_semantic_facts(right_text))
    dimensions: list[str] = []
    left_values: list[str] = []
    right_values: list[str] = []

    for kind in sorted(left_groups.keys() & right_groups.keys()):
        if left_groups[kind] == right_groups[kind]:
            continue
        label = _FACT_LABELS.get(kind, kind)
        dimensions.append(label)
        left_values.append(f"{label}: " + ", ".join(describe_fact(item) for item in left_groups[kind]))
        right_values.append(f"{label}: " + ", ".join(describe_fact(item) for item in right_groups[kind]))

    left_stances = _extract_stances(left_text)
    right_stances = _extract_stances(right_text)
    if _stances_conflict(left_stances, right_stances):
        dimensions.append("规则立场")
        left_values.append("规则立场: " + _STANCE_LABELS[next(iter(left_stances))])
        right_values.append("规则立场: " + _STANCE_LABELS[next(iter(right_stances))])

    if not dimensions:
        return None
    return dimensions, left_values, right_values


def _facts_by_kind(facts: set[SemanticFact]) -> dict[str, tuple[SemanticFact, ...]]:
    grouped: dict[str, list[SemanticFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.kind, []).append(fact)
    return {kind: tuple(sorted(values)) for kind, values in grouped.items()}


def _extract_stances(text: str) -> set[str]:
    stances: set[str] = set()
    if _PROHIBITED.search(text):
        stances.add("prohibited")
    if _NOT_REQUIRED.search(text):
        stances.add("not_required")

    # Remove negative phrases before finding their positive substrings, e.g.
    # “不允许” must not also become “允许”, and “不需要” must not become “需要”.
    affirmative_text = _NOT_REQUIRED.sub(" ", _PROHIBITED.sub(" ", text))
    if _PERMITTED.search(affirmative_text):
        stances.add("permitted")
    if _REQUIRED.search(affirmative_text):
        stances.add("required")
    return stances


def _stances_conflict(left: set[str], right: set[str]) -> bool:
    # Multiple stances usually mean that one chunk contains different clauses
    # or populations. Without clause-level scope, automatic conflict would be
    # unsafe, so leave it for clarification/model review.
    if len(left) != 1 or len(right) != 1:
        return False
    return frozenset((next(iter(left)), next(iter(right)))) in _OPPOSING_STANCES


def _scope_overlaps(left: DocumentScope, right: DocumentScope) -> bool:
    return all(
        _dimension_overlaps(left_values, right_values)
        for left_values, right_values in (
            (left.employee_types, right.employee_types),
            (left.regions, right.regions),
            (left.business_types, right.business_types),
        )
    )


def _dimension_overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return "all" in left or "all" in right or bool(set(left) & set(right))


def _scope_summary(scope: DocumentScope) -> str:
    def render(values: tuple[str, ...]) -> str:
        return "全部" if "all" in values else ",".join(values)

    return (
        f"员工={render(scope.employee_types)};"
        f"地区={render(scope.regions)};"
        f"业务={render(scope.business_types)}"
    )


def _scopes_equivalent(left: DocumentScope, right: DocumentScope) -> bool:
    return all(
        set(left_values) == set(right_values)
        for left_values, right_values in (
            (left.employee_types, right.employee_types),
            (left.regions, right.regions),
            (left.business_types, right.business_types),
        )
    )


def _matched_narrow_scope(
    query: str,
    special: DocumentScope,
    general: DocumentScope,
) -> str | None:
    normalized_query = query.lower()
    matched: list[str] = []
    for label, special_values, general_values in (
        ("员工", special.employee_types, general.employee_types),
        ("地区", special.regions, general.regions),
        ("业务", special.business_types, general.business_types),
    ):
        if set(special_values) == set(general_values):
            continue
        concrete_values = [value for value in special_values if value != "all"]
        hits = [value for value in concrete_values if value.lower() in normalized_query]
        if not hits:
            return None
        matched.append(f"{label}={','.join(hits)}")
    return ";".join(matched) if matched else None
