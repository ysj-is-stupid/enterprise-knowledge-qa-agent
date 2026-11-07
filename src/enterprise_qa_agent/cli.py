"""Command-line entry point.

Only the stage-4 scaffold is available here. The business Agent is implemented in
the next stage so this module deliberately returns an explicit placeholder status.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Sequence

from . import __version__
from .agent import EnterpriseQAAgent, result_to_dict
from .config import AgentConfig
from .feedback import (
    FeedbackStoreError,
    SQLiteFeedbackStore,
    append_feedback_jsonl,
    build_feedback_record,
)
from .models import ChatMessage, QueryRequest
from .policy import ModelError, OfflineMockModel, OpenAICompatibleModel
from .repository import KnowledgeBase, KnowledgeBaseError
from .retrieval import HybridRetriever
from .tools import KnowledgeTools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise-qa",
        description="企业知识问答 Agent",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="检查工程骨架与配置")
    health.add_argument("--json", action="store_true", dest="as_json")

    feedback = subparsers.add_parser("feedback", help="记录并归因一次回答反馈")
    feedback.add_argument("--result", type=Path, required=True, help="ask 命令保存的结果 JSON")
    feedback.add_argument("--rating", choices=("helpful", "unhelpful"), required=True)
    feedback.add_argument(
        "--reason",
        choices=(
            "retrieval_quality",
            "citation_quality",
            "conflict_handling",
            "answer_quality",
            "outdated_policy",
            "permission_issue",
            "other",
        ),
    )
    feedback.add_argument("--comment")
    feedback.add_argument(
        "--idempotency-key",
        help="调用方生成的重试幂等键；相同 run、rating 和键只写入一次",
    )
    feedback.add_argument("--store", type=Path, default=Path("runtime/feedback.sqlite3"))
    feedback.add_argument(
        "--format",
        choices=("sqlite", "jsonl"),
        default="sqlite",
        dest="store_format",
        help="默认事务型 SQLite；jsonl 仅用于兼容导出",
    )
    feedback.add_argument("--json", action="store_true", dest="as_json")

    feedback_summary = subparsers.add_parser("feedback-summary", help="汇总反馈数据库")
    feedback_summary.add_argument("--store", type=Path, default=Path("runtime/feedback.sqlite3"))
    feedback_summary.add_argument("--json", action="store_true", dest="as_json")

    feedback_migrate = subparsers.add_parser("feedback-migrate", help="将旧 JSONL 反馈迁移到 SQLite")
    feedback_migrate.add_argument("--source-jsonl", type=Path, required=True)
    feedback_migrate.add_argument("--store", type=Path, default=Path("runtime/feedback.sqlite3"))
    feedback_migrate.add_argument("--json", action="store_true", dest="as_json")

    ask = subparsers.add_parser("ask", help="提交企业知识问题")
    ask.add_argument("--question", required=True)
    ask.add_argument("--history", type=Path)
    ask.add_argument("--mode", choices=("offline", "live"), default="offline")
    ask.add_argument(
        "--department",
        action="append",
        dest="departments",
        help="请求者所属部门，可重复指定；默认 public",
    )
    ask.add_argument(
        "--as-of-date",
        help="规则有效性基准日期，ISO YYYY-MM-DD；默认今天",
    )
    ask.add_argument("--knowledge-base", type=Path, default=Path("data/documents.json"))
    ask.add_argument("--synonyms", type=Path, default=Path("data/synonyms.json"))
    ask.add_argument("--max-steps", type=int, default=10)
    ask.add_argument("--max-tool-calls", type=int, default=8)
    ask.add_argument("--fail-tool", choices=("search_docs", "read_doc"))
    ask.add_argument("--fail-count", type=int, default=0)
    ask.add_argument("--json", action="store_true", dest="as_json")
    ask.add_argument("--output", type=Path)
    ask.add_argument("--trace-output", type=Path)
    ask.add_argument("--quiet", action="store_true", help="保存文件时不打印结果")
    return parser


def _render(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = AgentConfig.from_environment()

    if args.command == "health":
        _render(
            {
                "status": "core_ready",
                "version": __version__,
                "config": config.public_summary(),
            },
            args.as_json,
        )
        return 0

    if args.command == "feedback":
        try:
            result_payload = json.loads(args.result.read_text(encoding="utf-8"))
            if not isinstance(result_payload, dict):
                raise ValueError("result 文件顶层必须是对象")
            record = build_feedback_record(
                result_payload,
                args.rating,
                reason=args.reason,
                comment=args.comment,
                idempotency_key=args.idempotency_key,
            )
            if args.store_format == "sqlite":
                store = SQLiteFeedbackStore(args.store)
                inserted = store.save(record)
                if not inserted:
                    record = store.get(record["feedback_id"]) or record
            else:
                append_feedback_jsonl(args.store, record)
                inserted = True
        except (OSError, ValueError, json.JSONDecodeError, FeedbackStoreError) as exc:
            _render({"status": "invalid_input", "message": str(exc)}, True)
            return 2
        _render(
            {
                "status": "feedback_recorded" if inserted else "feedback_duplicate",
                "storage": {
                    "format": args.store_format,
                    "path": str(args.store),
                    "inserted": inserted,
                },
                "feedback": record,
            },
            args.as_json,
        )
        return 0

    if args.command == "feedback-summary":
        try:
            summary = SQLiteFeedbackStore(args.store).summary()
        except FeedbackStoreError as exc:
            _render({"status": "storage_error", "message": str(exc)}, True)
            return 2
        _render(
            {"status": "feedback_summary", "store": str(args.store), "summary": summary},
            args.as_json,
        )
        return 0

    if args.command == "feedback-migrate":
        try:
            migration = SQLiteFeedbackStore(args.store).import_jsonl(args.source_jsonl)
        except FeedbackStoreError as exc:
            _render({"status": "migration_error", "message": str(exc)}, True)
            return 2
        _render(
            {
                "status": "feedback_migrated",
                "source": str(args.source_jsonl),
                "store": str(args.store),
                "migration": migration,
            },
            args.as_json,
        )
        return 0

    try:
        if args.max_steps < 1 or args.max_tool_calls < 1 or args.fail_count < 0:
            raise ValueError("执行上限必须大于 0，故障次数不能为负数")
        history = _load_history(args.history)
        knowledge_base = KnowledgeBase.load(args.knowledge_base)
        config = replace(
            AgentConfig.from_environment(),
            mode=args.mode,
            max_decision_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls,
        )
        retriever = HybridRetriever.from_synonym_file(
            knowledge_base,
            args.synonyms,
            min_score=config.search_min_score,
            conflict_margin=config.conflict_score_margin,
        )
        failures = {args.fail_tool: args.fail_count} if args.fail_tool and args.fail_count else None
        tools = KnowledgeTools(knowledge_base, retriever, injected_failures=failures)
        model = OfflineMockModel() if args.mode == "offline" else OpenAICompatibleModel(config)
        agent = EnterpriseQAAgent(config, model, knowledge_base, tools)
        departments = tuple(args.departments or ["public"])
        result = agent.run(
            QueryRequest(
                args.question,
                tuple(history),
                requester_departments=departments,
                **({"as_of_date": args.as_of_date} if args.as_of_date else {}),
            )
        )
        payload = result_to_dict(result)
    except (KnowledgeBaseError, ModelError, OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"status": "invalid_input", "message": str(exc)}
        _render(payload, True if args.output else args.as_json)
        return 2

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.trace_output:
        args.trace_output.parent.mkdir(parents=True, exist_ok=True)
        args.trace_output.write_text(
            json.dumps(payload.get("trace", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if not args.quiet:
        _render(payload, args.as_json)
    return 0


def _load_history(path: Path | None) -> list[ChatMessage]:
    if path is None:
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("history 文件顶层必须是数组")
    messages: list[ChatMessage] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise ValueError(f"history 第 {index} 条消息格式无效")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"history 第 {index} 条消息内容为空")
        messages.append(ChatMessage(item["role"], content.strip()))
    return messages


def entrypoint() -> None:
    # Windows terminals may otherwise expose a legacy code page while the
    # surrounding runner expects UTF-8 JSON.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
