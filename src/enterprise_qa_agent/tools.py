"""Business tools exposed to the decision model through the executor."""

from __future__ import annotations

from collections import Counter
from datetime import date

from .models import ToolObservation
from .repository import (
    KnowledgeBase,
    KnowledgeBaseError,
    can_access_document,
    is_effective_on,
)
from .retrieval import HybridRetriever


class KnowledgeTools:
    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        retriever: HybridRetriever,
        *,
        injected_failures: dict[str, int] | None = None,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.retriever = retriever
        self._failures = Counter(injected_failures or {})
        self._evidence_counter = 0

    def search_docs(
        self,
        query: str,
        top_k: int,
        requester_departments: tuple[str, ...] = ("public",),
        as_of_date: str | None = None,
    ) -> ToolObservation:
        arguments = {
            "query": query,
            "top_k": top_k,
            "requester_departments": list(requester_departments),
            "as_of_date": as_of_date,
        }
        injected = self._take_injected_failure("search_docs")
        if injected:
            return ToolObservation(
                tool_name="search_docs",
                ok=False,
                arguments=arguments,
                result_summary="注入的检索工具故障",
                error_code="search_unavailable",
                retryable=True,
                injected_failure=True,
            )
        if not query.strip():
            return ToolObservation(
                tool_name="search_docs",
                ok=False,
                arguments=arguments,
                result_summary="检索词不能为空",
                error_code="invalid_query",
                retryable=False,
            )
        try:
            hits = self.retriever.search(
                query,
                top_k,
                requester_departments,
                as_of_date,
            )
        except Exception as exc:  # tool boundary intentionally converts failures
            return ToolObservation(
                tool_name="search_docs",
                ok=False,
                arguments=arguments,
                result_summary=f"检索失败: {type(exc).__name__}",
                error_code="knowledge_base_error",
                retryable=False,
            )
        summary = "未找到达到阈值的候选文档" if not hits else (
            f"找到 {len(hits)} 份候选: "
            + ", ".join(f"{hit.document_id}({hit.score:.3f})" for hit in hits)
        )
        return ToolObservation(
            tool_name="search_docs",
            ok=True,
            arguments=arguments,
            result_summary=summary,
            search_hits=hits,
        )

    def read_doc(
        self,
        document_id: str,
        chunk_id: str,
        requester_departments: tuple[str, ...] = ("public",),
        as_of_date: str | None = None,
    ) -> ToolObservation:
        arguments = {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "requester_departments": list(requester_departments),
            "as_of_date": as_of_date,
        }
        injected = self._take_injected_failure("read_doc")
        if injected:
            return ToolObservation(
                tool_name="read_doc",
                ok=False,
                arguments=arguments,
                result_summary="注入的原文读取工具故障",
                error_code="read_unavailable",
                retryable=True,
                injected_failure=True,
            )
        self._evidence_counter += 1
        try:
            document = self.knowledge_base.get_document(document_id)
            if not can_access_document(document, requester_departments) or (
                as_of_date is not None and not is_effective_on(document, as_of_date)
            ):
                raise KnowledgeBaseError("文档不存在或当前请求上下文不可读取")
            chunk = self.knowledge_base.get_chunk(document_id, chunk_id)
            requested_date = as_of_date or date.today().isoformat()
            allowed_dependencies = []
            blocked_dependency_count = 0
            for reference in chunk.required_evidence_refs:
                target_document = self.knowledge_base.get_document(reference.document_id)
                if can_access_document(
                    target_document,
                    requester_departments,
                ) and is_effective_on(target_document, requested_date):
                    allowed_dependencies.append(reference)
                else:
                    blocked_dependency_count += 1
            evidence = self.knowledge_base.make_evidence(
                document_id,
                chunk_id,
                f"E{self._evidence_counter}",
                required_evidence_refs=tuple(allowed_dependencies),
                blocked_dependency_count=blocked_dependency_count,
            )
        except KnowledgeBaseError as exc:
            return ToolObservation(
                tool_name="read_doc",
                ok=False,
                arguments=arguments,
                result_summary=str(exc),
                error_code="chunk_not_found",
                retryable=False,
            )
        return ToolObservation(
            tool_name="read_doc",
            ok=True,
            arguments=arguments,
            result_summary=(
                f"读取 {evidence.document_id}/{evidence.chunk_id}: "
                f"{evidence.text[:80]}"
                + (
                    f"；{evidence.blocked_dependency_count} 条关联证据在当前权限或日期下不可用"
                    if evidence.blocked_dependency_count
                    else ""
                )
            ),
            evidence=evidence,
        )

    def _take_injected_failure(self, tool_name: str) -> bool:
        if self._failures[tool_name] <= 0:
            return False
        self._failures[tool_name] -= 1
        return True
