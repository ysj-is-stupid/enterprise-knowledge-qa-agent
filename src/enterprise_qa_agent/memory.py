"""Provenance-preserving episodic retrieval for long chat histories."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .models import ChatMessage


_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_ASCII_TOKEN = re.compile(r"[a-zA-Z0-9]+")
_ANAPHORA_MARKERS = ("那", "它", "这个", "这种", "还有呢", "那么", "上述", "前面")
_LOW_INFORMATION = {
    "好", "好的", "知道了", "明白了", "谢谢", "感谢", "继续", "请继续", "请继续提问",
    "不客气", "可以", "嗯", "ok", "okay",
}
_GENERIC_TOKENS = {"制度", "规定", "问题", "哪些", "什么", "怎么", "如何", "相关"}


def retrieve_history_context(
    history: tuple[ChatMessage, ...],
    question: str,
    *,
    recent_limit: int,
    memory_limit: int = 6,
) -> dict[str, Any]:
    """Retrieve exact older messages without replacing them with a summary."""

    recent_count = min(max(recent_limit, 0), len(history))
    split_at = len(history) - recent_count
    recent_indexes = tuple(range(split_at, len(history)))
    older_indexes = tuple(range(split_at))
    query_tokens = _memory_tokens(question)

    scored: list[tuple[float, int]] = []
    for index in older_indexes:
        message = history[index]
        if _is_low_information_message(message.content):
            continue
        tokens = _memory_tokens(message.content)
        overlap = len(query_tokens & tokens) / max(len(query_tokens), 1)
        role_bonus = 0.08 if message.role == "user" else 0.0
        anaphora_bonus = (
            0.08
            if message.role == "user" and any(marker in question for marker in _ANAPHORA_MARKERS)
            else 0.0
        )
        recency = (index + 1) / max(len(history), 1) * 0.03
        scored.append((overlap + role_bonus + anaphora_bonus + recency, index))

    selected_indexes = tuple(
        sorted(
            index
            for _, index in sorted(scored, key=lambda item: (-item[0], -item[1]))[
                : max(memory_limit, 0)
            ]
        )
    )
    retrieved = [_memory_record(history[index], index) for index in selected_indexes]
    recent = [_memory_record(history[index], index) for index in recent_indexes]

    anchor_candidates: list[tuple[float, int]] = []
    for index, message in enumerate(history):
        if message.role != "user" or _is_low_information_message(message.content):
            continue
        tokens = _memory_tokens(message.content)
        overlap = len(query_tokens & tokens) / max(len(query_tokens), 1)
        recency = (index + 1) / max(len(history), 1) * 0.03
        anchor_candidates.append((overlap + recency, index))
    primary_anchor_index = (
        max(anchor_candidates, key=lambda item: (item[0], item[1]))[1]
        if anchor_candidates
        else None
    )

    provided_indexes = set(selected_indexes) | set(recent_indexes)
    return {
        "memory_strategy": "provenance_preserving_episodic_retrieval_v1",
        "retrieved_memories": retrieved,
        "recent_messages": recent,
        "retrieved_message_indexes": list(selected_indexes),
        "recent_message_indexes": list(recent_indexes),
        "primary_anchor_index": primary_anchor_index,
        "primary_anchor": (
            history[primary_anchor_index].content
            if primary_anchor_index is not None
            else ""
        ),
        "omitted_message_count": len(history) - len(provided_indexes),
        "history_message_count": len(history),
    }


def _memory_record(message: ChatMessage, index: int) -> dict[str, Any]:
    return {
        "message_index": index,
        "role": message.role,
        "content": message.content,
        "content_sha256": hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
    }


def _memory_tokens(text: str) -> set[str]:
    tokens = {item.lower() for item in _ASCII_TOKEN.findall(text)}
    for run in _CJK_RUN.findall(text):
        tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(max(len(run) - 1, 0)))
    return {item for item in tokens if item not in _GENERIC_TOKENS}


def _is_low_information_message(content: str) -> bool:
    normalized = re.sub(r"[\s，。！？!?、]", "", content).lower()
    return normalized in _LOW_INFORMATION
