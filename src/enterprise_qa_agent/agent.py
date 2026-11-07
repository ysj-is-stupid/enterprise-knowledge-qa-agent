"""ReAct executor coordinating model decisions, tools, and safety guards."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import time
from typing import Any, Callable

from .config import AgentConfig
from .conflicts import ConflictAnalyzer
from .models import (
    ActionType,
    AgentAction,
    AgentResult,
    AgentState,
    ChatMessage,
    Citation,
    Claim,
    ClaimSupportCertificate,
    Conflict,
    QueryRequest,
    RouteType,
    SearchHit,
    TaskStatus,
    ToolObservation,
    TraceStep,
)
from .policy import DecisionModel, ModelError
from .repository import KnowledgeBase
from .tools import KnowledgeTools
from .validators import CompletionValidator
from .support_proof import certify_claim_support


class EnterpriseQAAgent:
    def __init__(
        self,
        config: AgentConfig,
        model: DecisionModel,
        knowledge_base: KnowledgeBase,
        tools: KnowledgeTools,
        retry_sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.model = model
        self.knowledge_base = knowledge_base
        self.tools = tools
        self.retry_sleeper = retry_sleeper
        self.conflicts = ConflictAnalyzer(knowledge_base)
        self.validator = CompletionValidator(
            knowledge_base,
            config.conflict_score_margin,
            max_claims=config.max_claims,
            max_claim_chars=config.max_claim_chars,
            max_total_claim_chars=config.max_total_claim_chars,
        )

    def run(self, request: QueryRequest) -> AgentResult:
        try:
            resolved = self.model.resolve_query(request)
        except ModelError as exc:
            return AgentResult(TaskStatus.TOOL_ERROR, f"查询解析模型不可用：{exc}", gaps=["无法生成独立查询。"])

        if resolved.route == RouteType.INVALID_INPUT:
            return self._early_result(TaskStatus.INVALID_INPUT, "问题不能为空。", request, resolved)
        if resolved.route == RouteType.NON_QUESTION:
            return self._early_result(TaskStatus.NOT_APPLICABLE, "当前输入不是企业知识问题。", request, resolved)

        state = AgentState(request=request, resolved_query=resolved)
        while state.decision_steps_used < self.config.max_decision_steps:
            try:
                action = self.model.next_action(state)
            except ModelError as exc:
                state.validation_errors = [{"code": "model_error", "message": str(exc)}]
                return self._finalize(
                    state,
                    TaskStatus.TOOL_ERROR,
                    "决策模型不可用，无法继续核验资料。",
                    gaps=[f"模型动作生成失败：{exc}"],
                )

            if state.trace_steps:
                state.trace_steps[-1].next_action = action.action.value
            state.decision_steps_used += 1
            state.action_signatures.append(_action_signature(action))

            if action.action in {ActionType.SEARCH_DOCS, ActionType.READ_DOC}:
                result = self._execute_tool_action(state, action)
                if result is not None:
                    return result
                continue
            if action.action == ActionType.ASK_USER:
                self._append_trace(state, action, "需要用户补充信息，当前任务停止。")
                return self._clarification_result(state, action)
            if action.action == ActionType.FINISH:
                result = self._handle_finish(state, action)
                if result is not None:
                    return result
                continue
            self._validation_observation(state, action, "unknown_action", "未知动作。")

        return self._budget_exceeded_result(state, "达到模型决策步数上限，任务未完成。")

    def _execute_tool_action(self, state: AgentState, action: AgentAction) -> AgentResult | None:
        if state.tool_calls_used >= self.config.max_tool_calls:
            return self._budget_exceeded_result(state, "达到工具调用次数上限，任务未完成。")
        observation: ToolObservation
        retry_delay = self._retry_delay(state, action)
        if retry_delay > 0:
            self.retry_sleeper(retry_delay)
        if action.action == ActionType.SEARCH_DOCS:
            query = action.arguments.get("query")
            top_k = action.arguments.get("top_k", self.config.search_top_k)
            if not isinstance(query, str) or not query.strip() or not isinstance(top_k, int) or not 1 <= top_k <= 10:
                self._validation_observation(state, action, "invalid_search_action", "检索动作参数无效。")
                return None
            observation = self.tools.search_docs(
                query,
                top_k,
                state.request.requester_departments,
                state.request.as_of_date,
            )
            state.search_history.append(observation)
            if observation.ok:
                for hit in observation.search_hits:
                    previous = state.candidate_hits.get(hit.document_id)
                    state.candidate_hits[hit.document_id] = (
                        hit if previous is None else _merge_search_hits(previous, hit)
                    )
        else:
            document_id = action.arguments.get("document_id")
            chunk_id = action.arguments.get("chunk_id")
            if not isinstance(document_id, str) or not isinstance(chunk_id, str):
                self._validation_observation(state, action, "invalid_read_action", "原文读取动作参数无效。")
                return None
            hit = state.candidate_hits.get(document_id)
            if hit is None or chunk_id not in hit.matched_chunk_ids:
                self._validation_observation(
                    state,
                    action,
                    "read_target_not_searched",
                    "只能读取检索结果中的文档片段。",
                )
                return None
            observation = self.tools.read_doc(
                document_id,
                chunk_id,
                state.request.requester_departments,
                state.request.as_of_date,
            )
            if observation.ok and observation.evidence is not None:
                state.evidence_store[observation.evidence.evidence_id] = observation.evidence
                state.detected_conflicts = self.conflicts.analyze(
                    state.evidence_store,
                    state.resolved_query.standalone_query,
                )

        if retry_delay > 0:
            observation.result_summary = (
                f"退避 {retry_delay:.2f} 秒后重试；{observation.result_summary}"
            )

        state.tool_calls_used += 1
        state.tool_observations.append(observation)
        state.validation_errors.clear()
        self._append_trace(state, action, observation.result_summary)
        return None

    def _retry_delay(self, state: AgentState, action: AgentAction) -> float:
        if not state.tool_observations:
            return 0.0
        last = state.tool_observations[-1]
        if last.ok or not last.retryable or last.tool_name != action.action.value:
            return 0.0
        consecutive_failures = 0
        for observation in reversed(state.tool_observations):
            if (
                observation.ok
                or not observation.retryable
                or observation.tool_name != action.action.value
            ):
                break
            consecutive_failures += 1
        delay = self.config.retry_base_delay_seconds * (2 ** max(consecutive_failures - 1, 0))
        return min(delay, self.config.retry_max_delay_seconds)

    def _handle_finish(self, state: AgentState, action: AgentAction) -> AgentResult | None:
        status = action.arguments.get("status")
        claims = _parse_claims(action.arguments.get("claims", []))
        gaps = [str(item) for item in action.arguments.get("gaps", [])]

        if status == TaskStatus.COMPLETED:
            errors = self.validator.validate_completed(state, claims)
            if errors:
                state.validation_errors = errors
                self._append_trace(state, action, _validation_summary(errors))
                return None
            support_certificates, proof_errors = certify_claim_support(state, claims)
            if proof_errors:
                state.validation_errors = proof_errors
                self._append_trace(state, action, _validation_summary(proof_errors))
                return None
            self._append_trace(
                state,
                action,
                f"完成校验通过；{len(support_certificates)} 条声明均生成逐句支持证书。",
            )
            return self._completed_result(state, claims, gaps, support_certificates)
        if status == TaskStatus.INSUFFICIENT_EVIDENCE:
            errors = self.validator.validate_insufficient_evidence(state)
            if errors:
                state.validation_errors = errors
                self._append_trace(state, action, _validation_summary(errors))
                return None
            self._append_trace(state, action, "资料不足终止条件通过。")
            return self._finalize(
                state,
                TaskStatus.INSUFFICIENT_EVIDENCE,
                "现有知识库没有足够资料回答该问题。",
                gaps=gaps or ["缺少相关制度或产品资料。"],
            )
        if status == TaskStatus.TOOL_ERROR:
            errors = self.validator.validate_tool_error(state)
            if errors:
                state.validation_errors = errors
                self._append_trace(state, action, _validation_summary(errors))
                return None
            self._append_trace(state, action, "工具失败终止条件通过。")
            return self._finalize(
                state,
                TaskStatus.TOOL_ERROR,
                "关键知识工具持续失败，无法核验资料；未凭模型记忆作答。",
                gaps=gaps or ["知识工具当前不可用。"],
            )
        self._validation_observation(state, action, "invalid_finish_status", "模型请求了不允许的完成状态。")
        return None

    def _completed_result(
        self,
        state: AgentState,
        claims: list[Claim],
        gaps: list[str],
        support_certificates: list[ClaimSupportCertificate],
    ) -> AgentResult:
        cited_evidence_ids: list[str] = []
        for claim in claims:
            for evidence_id in claim.evidence_ids:
                if evidence_id not in cited_evidence_ids:
                    cited_evidence_ids.append(evidence_id)
        for conflict in state.detected_conflicts:
            for evidence_id in conflict.evidence_ids:
                if evidence_id not in cited_evidence_ids:
                    cited_evidence_ids.append(evidence_id)
        citations, evidence_to_citation = _citations_for_evidence(state, cited_evidence_ids)
        output_claims = [
            Claim(
                claim.text,
                tuple(evidence_to_citation[item] for item in claim.evidence_ids),
            )
            for claim in claims
        ]
        output_certificates = [
            ClaimSupportCertificate(
                claim_index=item.claim_index,
                claim_text=item.claim_text,
                evidence_ids=tuple(evidence_to_citation[value] for value in item.evidence_ids),
                proof_methods=item.proof_methods,
                matched_source_clauses=item.matched_source_clauses,
                normalized_claim=item.normalized_claim,
            )
            for item in support_certificates
        ]
        answer_parts: list[str] = []
        for claim in claims:
            markers = "".join(f"[{evidence_to_citation[item]}]" for item in claim.evidence_ids)
            answer_parts.append(f"{claim.text}{markers}")
        resolved_basis = [
            item.resolution_basis
            for item in state.detected_conflicts
            if item.resolution_status == "resolved" and item.resolution_basis
        ]
        if resolved_basis:
            answer_parts.append("冲突裁决依据：" + "；".join(resolved_basis))
        return self._finalize(
            state,
            TaskStatus.COMPLETED,
            " ".join(answer_parts),
            claims=output_claims,
            citations=citations,
            gaps=gaps,
            conflicts=_map_conflicts_to_citations(state.detected_conflicts, evidence_to_citation),
            support_certificates=output_certificates,
        )

    def _clarification_result(self, state: AgentState, action: AgentAction) -> AgentResult:
        evidence_ids: list[str] = []
        for conflict in state.detected_conflicts:
            if conflict.resolution_status == "unresolved":
                evidence_ids.extend(conflict.evidence_ids)
        citations, evidence_to_citation = _citations_for_evidence(state, evidence_ids)
        conflict_parts: list[str] = []
        for conflict in state.detected_conflicts:
            if conflict.resolution_status != "unresolved":
                continue
            items = []
            for index, evidence_id in enumerate(conflict.evidence_ids):
                evidence = state.evidence_store[evidence_id]
                scope = (
                    conflict.scope_summaries[index]
                    if index < len(conflict.scope_summaries)
                    else "适用范围未知"
                )
                items.append(
                    f"{evidence.document_id}（适用范围：{scope}）："
                    f"{evidence.text}[{evidence_to_citation[evidence_id]}]"
                )
            conflict_parts.append("；".join(items))
        question = str(action.arguments.get("question", "请确认适用制度或业务范围。"))
        answer = "发现无法自动裁决的资料冲突：" + " ".join(conflict_parts) + " " + question
        return self._finalize(
            state,
            TaskStatus.NEEDS_CLARIFICATION,
            answer,
            citations=citations,
            gaps=[str(item) for item in action.arguments.get("gap_codes", [])],
            conflicts=_map_conflicts_to_citations(state.detected_conflicts, evidence_to_citation),
        )

    def _validation_observation(
        self,
        state: AgentState,
        action: AgentAction,
        code: str,
        message: str,
    ) -> None:
        state.validation_errors = [{"code": code, "message": message}]
        self._append_trace(state, action, f"动作校验失败: {message}")

    def _budget_exceeded_result(self, state: AgentState, reason: str) -> AgentResult:
        evidence_ids = list(state.evidence_store)
        citations, evidence_to_citation = _citations_for_evidence(state, evidence_ids)
        answer = reason
        gaps = ["执行预算已耗尽，尚未完成全部核验。"]
        if citations:
            markers = "、".join(f"[{item.citation_id}]" for item in citations)
            answer += (
                f" 已收集 {len(citations)} 条原文证据（{markers}），"
                "但这些仅是中间结果，不构成最终答案。"
            )
            gaps.append("需要继续检查关联资料、冲突和引用支持后才能形成结论。")
        elif state.candidate_hits:
            answer += (
                f" 已找到 {len(state.candidate_hits)} 份候选资料，但尚未形成可核验答案。"
            )
            gaps.append("候选资料尚未全部读取。")
        else:
            gaps.append("尚未收集到可引用原文。")
        return self._finalize(
            state,
            TaskStatus.MAX_STEPS_EXCEEDED,
            answer,
            citations=citations,
            gaps=gaps,
            conflicts=_map_conflicts_to_citations(
                state.detected_conflicts,
                evidence_to_citation,
            ),
        )

    def _append_trace(self, state: AgentState, action: AgentAction, observation: str) -> None:
        state.trace_steps.append(
            TraceStep(
                step=state.decision_steps_used,
                action=action.action.value,
                arguments=dict(action.arguments),
                rationale=action.rationale,
                observation_summary=observation,
                next_action=None,
                decision_steps_remaining=max(self.config.max_decision_steps - state.decision_steps_used, 0),
                tool_calls_remaining=max(self.config.max_tool_calls - state.tool_calls_used, 0),
            )
        )

    def _early_result(self, status: TaskStatus, answer: str, request: QueryRequest, resolved: Any) -> AgentResult:
        trace = {
            "trace_version": "1.0",
            "mode": self.model.mode_name,
            "original_question": request.question,
            "access_context": {
                "departments": list(request.requester_departments),
                "as_of_date": request.as_of_date,
            },
            "standalone_query": resolved.standalone_query,
            "route": resolved.route.value,
            "history_context": {
                "memory_strategy": resolved.memory_strategy,
                "retrieved_message_indexes": list(resolved.retrieved_history_indexes),
                "recent_message_indexes": list(resolved.recent_history_indexes),
            },
            "steps": [],
            "final_status": status.value,
        }
        trace["run_id"] = _trace_run_id(trace)
        return AgentResult(
            status=status,
            answer=answer,
            trace=trace,
        )

    def _finalize(
        self,
        state: AgentState,
        status: TaskStatus,
        answer: str,
        *,
        claims: list[Claim] | None = None,
        citations: list[Citation] | None = None,
        gaps: list[str] | None = None,
        conflicts: list[Conflict] | None = None,
        support_certificates: list[ClaimSupportCertificate] | None = None,
    ) -> AgentResult:
        trace = {
            "trace_version": "1.0",
            "mode": self.model.mode_name,
            "original_question": state.resolved_query.original_question,
            "standalone_query": state.resolved_query.standalone_query,
            "route": state.resolved_query.route.value,
            "access_context": {
                "departments": list(state.request.requester_departments),
                "as_of_date": state.request.as_of_date,
            },
            "resolution_note": state.resolved_query.resolution_note,
            "history_context": {
                "memory_strategy": state.resolved_query.memory_strategy,
                "retrieved_message_indexes": list(
                    state.resolved_query.retrieved_history_indexes
                ),
                "recent_message_indexes": list(state.resolved_query.recent_history_indexes),
            },
            "limits": {
                "max_decision_steps": self.config.max_decision_steps,
                "max_tool_calls": self.config.max_tool_calls,
            },
            "steps": [asdict(item) for item in state.trace_steps],
            "final_status": status.value,
            "tool_calls_used": state.tool_calls_used,
            "decision_steps_used": state.decision_steps_used,
        }
        trace["run_id"] = _trace_run_id(trace)
        return AgentResult(
            status=status,
            answer=answer,
            claims=claims or [],
            citations=citations or [],
            gaps=gaps or [],
            conflicts=list(state.detected_conflicts) if conflicts is None else conflicts,
            support_certificates=support_certificates or [],
            tool_calls=state.tool_calls_used,
            decision_steps=state.decision_steps_used,
            trace=trace,
        )


def result_to_dict(result: AgentResult) -> dict[str, Any]:
    claims = [
        {"text": claim.text, "citation_ids": list(claim.evidence_ids)}
        for claim in result.claims
    ]
    return {
        "status": result.status.value,
        "answer": result.answer,
        "claims": claims,
        "citations": [asdict(item) for item in result.citations],
        "gaps": result.gaps,
        "conflicts": [asdict(item) for item in result.conflicts],
        "support_certificates": [asdict(item) for item in result.support_certificates],
        "tool_calls": result.tool_calls,
        "decision_steps": result.decision_steps,
        "trace": result.trace,
    }


def result_to_json(result: AgentResult) -> str:
    return json.dumps(result_to_dict(result), ensure_ascii=False, indent=2)


def _parse_claims(raw: Any) -> list[Claim]:
    if not isinstance(raw, list):
        return []
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        evidence_ids = item.get("evidence_ids")
        if isinstance(text, str) and isinstance(evidence_ids, (list, tuple)):
            claims.append(Claim(text.strip(), tuple(str(value) for value in evidence_ids)))
    return claims


def _citations_for_claims(
    state: AgentState,
    claims: list[Claim],
) -> tuple[list[Citation], dict[str, str]]:
    evidence_ids: list[str] = []
    for claim in claims:
        for evidence_id in claim.evidence_ids:
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
    return _citations_for_evidence(state, evidence_ids)


def _citations_for_evidence(
    state: AgentState,
    evidence_ids: list[str],
) -> tuple[list[Citation], dict[str, str]]:
    citations: list[Citation] = []
    mapping: dict[str, str] = {}
    for evidence_id in evidence_ids:
        if evidence_id in mapping or evidence_id not in state.evidence_store:
            continue
        evidence = state.evidence_store[evidence_id]
        citation_id = f"C{len(citations) + 1}"
        mapping[evidence_id] = citation_id
        citations.append(
            Citation(
                citation_id=citation_id,
                document_id=evidence.document_id,
                version_date=evidence.version_date,
                effective_from=evidence.effective_from,
                effective_to=evidence.effective_to,
                chunk_id=evidence.chunk_id,
                quote=evidence.text,
            )
        )
    return citations, mapping


def _action_signature(action: AgentAction) -> str:
    return json.dumps(
        {"action": action.action.value, "arguments": action.arguments},
        ensure_ascii=False,
        sort_keys=True,
    )


def _merge_search_hits(left: SearchHit, right: SearchHit) -> SearchHit:
    base = right if right.score > left.score else left
    return replace(
        base,
        score=max(left.score, right.score),
        matched_chunk_ids=tuple(
            dict.fromkeys((*left.matched_chunk_ids, *right.matched_chunk_ids))
        ),
        matched_rule_keys=tuple(
            sorted({*left.matched_rule_keys, *right.matched_rule_keys})
        ),
        match_reasons=tuple(dict.fromkeys((*left.match_reasons, *right.match_reasons))),
    )


def _validation_summary(errors: list[dict[str, object]]) -> str:
    return "完成校验未通过: " + "; ".join(str(item.get("message", item.get("code"))) for item in errors)


def _trace_run_id(trace: dict[str, Any]) -> str:
    encoded = json.dumps(trace, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "run_" + hashlib.sha256(encoded).hexdigest()[:16]


def _map_conflicts_to_citations(
    conflicts: list[Conflict],
    evidence_to_citation: dict[str, str],
) -> list[Conflict]:
    return [
        Conflict(
            conflict_id=item.conflict_id,
            rule_key=item.rule_key,
            evidence_ids=tuple(evidence_to_citation.get(value, value) for value in item.evidence_ids),
            values=item.values,
            description=item.description,
            resolution_status=item.resolution_status,
            resolution_basis=item.resolution_basis,
            selected_evidence_ids=tuple(
                evidence_to_citation.get(value, value) for value in item.selected_evidence_ids
            ),
            scope_summaries=item.scope_summaries,
        )
        for item in conflicts
    ]
