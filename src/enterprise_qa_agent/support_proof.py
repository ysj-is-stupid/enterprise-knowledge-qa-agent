"""Fail-closed, auditable proof obligations for completed answer claims.

The verifier intentionally does not attempt open-ended natural-language
entailment.  A claim clause is accepted only when it is a verbatim source
clause or differs solely by a deterministic semantic-fact canonicalization
such as ``30天`` -> ``30日``.  Anything more expressive must remain a quote or
be rejected, so a completed answer never depends on a heuristic similarity
score alone.
"""

from __future__ import annotations

import re
import unicodedata

from .models import AgentState, Claim, ClaimSupportCertificate
from .semantic_facts import canonicalize_semantic_facts


_CLAUSE = re.compile(r"[^。；;！？!?]+[。；;！？!?]?")
_ENDING_PUNCTUATION = "。；;！？!?"


def certify_claim_support(
    state: AgentState,
    claims: list[Claim],
) -> tuple[list[ClaimSupportCertificate], list[dict[str, object]]]:
    """Return deterministic support certificates or fail-closed errors."""

    certificates: list[ClaimSupportCertificate] = []
    errors: list[dict[str, object]] = []
    for claim_index, claim in enumerate(claims, 1):
        if not claim.text.strip() or not claim.evidence_ids:
            continue
        evidence = [state.evidence_store.get(item) for item in claim.evidence_ids]
        if any(item is None for item in evidence):
            continue

        source_clauses: list[tuple[str, str]] = []
        for evidence_id, item in zip(claim.evidence_ids, evidence, strict=True):
            if item is None:
                continue
            source_clauses.extend(
                (evidence_id, clause)
                for clause in _split_clauses(item.text)
            )

        matched: list[tuple[str, str, str]] = []
        unmatched: list[str] = []
        for claim_clause in _split_clauses(claim.text):
            canonical_claim = _proof_normalize(claim_clause)
            match = next(
                (
                    (evidence_id, source_clause)
                    for evidence_id, source_clause in source_clauses
                    if _proof_normalize(source_clause) == canonical_claim
                ),
                None,
            )
            if match is None:
                unmatched.append(claim_clause)
                continue
            evidence_id, source_clause = match
            method = (
                "verbatim_clause"
                if _surface_normalize(source_clause) == _surface_normalize(claim_clause)
                else "canonical_fact_equivalence"
            )
            matched.append((evidence_id, source_clause, method))

        if unmatched:
            errors.append(
                {
                    "code": "unproven_semantic_clause",
                    "message": (
                        f"声明 {claim_index} 含无法由绑定原文逐句证明的内容："
                        + "；".join(unmatched)
                        + "。请直接使用原文分句；仅允许已定义的单位等价改写。"
                    ),
                    "claim_index": claim_index,
                    "unmatched_clauses": unmatched,
                }
            )
            continue

        used_evidence_ids = tuple(dict.fromkeys(item[0] for item in matched))
        unused_evidence_ids = [
            evidence_id
            for evidence_id in claim.evidence_ids
            if evidence_id not in used_evidence_ids
        ]
        if unused_evidence_ids:
            errors.append(
                {
                    "code": "unused_claim_evidence",
                    "message": (
                        f"声明 {claim_index} 引用了未参与逐句证明的证据："
                        + "、".join(unused_evidence_ids)
                        + "。"
                    ),
                    "claim_index": claim_index,
                    "evidence_ids": unused_evidence_ids,
                }
            )
            continue

        methods = tuple(dict.fromkeys(item[2] for item in matched))
        certificates.append(
            ClaimSupportCertificate(
                claim_index=claim_index,
                claim_text=claim.text,
                evidence_ids=used_evidence_ids,
                proof_methods=methods,
                matched_source_clauses=tuple(item[1] for item in matched),
                normalized_claim=_proof_normalize(claim.text),
            )
        )
    return certificates, errors


def _split_clauses(text: str) -> list[str]:
    return [item.strip() for item in _CLAUSE.findall(text) if item.strip()]


def _surface_normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.rstrip(_ENDING_PUNCTUATION)


def _proof_normalize(text: str) -> str:
    return _surface_normalize(canonicalize_semantic_facts(text))
