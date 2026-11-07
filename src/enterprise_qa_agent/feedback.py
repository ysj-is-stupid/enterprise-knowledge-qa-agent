"""Structured feedback attribution with transactional SQLite persistence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


VALID_RATINGS = {"helpful", "unhelpful"}


def build_feedback_record(
    result: dict[str, Any],
    rating: str,
    *,
    reason: str | None = None,
    comment: str | None = None,
    created_at: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if rating not in VALID_RATINGS:
        raise ValueError("rating 必须是 helpful 或 unhelpful")
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    run_id = str(trace.get("run_id") or _stable_run_id(result))
    status = str(result.get("status", "unknown"))
    category, signals = _attribute_feedback(result, rating, reason)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "feedback_version": "1.0",
        "feedback_id": _feedback_id(run_id, rating, idempotency_key or timestamp),
        "run_id": run_id,
        "created_at": timestamp,
        "rating": rating,
        "source_status": status,
        "attribution": {
            "category": category,
            "signals": signals,
            "user_reason": reason,
        },
    }
    if comment:
        record["comment"] = comment.strip()
    return record


class FeedbackStoreError(RuntimeError):
    """Safe persistence error suitable for CLI display."""


class SQLiteFeedbackStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS feedback_records (
                        feedback_id TEXT PRIMARY KEY,
                        feedback_version TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        rating TEXT NOT NULL CHECK (rating IN ('helpful', 'unhelpful')),
                        source_status TEXT NOT NULL,
                        attribution_category TEXT NOT NULL,
                        attribution_signals_json TEXT NOT NULL,
                        user_reason TEXT,
                        comment TEXT,
                        record_json TEXT NOT NULL,
                        inserted_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_feedback_run_id
                        ON feedback_records(run_id);
                    CREATE INDEX IF NOT EXISTS idx_feedback_category_created
                        ON feedback_records(attribution_category, created_at);
                    CREATE INDEX IF NOT EXISTS idx_feedback_rating_created
                        ON feedback_records(rating, created_at);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self.SCHEMA_VERSION),),
                )
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库初始化失败: {type(exc).__name__}") from exc

    def save(self, record: dict[str, Any]) -> bool:
        self.initialize()
        values = _feedback_row(record)
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO feedback_records(
                        feedback_id, feedback_version, run_id, created_at, rating,
                        source_status, attribution_category, attribution_signals_json,
                        user_reason, comment, record_json, inserted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feedback_id) DO NOTHING
                    """,
                    values,
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库写入失败: {type(exc).__name__}") from exc

    def save_many(self, records: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        rows = [_feedback_row(record) for record in records]
        inserted = 0
        try:
            with self._connection() as connection:
                for values in rows:
                    cursor = connection.execute(
                        """
                        INSERT INTO feedback_records(
                            feedback_id, feedback_version, run_id, created_at, rating,
                            source_status, attribution_category, attribution_signals_json,
                            user_reason, comment, record_json, inserted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(feedback_id) DO NOTHING
                        """,
                        values,
                    )
                    inserted += int(cursor.rowcount == 1)
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库批量写入失败: {type(exc).__name__}") from exc
        return {"total": len(records), "inserted": inserted, "duplicates": len(records) - inserted}

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM feedback_records WHERE run_id = ? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库查询失败: {type(exc).__name__}") from exc
        return [json.loads(row[0]) for row in rows]

    def get(self, feedback_id: str) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT record_json FROM feedback_records WHERE feedback_id = ?",
                    (feedback_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库查询失败: {type(exc).__name__}") from exc
        return json.loads(row[0]) if row is not None else None

    def summary(self) -> dict[str, Any]:
        self.initialize()
        try:
            with self._connection() as connection:
                total = int(connection.execute("SELECT COUNT(*) FROM feedback_records").fetchone()[0])
                rating_rows = connection.execute(
                    "SELECT rating, COUNT(*) FROM feedback_records GROUP BY rating ORDER BY rating"
                ).fetchall()
                category_rows = connection.execute(
                    "SELECT attribution_category, COUNT(*) FROM feedback_records "
                    "GROUP BY attribution_category ORDER BY attribution_category"
                ).fetchall()
                status_rows = connection.execute(
                    "SELECT source_status, COUNT(*) FROM feedback_records "
                    "GROUP BY source_status ORDER BY source_status"
                ).fetchall()
                time_range = connection.execute(
                    "SELECT MIN(created_at), MAX(created_at) FROM feedback_records"
                ).fetchone()
                schema_version = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key='schema_version'"
                ).fetchone()[0]
        except sqlite3.Error as exc:
            raise FeedbackStoreError(f"反馈数据库统计失败: {type(exc).__name__}") from exc
        return {
            "schema_version": int(schema_version),
            "total": total,
            "by_rating": {str(key): int(value) for key, value in rating_rows},
            "by_attribution": {str(key): int(value) for key, value in category_rows},
            "by_status": {str(key): int(value) for key, value in status_rows},
            "earliest_created_at": time_range[0],
            "latest_created_at": time_range[1],
        }

    def import_jsonl(self, source: str | Path) -> dict[str, int]:
        path = Path(source)
        records: list[dict[str, Any]] = []
        try:
            for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not raw_line.strip():
                    continue
                parsed = json.loads(raw_line)
                if not isinstance(parsed, dict):
                    raise ValueError(f"第 {line_number} 行不是 JSON 对象")
                _feedback_row(parsed)
                records.append(parsed)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise FeedbackStoreError(f"JSONL 迁移校验失败: {exc}") from exc
        return self.save_many(records)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def append_feedback_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Explicit compatibility export; SQLite is the default store."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _feedback_row(record: dict[str, Any]) -> tuple[Any, ...]:
    attribution = record.get("attribution")
    if not isinstance(attribution, dict):
        raise ValueError("feedback attribution 必须是对象")
    required = ("feedback_id", "feedback_version", "run_id", "created_at", "rating", "source_status")
    missing = [key for key in required if not isinstance(record.get(key), str) or not record[key]]
    if missing:
        raise ValueError("feedback 缺少字段: " + ", ".join(missing))
    rating = str(record["rating"])
    if rating not in VALID_RATINGS:
        raise ValueError("feedback rating 无效")
    category = attribution.get("category")
    signals = attribution.get("signals")
    if not isinstance(category, str) or not category or not isinstance(signals, list):
        raise ValueError("feedback attribution 字段无效")
    return (
        record["feedback_id"],
        record["feedback_version"],
        record["run_id"],
        record["created_at"],
        rating,
        record["source_status"],
        category,
        json.dumps(signals, ensure_ascii=False, sort_keys=True),
        attribution.get("user_reason"),
        record.get("comment"),
        json.dumps(record, ensure_ascii=False, sort_keys=True),
        datetime.now(timezone.utc).isoformat(),
    )


def _attribute_feedback(
    result: dict[str, Any],
    rating: str,
    reason: str | None,
) -> tuple[str, list[str]]:
    status = str(result.get("status", "unknown"))
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    steps = trace.get("steps") if isinstance(trace.get("steps"), list) else []
    signals = [f"status={status}"]
    if steps:
        signals.append(f"last_action={steps[-1].get('action', 'unknown')}")
    if reason:
        return reason, signals + ["source=user_reason"]
    if rating == "helpful":
        return "confirmed_helpful", signals
    if status == "tool_error":
        return "tool_failure", signals
    if status == "insufficient_evidence":
        return "knowledge_gap", signals
    if status == "needs_clarification":
        return "unresolved_policy_conflict", signals
    if status == "max_steps_exceeded":
        validation_seen = any(
            "校验未通过" in str(step.get("observation_summary", ""))
            for step in steps
        )
        return ("validation_or_generation_failure" if validation_seen else "execution_budget"), signals
    if status == "completed":
        return "answer_quality", signals
    if status in {"invalid_input", "not_applicable"}:
        return "input_or_routing", signals
    return "unclassified", signals


def _stable_run_id(result: dict[str, Any]) -> str:
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "run_" + hashlib.sha256(payload).hexdigest()[:16]


def _feedback_id(run_id: str, rating: str, seed: str) -> str:
    raw = f"{run_id}|{rating}|{seed}".encode("utf-8")
    return "fb_" + hashlib.sha256(raw).hexdigest()[:16]
