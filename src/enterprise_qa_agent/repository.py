"""Knowledge-base loading, validation, and exact source access."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Iterable

from .models import (
    DocumentChunk,
    DocumentScope,
    Evidence,
    EvidenceRef,
    KnowledgeDocument,
    RuleOverride,
)


class KnowledgeBaseError(ValueError):
    """Raised when the local knowledge base violates its data contract."""


class KnowledgeBase:
    def __init__(self, documents: Iterable[KnowledgeDocument]) -> None:
        self.documents = tuple(documents)
        self._documents = {document.document_id: document for document in self.documents}
        self._chunks: dict[tuple[str, str], DocumentChunk] = {}
        for document in self.documents:
            for chunk in document.chunks:
                self._chunks[(document.document_id, chunk.chunk_id)] = chunk
        self._validate_evidence_dependencies()
        self._validate_rule_overrides()

    @classmethod
    def load(cls, path: str | Path) -> "KnowledgeBase":
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeBaseError(f"无法加载知识库 {source}: {exc}") from exc
        return cls.from_data(raw)

    @classmethod
    def from_data(cls, raw: Any) -> "KnowledgeBase":
        """Build a validated knowledge base from in-memory JSON-compatible data."""

        if not isinstance(raw, list):
            raise KnowledgeBaseError("知识库顶层必须是 JSON 数组")
        return cls(cls._parse_documents(raw))

    @staticmethod
    def _parse_documents(raw_documents: list[Any]) -> list[KnowledgeDocument]:
        documents: list[KnowledgeDocument] = []
        document_ids: set[str] = set()
        chunk_ids: set[str] = set()
        for index, raw in enumerate(raw_documents):
            if not isinstance(raw, dict):
                raise KnowledgeBaseError(f"第 {index + 1} 份文档必须是对象")
            document_id = _required_text(raw, "document_id", f"文档 {index + 1}")
            if document_id in document_ids:
                raise KnowledgeBaseError(f"文档 ID 重复: {document_id}")
            document_ids.add(document_id)
            version_date = _required_text(raw, "version_date", document_id)
            try:
                date.fromisoformat(version_date)
            except ValueError as exc:
                raise KnowledgeBaseError(f"{document_id} 的 version_date 无效") from exc
            effective_from = _required_text(raw, "effective_from", document_id)
            effective_to = _optional_text(raw.get("effective_to"))
            try:
                effective_from_date = date.fromisoformat(effective_from)
                effective_to_date = date.fromisoformat(effective_to) if effective_to else None
            except ValueError as exc:
                raise KnowledgeBaseError(f"{document_id} 的有效期日期无效") from exc
            if effective_to_date is not None and effective_to_date < effective_from_date:
                raise KnowledgeBaseError(f"{document_id} 的 effective_to 不能早于 effective_from")

            scope_raw = raw.get("scope") or {}
            if not isinstance(scope_raw, dict):
                raise KnowledgeBaseError(f"{document_id} 的 scope 必须是对象")
            scope = DocumentScope(
                employee_types=_string_tuple(scope_raw.get("employee_types", ["all"])),
                regions=_string_tuple(scope_raw.get("regions", ["all"])),
                business_types=_string_tuple(scope_raw.get("business_types", ["all"])),
            )
            chunks_raw = raw.get("chunks")
            if not isinstance(chunks_raw, list) or not chunks_raw:
                raise KnowledgeBaseError(f"{document_id} 至少需要一个片段")
            chunks: list[DocumentChunk] = []
            for chunk_raw in chunks_raw:
                if not isinstance(chunk_raw, dict):
                    raise KnowledgeBaseError(f"{document_id} 的片段必须是对象")
                chunk_id = _required_text(chunk_raw, "chunk_id", document_id)
                if chunk_id in chunk_ids:
                    raise KnowledgeBaseError(f"片段 ID 重复: {chunk_id}")
                chunk_ids.add(chunk_id)
                chunks.append(
                    DocumentChunk(
                        chunk_id=chunk_id,
                        section=_required_text(chunk_raw, "section", chunk_id),
                        rule_key=_optional_text(chunk_raw.get("rule_key")),
                        keywords=_string_tuple(chunk_raw.get("keywords", [])),
                        required_evidence_refs=_parse_evidence_refs(
                            chunk_raw,
                            document_id,
                            chunk_id,
                        ),
                        text=_required_text(chunk_raw, "text", chunk_id),
                    )
                )
            supersedes = _string_tuple(raw.get("supersedes", []))
            if document_id in supersedes:
                raise KnowledgeBaseError(f"{document_id} 不能替代自身")
            priority = raw.get("priority")
            if priority is not None and not isinstance(priority, int):
                raise KnowledgeBaseError(f"{document_id} 的 priority 必须是整数或 null")
            access_control = raw.get("access_control")
            if not isinstance(access_control, dict):
                raise KnowledgeBaseError(f"{document_id} 缺少 access_control 对象")
            allowed_departments = tuple(
                item.strip().lower()
                for item in _string_tuple(access_control.get("departments"))
            )
            if not allowed_departments:
                raise KnowledgeBaseError(f"{document_id} 的 access_control.departments 不能为空")
            rule_overrides = _parse_rule_overrides(raw.get("rule_overrides", []), document_id)
            documents.append(
                KnowledgeDocument(
                    document_id=document_id,
                    title=_required_text(raw, "title", document_id),
                    version_date=version_date,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    topic=_required_text(raw, "topic", document_id),
                    scope=scope,
                    priority=priority,
                    supersedes=supersedes,
                    rule_overrides=rule_overrides,
                    keywords=_string_tuple(raw.get("keywords", [])),
                    allowed_departments=allowed_departments,
                    chunks=tuple(chunks),
                )
            )
        return documents

    def _validate_rule_overrides(self) -> None:
        for document in self.documents:
            seen: set[tuple[str, str, str]] = set()
            own_chunks = {chunk.chunk_id: chunk for chunk in document.chunks}
            for override in document.rule_overrides:
                signature = (
                    override.source_chunk_id,
                    override.target_document_id,
                    override.target_chunk_id,
                )
                if signature in seen:
                    raise KnowledgeBaseError(f"{document.document_id} 存在重复的 rule_overrides")
                seen.add(signature)
                source_chunk = own_chunks.get(override.source_chunk_id)
                if source_chunk is None:
                    raise KnowledgeBaseError(
                        f"{document.document_id} 的特例源片段不存在: {override.source_chunk_id}"
                    )
                try:
                    target_document = self.get_document(override.target_document_id)
                    target_chunk = self.get_chunk(
                        override.target_document_id,
                        override.target_chunk_id,
                    )
                except KnowledgeBaseError as exc:
                    raise KnowledgeBaseError(
                        f"{document.document_id} 的特例目标不存在"
                    ) from exc
                if source_chunk.rule_key != override.rule_key or target_chunk.rule_key != override.rule_key:
                    raise KnowledgeBaseError(
                        f"{document.document_id} 的特例 rule_key 与源或目标片段不一致"
                    )
                if not scope_is_strict_subset(document.scope, target_document.scope):
                    raise KnowledgeBaseError(
                        f"{document.document_id} 的特例范围必须是目标文档范围的严格子集"
                    )

    def _validate_evidence_dependencies(self) -> None:
        graph = {
            EvidenceRef(document.document_id, chunk.chunk_id): chunk.required_evidence_refs
            for document in self.documents
            for chunk in document.chunks
        }
        for source, required_refs in graph.items():
            for required in required_refs:
                if required not in graph:
                    raise KnowledgeBaseError(
                        f"{source.document_id}/{source.chunk_id} 引用了不存在的 "
                        f"required_evidence_ref: {required.document_id}/{required.chunk_id}"
                    )
                if required == source:
                    raise KnowledgeBaseError(
                        f"{source.document_id}/{source.chunk_id} 不能依赖自身"
                    )

        visiting: set[EvidenceRef] = set()
        visited: set[EvidenceRef] = set()

        def visit(reference: EvidenceRef) -> None:
            if reference in visited:
                return
            if reference in visiting:
                raise KnowledgeBaseError("required_evidence_refs 存在跨文档或文档内循环依赖")
            visiting.add(reference)
            for required in graph[reference]:
                visit(required)
            visiting.remove(reference)
            visited.add(reference)

        for reference in graph:
            visit(reference)

    def get_document(self, document_id: str) -> KnowledgeDocument:
        try:
            return self._documents[document_id]
        except KeyError as exc:
            raise KnowledgeBaseError(f"文档不存在: {document_id}") from exc

    def get_chunk(self, document_id: str, chunk_id: str) -> DocumentChunk:
        try:
            return self._chunks[(document_id, chunk_id)]
        except KeyError as exc:
            raise KnowledgeBaseError(f"片段不存在: {document_id}/{chunk_id}") from exc

    def make_evidence(
        self,
        document_id: str,
        chunk_id: str,
        evidence_id: str,
        *,
        required_evidence_refs: tuple[EvidenceRef, ...] | None = None,
        blocked_dependency_count: int = 0,
    ) -> Evidence:
        document = self.get_document(document_id)
        chunk = self.get_chunk(document_id, chunk_id)
        return Evidence(
            evidence_id=evidence_id,
            document_id=document.document_id,
            chunk_id=chunk.chunk_id,
            title=document.title,
            version_date=document.version_date,
            effective_from=document.effective_from,
            effective_to=document.effective_to,
            topic=document.topic,
            scope=document.scope,
            rule_key=chunk.rule_key,
            required_evidence_refs=(
                chunk.required_evidence_refs
                if required_evidence_refs is None
                else required_evidence_refs
            ),
            blocked_dependency_count=blocked_dependency_count,
            text=chunk.text,
        )


def can_access_document(
    document: KnowledgeDocument,
    requester_departments: tuple[str, ...],
) -> bool:
    """Return whether trusted request context permits access to a document."""

    if "all" in document.allowed_departments:
        return True
    return bool(set(document.allowed_departments) & set(requester_departments))


def is_effective_on(document: KnowledgeDocument, as_of_date: str) -> bool:
    """Treat both validity boundaries as inclusive."""

    requested = date.fromisoformat(as_of_date)
    starts = date.fromisoformat(document.effective_from)
    ends = date.fromisoformat(document.effective_to) if document.effective_to else None
    return starts <= requested and (ends is None or requested <= ends)


def scope_is_strict_subset(special: DocumentScope, general: DocumentScope) -> bool:
    dimensions = (
        (special.employee_types, general.employee_types),
        (special.regions, general.regions),
        (special.business_types, general.business_types),
    )
    return all(_dimension_is_subset(left, right) for left, right in dimensions) and any(
        not _dimension_is_equivalent(left, right) for left, right in dimensions
    )


def _dimension_is_subset(special: tuple[str, ...], general: tuple[str, ...]) -> bool:
    if "all" in special:
        return set(special) == {"all"} and "all" in general
    if "all" in general:
        return True
    return set(special) <= set(general)


def _dimension_is_equivalent(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return set(left) == set(right)


def _required_text(raw: dict[str, Any], key: str, owner: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeBaseError(f"{owner} 缺少非空字段 {key}")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeBaseError("可选文本字段必须是非空字符串或 null")
    return value.strip()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise KnowledgeBaseError("列表字段必须只包含非空字符串")
    return tuple(value)


def _parse_rule_overrides(value: Any, document_id: str) -> tuple[RuleOverride, ...]:
    if not isinstance(value, list):
        raise KnowledgeBaseError(f"{document_id} 的 rule_overrides 必须是数组")
    overrides: list[RuleOverride] = []
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise KnowledgeBaseError(f"{document_id} 的第 {index} 个 rule_override 必须是对象")
        owner = f"{document_id} rule_override {index}"
        overrides.append(
            RuleOverride(
                source_chunk_id=_required_text(raw, "source_chunk_id", owner),
                target_document_id=_required_text(raw, "target_document_id", owner),
                target_chunk_id=_required_text(raw, "target_chunk_id", owner),
                rule_key=_required_text(raw, "rule_key", owner),
                reason=_required_text(raw, "reason", owner),
            )
        )
    return tuple(overrides)


def _parse_evidence_refs(
    raw: dict[str, Any],
    document_id: str,
    chunk_id: str,
) -> tuple[EvidenceRef, ...]:
    refs = [
        EvidenceRef(document_id, required_id)
        for required_id in _string_tuple(raw.get("required_chunk_ids", []))
    ]
    cross_refs = raw.get("required_evidence_refs", [])
    if not isinstance(cross_refs, list):
        raise KnowledgeBaseError(f"{document_id}/{chunk_id} 的 required_evidence_refs 必须是数组")
    for index, item in enumerate(cross_refs, 1):
        if not isinstance(item, dict):
            raise KnowledgeBaseError(
                f"{document_id}/{chunk_id} 的第 {index} 个 required_evidence_ref 必须是对象"
            )
        owner = f"{document_id}/{chunk_id} required_evidence_ref {index}"
        refs.append(
            EvidenceRef(
                _required_text(item, "document_id", owner),
                _required_text(item, "chunk_id", owner),
            )
        )
    if len(set(refs)) != len(refs):
        raise KnowledgeBaseError(f"{document_id}/{chunk_id} 存在重复证据依赖")
    return tuple(refs)
