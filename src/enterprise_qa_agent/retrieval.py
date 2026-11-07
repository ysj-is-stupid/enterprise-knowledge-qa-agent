"""Deterministic, explainable hybrid retrieval for a small local corpus."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date
import json
import math
from pathlib import Path
import re
import unicodedata

from .models import DocumentChunk, KnowledgeDocument, SearchHit
from .repository import KnowledgeBase, can_access_document, is_effective_on


_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


class HybridRetriever:
    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        synonyms: dict[str, tuple[str, ...]] | None = None,
        *,
        min_score: float = 0.18,
        conflict_margin: float = 0.15,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.synonyms = synonyms or {}
        self.min_score = min_score
        self.conflict_margin = conflict_margin
        self._document_tokens = {
            document.document_id: _tokens(_document_text(document))
            for document in knowledge_base.documents
        }

    @classmethod
    def from_synonym_file(
        cls,
        knowledge_base: KnowledgeBase,
        path: str | Path,
        **kwargs: float,
    ) -> "HybridRetriever":
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法加载同义词文件 {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("同义词文件顶层必须是对象")
        synonyms: dict[str, tuple[str, ...]] = {}
        for concept, variants in raw.items():
            if not isinstance(concept, str) or not isinstance(variants, list):
                raise ValueError("同义词概念必须映射到字符串数组")
            if any(not isinstance(item, str) or not item for item in variants):
                raise ValueError(f"同义词概念 {concept} 含无效表达")
            synonyms[concept] = tuple(variants)
        return cls(knowledge_base, synonyms, **kwargs)

    def search(
        self,
        query: str,
        top_k: int = 5,
        requester_departments: tuple[str, ...] = ("public",),
        as_of_date: str | None = None,
    ) -> list[SearchHit]:
        normalized = _normalize(query)
        if not normalized:
            return []
        requested_date = as_of_date or date.today().isoformat()
        visible_documents = [
            document
            for document in self.knowledge_base.documents
            if can_access_document(document, requester_departments)
            and is_effective_on(document, requested_date)
        ]
        if not visible_documents:
            return []
        idf = self._build_idf(visible_documents)
        expanded_query, matched_concepts = self._expand_query(normalized)
        query_tokens = _tokens(expanded_query)
        query_ngrams = _cjk_ngrams(normalized)

        raw_bm25 = {
            document.document_id: self._bm25(
                query_tokens,
                self._document_tokens[document.document_id],
                idf,
            )
            for document in visible_documents
        }
        lexical_ceiling = sum(
            idf.get(token, self._unseen_idf(len(visible_documents))) for token in query_tokens
        ) or 1.0
        scored: list[tuple[float, KnowledgeDocument, list[str], list[str], str]] = []
        for document in visible_documents:
            text = _document_text(document)
            doc_normalized = _normalize(text)
            lexical = min(raw_bm25[document.document_id] / lexical_ceiling, 1.0)
            doc_ngrams = _cjk_ngrams(doc_normalized)
            ngram = len(query_ngrams & doc_ngrams) / max(len(query_ngrams), 1)
            concept_hits = [
                concept
                for concept in matched_concepts
                if any(_normalize(term) in doc_normalized for term in self.synonyms[concept])
            ]
            concept = len(concept_hits) / max(len(matched_concepts), 1) if matched_concepts else 0.0
            metadata = self._metadata_score(normalized, document)
            score = 0.45 * lexical + 0.30 * ngram + 0.20 * concept + 0.05 * metadata
            matched_chunks = _select_chunks(document, expanded_query)
            chunk_ids = [chunk.chunk_id for chunk in matched_chunks]
            rule_keys = sorted({chunk.rule_key for chunk in matched_chunks if chunk.rule_key})
            reasons: list[str] = []
            if lexical > 0:
                reasons.append(f"词项匹配 {lexical:.2f}")
            if ngram > 0:
                reasons.append(f"字符片段匹配 {ngram:.2f}")
            if concept_hits:
                reasons.append("同义概念: " + ", ".join(concept_hits))
            if metadata > 0:
                reasons.append("元数据匹配")
            preview = matched_chunks[0].text[:80] if matched_chunks else ""
            scored.append((score, document, chunk_ids, rule_keys, preview))

        scored.sort(key=lambda item: (-item[0], item[1].document_id))
        if not scored:
            return []
        top_score = scored[0][0]
        top_topic = scored[0][1].topic
        kept: list[tuple[float, KnowledgeDocument, list[str], list[str], str]] = []
        for item in scored:
            score, document, _, _, _ = item
            normal_match = score >= self.min_score
            conflict_band = (
                top_score >= self.min_score
                and
                document.topic == top_topic
                and score > 0
                and score >= top_score - self.conflict_margin
            )
            if normal_match or conflict_band:
                kept.append(item)
            if len(kept) >= top_k:
                break

        hits: list[SearchHit] = []
        for score, document, chunk_ids, rule_keys, preview in kept:
            reasons = self._reasons(normalized, expanded_query, matched_concepts, document, score)
            hits.append(
                SearchHit(
                    document_id=document.document_id,
                    title=document.title,
                    version_date=document.version_date,
                    effective_from=document.effective_from,
                    effective_to=document.effective_to,
                    topic=document.topic,
                    scope_summary=_scope_summary(document),
                    matched_chunk_ids=tuple(chunk_ids),
                    matched_rule_keys=tuple(rule_keys),
                    score=round(score, 6),
                    match_reasons=tuple(reasons),
                    preview=preview,
                )
            )
        return self._expand_dependency_hits(
            hits,
            {document.document_id: document for document in visible_documents},
        )

    def _expand_dependency_hits(
        self,
        hits: list[SearchHit],
        visible_documents: dict[str, KnowledgeDocument],
    ) -> list[SearchHit]:
        """Recursively add visible same- or cross-document evidence dependencies."""

        by_document = {hit.document_id: hit for hit in hits}
        order = [hit.document_id for hit in hits]
        queue = [
            (hit.document_id, chunk_id, hit.score)
            for hit in hits
            for chunk_id in hit.matched_chunk_ids
        ]
        visited: set[tuple[str, str]] = set()
        while queue:
            source_document_id, source_chunk_id, source_score = queue.pop(0)
            source_key = (source_document_id, source_chunk_id)
            if source_key in visited:
                continue
            visited.add(source_key)
            source_chunk = self.knowledge_base.get_chunk(*source_key)
            for reference in source_chunk.required_evidence_refs:
                target_document = visible_documents.get(reference.document_id)
                if target_document is None:
                    continue
                target_chunk = self.knowledge_base.get_chunk(
                    reference.document_id,
                    reference.chunk_id,
                )
                existing = by_document.get(reference.document_id)
                reason = (
                    f"证据依赖: {source_document_id}/{source_chunk_id} → "
                    f"{reference.document_id}/{reference.chunk_id}"
                )
                if existing is None:
                    inherited_score = round(max(source_score - 0.000001, 0.0), 6)
                    existing = SearchHit(
                        document_id=target_document.document_id,
                        title=target_document.title,
                        version_date=target_document.version_date,
                        effective_from=target_document.effective_from,
                        effective_to=target_document.effective_to,
                        topic=target_document.topic,
                        scope_summary=_scope_summary(target_document),
                        matched_chunk_ids=(target_chunk.chunk_id,),
                        matched_rule_keys=(
                            (target_chunk.rule_key,) if target_chunk.rule_key else ()
                        ),
                        score=inherited_score,
                        match_reasons=(reason,),
                        preview=target_chunk.text[:80],
                    )
                    by_document[reference.document_id] = existing
                    order.append(reference.document_id)
                else:
                    chunk_ids = tuple(
                        dict.fromkeys((*existing.matched_chunk_ids, target_chunk.chunk_id))
                    )
                    rule_keys = tuple(
                        sorted(
                            {
                                *existing.matched_rule_keys,
                                *([target_chunk.rule_key] if target_chunk.rule_key else []),
                            }
                        )
                    )
                    reasons = tuple(dict.fromkeys((*existing.match_reasons, reason)))
                    existing = replace(
                        existing,
                        matched_chunk_ids=chunk_ids,
                        matched_rule_keys=rule_keys,
                        match_reasons=reasons,
                    )
                    by_document[reference.document_id] = existing
                queue.append((reference.document_id, reference.chunk_id, existing.score))
        return [by_document[document_id] for document_id in order]

    def _build_idf(self, documents: list[KnowledgeDocument]) -> dict[str, float]:
        document_count = max(len(documents), 1)
        frequencies: Counter[str] = Counter()
        for document in documents:
            tokens = self._document_tokens[document.document_id]
            frequencies.update(set(tokens))
        return {
            token: math.log(1 + (document_count - count + 0.5) / (count + 0.5))
            for token, count in frequencies.items()
        }

    def _bm25(
        self,
        query_tokens: set[str],
        document_tokens: set[str],
        idf: dict[str, float] | None = None,
    ) -> float:
        if not query_tokens or not document_tokens:
            return 0.0
        active_idf = idf or self._build_idf(list(self.knowledge_base.documents))
        return sum(active_idf.get(token, 0.0) for token in query_tokens & document_tokens)

    def _unseen_idf(self, document_count: int) -> float:
        document_count = max(document_count, 1)
        return math.log(1 + (document_count + 0.5) / 0.5)

    def _expand_query(self, normalized: str) -> tuple[str, set[str]]:
        additions: list[str] = []
        matched: set[str] = set()
        for concept, variants in self.synonyms.items():
            if any(_normalize(variant) in normalized for variant in variants):
                matched.add(concept)
                additions.extend(variants)
        return " ".join([normalized, *additions]), matched

    def _metadata_score(self, query: str, document: KnowledgeDocument) -> float:
        fields = [_normalize(document.title), _normalize(document.topic), *map(_normalize, document.keywords)]
        return 1.0 if any(field and (field in query or query in field) for field in fields) else 0.0

    def _reasons(
        self,
        query: str,
        expanded_query: str,
        matched_concepts: set[str],
        document: KnowledgeDocument,
        score: float,
    ) -> list[str]:
        overlap = _tokens(expanded_query) & self._document_tokens[document.document_id]
        reasons = [f"融合相关度 {score:.3f}"]
        if overlap:
            reasons.append("词项命中: " + ", ".join(sorted(overlap)[:6]))
        concept_hits = [
            concept
            for concept in matched_concepts
            if any(_normalize(term) in _normalize(_document_text(document)) for term in self.synonyms[concept])
        ]
        if concept_hits:
            reasons.append("同义概念: " + ", ".join(concept_hits))
        return reasons


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", text).strip()


def _cjk_ngrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for size in (2, 3):
            grams.update(run[index : index + size] for index in range(max(len(run) - size + 1, 0)))
    return grams


def _tokens(text: str) -> set[str]:
    normalized = _normalize(text)
    return set(_ASCII_WORD.findall(normalized)) | _cjk_ngrams(normalized)


def _document_text(document: KnowledgeDocument) -> str:
    parts = [document.title, document.topic, *document.keywords]
    for chunk in document.chunks:
        parts.extend([chunk.section, *chunk.keywords, chunk.text])
    return " ".join(parts)


def _scope_summary(document: KnowledgeDocument) -> str:
    return (
        f"员工={','.join(document.scope.employee_types)};"
        f"地区={','.join(document.scope.regions)};"
        f"业务={','.join(document.scope.business_types)}"
    )


def _select_chunks(
    document: KnowledgeDocument,
    expanded_query: str,
) -> list[DocumentChunk]:
    query_tokens = _tokens(expanded_query)
    scored = [
        (
            len(query_tokens & _tokens(" ".join((chunk.section, *chunk.keywords, chunk.text)))),
            index,
            chunk,
        )
        for index, chunk in enumerate(document.chunks)
    ]
    positive = [item for item in scored if item[0] > 0]
    selected = [item[2] for item in sorted(positive, key=lambda item: (-item[0], item[1]))[:3]]
    if not selected and scored:
        selected = [max(scored, key=lambda item: (item[0], -item[1]))[2]]

    return selected
