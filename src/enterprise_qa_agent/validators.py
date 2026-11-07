"""Non-bypassable completion and evidence validation guards."""

from __future__ import annotations

from itertools import combinations
import re

from .conflicts import semantic_conflict_details
from .models import AgentState, Claim
from .repository import KnowledgeBase
from .semantic_facts import describe_fact, extract_semantic_facts
from .support_proof import certify_claim_support


_FACT_TOKEN = re.compile(r"\d+(?:\.\d+)?%?|[a-zA-Z]{3,}")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


class CompletionValidator:
    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        conflict_margin: float = 0.15,
        *,
        max_claims: int = 6,
        max_claim_chars: int = 240,
        max_total_claim_chars: int = 800,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.conflict_margin = conflict_margin
        self.max_claims = max_claims
        self.max_claim_chars = max_claim_chars
        self.max_total_claim_chars = max_total_claim_chars

    def validate_completed(self, state: AgentState, claims: list[Claim]) -> list[dict[str, object]]:
        errors: list[dict[str, object]] = []
        if not state.search_history or not any(item.ok for item in state.search_history):
            errors.append({"code": "search_required", "message": "完成前必须成功检索资料。"})
        if not state.evidence_store:
            errors.append({"code": "evidence_required", "message": "完成前必须读取原文。"})
        if not claims:
            errors.append({"code": "claims_required", "message": "成功回答至少需要一条事实声明。"})
        blocked_dependencies = sum(
            item.blocked_dependency_count for item in state.evidence_store.values()
        )
        if blocked_dependencies:
            errors.append(
                {
                    "code": "required_evidence_unavailable",
                    "message": (
                        f"有 {blocked_dependencies} 条必要关联证据在当前权限或规则有效期下不可用，"
                        "不能完成回答。"
                    ),
                }
            )
        if len(claims) > self.max_claims:
            errors.append(
                {
                    "code": "answer_too_long",
                    "message": f"回答声明数量超过上限 {self.max_claims}。",
                }
            )
        total_claim_chars = sum(len(claim.text) for claim in claims)
        if total_claim_chars > self.max_total_claim_chars:
            errors.append(
                {
                    "code": "answer_too_long",
                    "message": f"回答事实文本总长度超过上限 {self.max_total_claim_chars} 字符。",
                }
            )

        for index, claim in enumerate(claims, 1):
            if len(claim.text) > self.max_claim_chars:
                errors.append(
                    {
                        "code": "answer_too_long",
                        "message": (
                            f"声明 {index} 长度超过上限 {self.max_claim_chars} 字符，"
                            "请保留直接回答所需的最小事实。"
                        ),
                    }
                )
            if not claim.text.strip() or not claim.evidence_ids:
                errors.append({"code": "claim_missing_evidence", "message": f"声明 {index} 缺少证据。"})
                continue
            evidence = [state.evidence_store.get(item) for item in claim.evidence_ids]
            if any(item is None for item in evidence):
                errors.append({"code": "unknown_evidence", "message": f"声明 {index} 引用了不存在的证据。"})
                continue
            source_text = " ".join(item.text for item in evidence if item is not None)
            claim_facts = set(_FACT_TOKEN.findall(claim.text))
            source_facts = set(_FACT_TOKEN.findall(source_text))
            if not claim_facts.issubset(source_facts):
                errors.append(
                    {
                        "code": "unsupported_fact_token",
                        "message": f"声明 {index} 含原文未出现的数字、百分比或英文事实。",
                    }
                )
            claim_semantic_facts = extract_semantic_facts(claim.text)
            source_semantic_facts = extract_semantic_facts(source_text)
            unsupported_semantic_facts = claim_semantic_facts - source_semantic_facts
            if unsupported_semantic_facts:
                errors.append(
                    {
                        "code": "semantic_fact_mismatch",
                        "message": (
                            f"声明 {index} 含原文不支持的语义事实："
                            + "、".join(
                                describe_fact(item)
                                for item in sorted(unsupported_semantic_facts)
                            )
                            + "。"
                        ),
                    }
                )
            claim_support_tokens = _support_tokens(claim.text)
            source_support_tokens = _support_tokens(source_text)
            support_ratio = len(claim_support_tokens & source_support_tokens) / max(len(claim_support_tokens), 1)
            if claim_support_tokens and support_ratio < 0.35:
                errors.append(
                    {
                        "code": "low_textual_support",
                        "message": f"声明 {index} 与绑定原文的最低文本支持度不足。",
                    }
                )

        _, proof_errors = certify_claim_support(state, claims)
        errors.extend(proof_errors)

        unresolved = [item.conflict_id for item in state.detected_conflicts if item.resolution_status == "unresolved"]
        if unresolved:
            errors.append(
                {
                    "code": "unresolved_conflict",
                    "message": "存在未裁决冲突，不能完成回答。",
                    "conflict_ids": unresolved,
                }
            )
        errors.extend(self._missing_required_citations(state, claims))
        errors.extend(_claim_consistency_errors(state, claims))
        errors.extend(self._unread_required_chunks(state))
        errors.extend(self._unread_conflict_candidates(state))
        return errors

    def _unread_required_chunks(self, state: AgentState) -> list[dict[str, object]]:
        read_chunks = {
            (evidence.document_id, evidence.chunk_id)
            for evidence in state.evidence_store.values()
        }
        errors: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for evidence in state.evidence_store.values():
            for reference in evidence.required_evidence_refs:
                key = (reference.document_id, reference.chunk_id)
                if key in read_chunks or key in seen:
                    continue
                seen.add(key)
                errors.append(
                    {
                        "code": "unread_required_chunk",
                        "message": (
                            f"{evidence.document_id}/{evidence.chunk_id} 依赖关联片段 "
                            f"{reference.document_id}/{reference.chunk_id}，完成前必须读取。"
                        ),
                        "document_id": reference.document_id,
                        "chunk_id": reference.chunk_id,
                    }
                )
        return errors

    def _missing_required_citations(
        self,
        state: AgentState,
        claims: list[Claim],
    ) -> list[dict[str, object]]:
        cited_evidence = [
            state.evidence_store[evidence_id]
            for claim in claims
            for evidence_id in claim.evidence_ids
            if evidence_id in state.evidence_store
        ]
        cited_chunks = {
            (evidence.document_id, evidence.chunk_id)
            for evidence in cited_evidence
        }
        errors: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for evidence in cited_evidence:
            for reference in evidence.required_evidence_refs:
                key = (reference.document_id, reference.chunk_id)
                if key in cited_chunks or key in seen:
                    continue
                seen.add(key)
                errors.append(
                    {
                        "code": "missing_required_citation",
                        "message": (
                            f"声明引用了 {evidence.document_id}/{evidence.chunk_id}，但未引用其"
                            f"关联证据 {reference.document_id}/{reference.chunk_id}。"
                        ),
                        "document_id": reference.document_id,
                        "chunk_id": reference.chunk_id,
                    }
                )
        return errors

    def _unread_conflict_candidates(self, state: AgentState) -> list[dict[str, object]]:
        if not state.candidate_hits or not state.evidence_store:
            return []
        top_score = max(hit.score for hit in state.candidate_hits.values())
        read_chunks = {item.chunk_id for item in state.evidence_store.values()}
        read_rule_keys = {item.rule_key for item in state.evidence_store.values() if item.rule_key}
        errors: list[dict[str, object]] = []
        for hit in state.candidate_hits.values():
            if hit.score < top_score - self.conflict_margin:
                continue
            if not read_rule_keys.intersection(hit.matched_rule_keys):
                continue
            for chunk_id in hit.matched_chunk_ids:
                chunk = self.knowledge_base.get_chunk(hit.document_id, chunk_id)
                if chunk.rule_key in read_rule_keys and chunk_id not in read_chunks:
                    errors.append(
                        {
                            "code": "unread_conflict_candidate",
                            "message": f"完成前需要读取同规则候选 {hit.document_id}/{chunk_id}。",
                            "document_id": hit.document_id,
                            "chunk_id": chunk_id,
                        }
                    )
                    break
        return errors

    def validate_insufficient_evidence(self, state: AgentState) -> list[dict[str, object]]:
        unresolved = [
            item.conflict_id
            for item in state.detected_conflicts
            if item.resolution_status == "unresolved"
        ]
        if unresolved:
            return [
                {
                    "code": "conflict_requires_clarification",
                    "message": (
                        "已有充分原文但存在未裁决冲突；必须调用 ask_user 请求确认，"
                        "不得用 insufficient_evidence 结束。"
                    ),
                    "conflict_ids": unresolved,
                }
            ]
        if any(
            evidence.blocked_dependency_count > 0
            for evidence in state.evidence_store.values()
        ):
            return []
        empty_queries = {
            item.arguments.get("query")
            for item in state.search_history
            if item.ok and not item.search_hits
        }
        if len(empty_queries) < 2:
            return [
                {
                    "code": "supplementary_search_required",
                    "message": "报告资料不足前需要执行至少两个不同查询。",
                }
            ]
        return []

    def validate_tool_error(self, state: AgentState) -> list[dict[str, object]]:
        failures = [item for item in state.tool_observations if not item.ok]
        if len(failures) < 2:
            return [
                {
                    "code": "tool_retry_required",
                    "message": "报告工具失败前需要由模型观察错误后至少重试一次。",
                }
            ]
        return []


def _support_tokens(text: str) -> set[str]:
    tokens = {item.lower() for item in re.findall(r"[a-zA-Z0-9]+", text)}
    for run in _CJK_RUN.findall(text):
        tokens.update(run[index : index + 2] for index in range(max(len(run) - 1, 0)))
    return tokens


def _claim_consistency_errors(
    state: AgentState,
    claims: list[Claim],
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []

    for index, claim in enumerate(claims, 1):
        rule_keys = _claim_rule_keys(state, claim)
        if not rule_keys:
            continue
        segments = [
            item.strip()
            for item in re.split(r"[。；;]|但是|但|同时|另一方面", claim.text)
            if item.strip()
        ]
        for left, right in combinations(segments, 2):
            details = semantic_conflict_details(left, right)
            if details is None or _explicit_scopes_are_disjoint(state, claim, left, claim, right):
                continue
            dimensions, left_values, right_values = details
            errors.append(
                _self_contradiction_error(
                    (index,),
                    rule_keys,
                    dimensions,
                    left_values,
                    right_values,
                )
            )
            break

    for (left_index, left), (right_index, right) in combinations(enumerate(claims, 1), 2):
        shared_rule_keys = _claim_rule_keys(state, left) & _claim_rule_keys(state, right)
        if not shared_rule_keys:
            continue
        details = semantic_conflict_details(left.text, right.text)
        if details is None or _explicit_scopes_are_disjoint(
            state,
            left,
            left.text,
            right,
            right.text,
        ):
            continue
        dimensions, left_values, right_values = details
        errors.append(
            _self_contradiction_error(
                (left_index, right_index),
                shared_rule_keys,
                dimensions,
                left_values,
                right_values,
            )
        )
    return errors


def _claim_rule_keys(state: AgentState, claim: Claim) -> set[str]:
    return {
        evidence.rule_key
        for evidence_id in claim.evidence_ids
        if (evidence := state.evidence_store.get(evidence_id)) is not None
        and evidence.rule_key is not None
    }


def _explicit_scopes_are_disjoint(
    state: AgentState,
    left_claim: Claim,
    left_text: str,
    right_claim: Claim,
    right_text: str,
) -> bool:
    values_by_dimension = [set(), set(), set()]
    for claim in (left_claim, right_claim):
        for evidence_id in claim.evidence_ids:
            evidence = state.evidence_store.get(evidence_id)
            if evidence is None:
                continue
            for values, scope_values in zip(
                values_by_dimension,
                (
                    evidence.scope.employee_types,
                    evidence.scope.regions,
                    evidence.scope.business_types,
                ),
                strict=True,
            ):
                values.update(value for value in scope_values if value != "all")

    for known_values in values_by_dimension:
        left_matches = {value for value in known_values if value in left_text}
        right_matches = {value for value in known_values if value in right_text}
        if left_matches and right_matches and left_matches.isdisjoint(right_matches):
            return True
    return False


def _self_contradiction_error(
    claim_indexes: tuple[int, ...],
    rule_keys: set[str],
    dimensions: list[str],
    left_values: list[str],
    right_values: list[str],
) -> dict[str, object]:
    location = "、".join(str(item) for item in claim_indexes)
    return {
        "code": "answer_self_contradiction",
        "message": (
            f"回答声明 {location} 对同一规则给出互斥结论："
            f"{' / '.join(left_values)} 与 {' / '.join(right_values)}。"
        ),
        "claim_indexes": list(claim_indexes),
        "rule_keys": sorted(rule_keys),
        "dimensions": dimensions,
    }
