from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest

from enterprise_qa_agent.agent import EnterpriseQAAgent, result_to_dict
from enterprise_qa_agent.config import AgentConfig
from enterprise_qa_agent.feedback import (
    FeedbackStoreError,
    SQLiteFeedbackStore,
    append_feedback_jsonl,
    build_feedback_record,
)
from enterprise_qa_agent.memory import retrieve_history_context
from enterprise_qa_agent.models import (
    ActionType,
    AgentAction,
    ChatMessage,
    QueryRequest,
    TaskStatus,
)
from enterprise_qa_agent.policy import (
    ModelError,
    OfflineMockModel,
    _ACTION_TOOL_SCHEMAS,
    _parse_native_tool_call,
    parse_action_payload,
)
from enterprise_qa_agent.repository import KnowledgeBase, KnowledgeBaseError
from enterprise_qa_agent.retrieval import HybridRetriever
from enterprise_qa_agent.tools import KnowledgeTools


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = json.loads((ROOT / "data" / "documents.json").read_text(encoding="utf-8"))
SYNONYMS = ROOT / "data" / "synonyms.json"


def agent_from_data(
    data,
    *,
    model: OfflineMockModel | None = None,
    config: AgentConfig | None = None,
    failures: dict[str, int] | None = None,
    retry_sleeper=None,
) -> EnterpriseQAAgent:
    active_config = config or AgentConfig()
    knowledge_base = KnowledgeBase.from_data(data)
    retriever = HybridRetriever.from_synonym_file(
        knowledge_base,
        SYNONYMS,
        min_score=active_config.search_min_score,
        conflict_margin=active_config.conflict_score_margin,
    )
    agent_kwargs = {"retry_sleeper": retry_sleeper} if retry_sleeper is not None else {}
    return EnterpriseQAAgent(
        active_config,
        model or OfflineMockModel(),
        knowledge_base,
        KnowledgeTools(knowledge_base, retriever, injected_failures=failures),
        **agent_kwargs,
    )


def special_override_documents():
    data = deepcopy(DOCUMENTS[:2])
    data[1]["scope"]["employee_types"] = ["试用期员工"]
    data[1]["rule_overrides"] = [
        {
            "source_chunk_id": "D02-C01",
            "target_document_id": "D01",
            "target_chunk_id": "D01-C01",
            "rule_key": "expense.submission_deadline",
            "reason": "试用期员工采用单独的快速报销规则",
        }
    ]
    return data


def cross_chunk_document():
    return [
        {
            "document_id": "D10",
            "title": "企业设备借用管理办法",
            "version_date": "2026-01-01",
            "effective_from": "2026-01-01",
            "effective_to": None,
            "topic": "device_borrowing",
            "scope": {
                "employee_types": ["all"],
                "regions": ["all"],
                "business_types": ["all"],
            },
            "priority": None,
            "supersedes": [],
            "rule_overrides": [],
            "access_control": {"departments": ["all"]},
            "keywords": ["设备借用", "申请条件"],
            "chunks": [
                {
                    "chunk_id": "D10-C01",
                    "section": "申请条件",
                    "rule_key": "device_borrowing.application",
                    "keywords": ["设备借用", "申请条件"],
                    "required_chunk_ids": ["D10-C04"],
                    "text": "设备借用申请应同时满足资格定义章节的要求。",
                },
                {
                    "chunk_id": "D10-C02",
                    "section": "申请后的领取流程",
                    "rule_key": "device_borrowing.pickup",
                    "keywords": ["设备借用申请", "领取"],
                    "text": "设备借用申请批准后应到服务台领取设备。",
                },
                {
                    "chunk_id": "D10-C03",
                    "section": "申请后的归还流程",
                    "rule_key": "device_borrowing.return",
                    "keywords": ["设备借用申请", "归还"],
                    "text": "设备借用申请结束后应在服务台完成归还。",
                },
                {
                    "chunk_id": "D10-C04",
                    "section": "附录：资格定义",
                    "rule_key": "device_borrowing.eligibility",
                    "keywords": ["连续任职", "安全培训"],
                    "text": "申请人须连续任职满6个月并完成安全培训。",
                },
            ],
        }
    ]


def cross_document_dependency_documents(
    *,
    target_departments=None,
    target_effective_from="2026-01-01",
):
    return [
        {
            "document_id": "D20",
            "title": "设备借用申请办法",
            "version_date": "2026-01-01",
            "effective_from": "2026-01-01",
            "effective_to": None,
            "topic": "device_borrowing",
            "scope": {
                "employee_types": ["all"],
                "regions": ["all"],
                "business_types": ["all"],
            },
            "priority": None,
            "supersedes": [],
            "rule_overrides": [],
            "access_control": {"departments": ["all"]},
            "keywords": ["设备借用", "申请条件"],
            "chunks": [
                {
                    "chunk_id": "D20-C01",
                    "section": "申请条件",
                    "rule_key": "device_borrowing.application",
                    "keywords": ["设备借用", "申请条件"],
                    "required_evidence_refs": [
                        {"document_id": "D21", "chunk_id": "D21-C01"}
                    ],
                    "text": "设备借用资格以关联资格标准为准。",
                }
            ],
        },
        {
            "document_id": "D21",
            "title": "人员资格基线附录",
            "version_date": target_effective_from,
            "effective_from": target_effective_from,
            "effective_to": None,
            "topic": "eligibility_baseline",
            "scope": {
                "employee_types": ["all"],
                "regions": ["all"],
                "business_types": ["all"],
            },
            "priority": None,
            "supersedes": [],
            "rule_overrides": [],
            "access_control": {"departments": target_departments or ["all"]},
            "keywords": ["连续任职", "安全培训"],
            "chunks": [
                {
                    "chunk_id": "D21-C01",
                    "section": "资格基线",
                    "rule_key": "employee.eligibility_baseline",
                    "keywords": ["连续任职", "安全培训"],
                    "text": "申请人须连续任职满6个月并完成安全培训。",
                }
            ],
        },
    ]


class FabricatedEvidenceModel(OfflineMockModel):
    def next_action(self, state):
        if state.evidence_store:
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [{"text": "员工可以获得无限特权。", "evidence_ids": ["E999"]}],
                    "gaps": [],
                },
                "提交一个不存在的证据编号以测试守卫。",
            )
        return super().next_action(state)


class UnsupportedTextModel(OfflineMockModel):
    def next_action(self, state):
        if state.evidence_store:
            evidence_id = next(iter(state.evidence_store))
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [{"text": "员工拥有无限特权。", "evidence_ids": [evidence_id]}],
                    "gaps": [],
                },
                "提交与原文无关的声明以测试文本支持度。",
            )
        return super().next_action(state)


class DurationUnitModel(OfflineMockModel):
    def __init__(self, target_unit: str) -> None:
        self.target_unit = target_unit

    def next_action(self, state):
        if state.evidence_store:
            evidence_id, evidence = next(iter(state.evidence_store.items()))
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [
                        {
                            "text": evidence.text.replace("30天", f"30{self.target_unit}"),
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "gaps": [],
                },
                "改写期限单位以测试语义事实校验。",
            )
        return super().next_action(state)


class ContradictoryAnswerModel(OfflineMockModel):
    def __init__(self, style: str) -> None:
        self.style = style

    def next_action(self, state):
        if state.evidence_store:
            evidence_id = next(iter(state.evidence_store))
            if self.style == "single_claim":
                claims = [
                    {
                        "text": "员工可以申请远程办公，但员工不能申请远程办公。",
                        "evidence_ids": [evidence_id],
                    }
                ]
            elif self.style == "scoped":
                claims = [
                    {
                        "text": "完成转正的正式员工可以申请远程办公。",
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "text": "试用期员工暂不适用远程办公安排。",
                        "evidence_ids": [evidence_id],
                    },
                ]
            else:
                claims = [
                    {"text": "员工可以申请远程办公。", "evidence_ids": [evidence_id]},
                    {"text": "员工不能申请远程办公。", "evidence_ids": [evidence_id]},
                ]
            return AgentAction(
                ActionType.FINISH,
                {"status": "completed", "claims": claims, "gaps": []},
                "提交相反规则立场以测试回答一致性守卫。",
            )
        return super().next_action(state)


class PrematureCrossChunkFinishModel(OfflineMockModel):
    def __init__(self) -> None:
        self.attempted = False

    def next_action(self, state):
        if state.evidence_store and not self.attempted:
            self.attempted = True
            evidence_id, evidence = next(iter(state.evidence_store.items()))
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [{"text": evidence.text, "evidence_ids": [evidence_id]}],
                    "gaps": [],
                },
                "故意只使用主条款，测试关联片段读取守卫。",
            )
        return super().next_action(state)


class MissingRequiredCitationModel(OfflineMockModel):
    def next_action(self, state):
        primary = next(
            (
                (evidence_id, evidence)
                for evidence_id, evidence in state.evidence_store.items()
                if evidence.chunk_id == "D10-C01"
            ),
            None,
        )
        dependency_read = any(
            evidence.chunk_id == "D10-C04" for evidence in state.evidence_store.values()
        )
        if primary is not None and dependency_read:
            evidence_id, evidence = primary
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [{"text": evidence.text, "evidence_ids": [evidence_id]}],
                    "gaps": [],
                },
                "故意遗漏已读取的关联证据引用。",
            )
        return super().next_action(state)


class RepeatedRetryModel(OfflineMockModel):
    def next_action(self, state):
        failures = [item for item in state.tool_observations if not item.ok]
        if failures and len(failures) < 4:
            last = failures[-1]
            return AgentAction(
                ActionType(last.tool_name),
                dict(last.arguments),
                "继续重试以验证指数退避和上限。",
            )
        if len(failures) >= 4:
            return AgentAction(
                ActionType.FINISH,
                {"status": "tool_error", "claims": [], "gaps": ["检索持续失败。"]},
                "多次退避重试后仍失败。",
            )
        return super().next_action(state)


class VerboseAnswerModel(OfflineMockModel):
    def __init__(self, style: str) -> None:
        self.style = style

    def next_action(self, state):
        if state.evidence_store:
            evidence_id, evidence = next(iter(state.evidence_store.items()))
            if self.style == "many":
                claims = [
                    {"text": evidence.text, "evidence_ids": [evidence_id]}
                    for _ in range(7)
                ]
            else:
                claims = [
                    {
                        "text": evidence.text * 8,
                        "evidence_ids": [evidence_id],
                    }
                ]
            return AgentAction(
                ActionType.FINISH,
                {"status": "completed", "claims": claims, "gaps": []},
                "故意生成冗长回答以测试长度守卫。",
            )
        return super().next_action(state)


class MutatedConditionModel(OfflineMockModel):
    """Keeps most words and the same number but changes the triggering condition."""

    def next_action(self, state):
        if state.evidence_store:
            evidence_id = next(iter(state.evidence_store))
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [
                        {
                            "text": "员工应在审批通过之日起30天内提交报销。",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "gaps": [],
                },
                "改变条件但保留高文本相似度，以测试逐句证明守卫。",
            )
        return super().next_action(state)


class CitationLaunderingModel(OfflineMockModel):
    def next_action(self, state):
        if len(state.evidence_store) >= 2:
            evidence_ids = list(state.evidence_store)
            first = state.evidence_store[evidence_ids[0]]
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [
                        {
                            "text": first.text,
                            "evidence_ids": evidence_ids,
                        }
                    ],
                    "gaps": [],
                },
                "附加未参与声明证明的引用，以测试引用洗白守卫。",
            )
        return super().next_action(state)


class ConflictMisclassifiedAsGapModel(OfflineMockModel):
    def __init__(self) -> None:
        self.attempted = False

    def next_action(self, state):
        if (
            any(item.resolution_status == "unresolved" for item in state.detected_conflicts)
            and not self.attempted
        ):
            self.attempted = True
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "insufficient_evidence",
                    "claims": [],
                    "gaps": ["错误地把冲突归类为资料不足。"],
                },
                "故意错误终止以测试冲突状态守卫。",
            )
        return super().next_action(state)


class AcceptanceGuardTests(unittest.TestCase):
    def test_explicit_supersedes_resolves_conflict_with_both_sources_cited(self) -> None:
        data = deepcopy(DOCUMENTS)
        for document in data:
            if document["document_id"] == "D02":
                document["supersedes"] = ["D01"]

        result = agent_from_data(data).run(QueryRequest("报销期限多久？"))
        payload = result_to_dict(result)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("15天", result.answer)
        self.assertIn("明确替代", result.answer)
        self.assertEqual({item["document_id"] for item in payload["citations"]}, {"D01", "D02"})
        self.assertEqual(payload["conflicts"][0]["resolution_status"], "resolved")
        self.assertEqual(set(payload["conflicts"][0]["evidence_ids"]), {"C1", "C2"})

    def test_fabricated_evidence_id_cannot_complete(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(DOCUMENTS, model=FabricatedEvidenceModel(), config=config).run(
            QueryRequest("忘记密码怎么重置？")
        )

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(result.claims, [])
        self.assertEqual([item.document_id for item in result.citations], ["D05"])
        self.assertIn("不构成最终答案", result.answer)
        self.assertTrue(
            any("不存在的证据" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_low_textual_support_claim_cannot_complete(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(DOCUMENTS, model=UnsupportedTextModel(), config=config).run(
            QueryRequest("忘记密码怎么重置？")
        )

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("文本支持度不足" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_business_day_cannot_replace_calendar_day_in_citation(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[0])],
            model=DurationUnitModel("个工作日"),
            config=config,
        ).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("原文不支持的语义事实：30工作日" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_day_and_calendar_day_equivalent_wording_is_allowed(self) -> None:
        result = agent_from_data(
            [deepcopy(DOCUMENTS[0])],
            model=DurationUnitModel("日"),
        ).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("30日", result.answer)
        self.assertEqual(result.citations[0].quote, "员工应在费用发生之日起30天内提交报销。")
        self.assertEqual(
            result.support_certificates[0].proof_methods,
            ("canonical_fact_equivalence",),
        )
        self.assertEqual(result.support_certificates[0].evidence_ids, ("C1",))

    def test_high_overlap_condition_mutation_cannot_complete(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[0])],
            model=MutatedConditionModel(),
            config=config,
        ).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any(
                "无法由绑定原文逐句证明" in step["observation_summary"]
                for step in result.trace["steps"]
            )
        )

    def test_unused_evidence_cannot_launder_a_claim(self) -> None:
        data = deepcopy(DOCUMENTS[:2])
        data[1]["chunks"][0]["text"] = data[0]["chunks"][0]["text"]
        result = agent_from_data(
            data,
            model=CitationLaunderingModel(),
            config=replace(AgentConfig(), max_decision_steps=6),
        ).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any(
                "未参与逐句证明" in step["observation_summary"]
                for step in result.trace["steps"]
            )
        )

    def test_duplicate_document_id_is_rejected(self) -> None:
        data = deepcopy(DOCUMENTS)
        data.append(deepcopy(data[0]))
        with self.assertRaisesRegex(KnowledgeBaseError, "文档 ID 重复"):
            KnowledgeBase.from_data(data)

    def test_empty_question_returns_invalid_input_without_tools(self) -> None:
        result = agent_from_data(DOCUMENTS).run(QueryRequest("   "))

        self.assertEqual(result.status, TaskStatus.INVALID_INPUT)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.trace["steps"], [])

    def test_department_acl_filters_before_retrieval_and_trace(self) -> None:
        hr_result = agent_from_data(DOCUMENTS).run(
            QueryRequest(
                "年度薪酬调整需要什么审批？",
                requester_departments=("HR",),
            )
        )
        sales_result = agent_from_data(DOCUMENTS).run(
            QueryRequest(
                "年度薪酬调整需要什么审批？",
                requester_departments=("sales",),
            )
        )

        self.assertEqual(hr_result.status, TaskStatus.COMPLETED)
        self.assertEqual(hr_result.citations[0].document_id, "D06")
        self.assertEqual(hr_result.trace["access_context"]["departments"], ["hr"])
        self.assertEqual(sales_result.status, TaskStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(sales_result.citations, [])
        self.assertNotIn("D06", json.dumps(sales_result.trace, ensure_ascii=False))
        self.assertEqual(sales_result.trace["access_context"]["departments"], ["sales"])

    def test_document_without_acl_is_rejected_by_secure_default(self) -> None:
        data = deepcopy(DOCUMENTS)
        data[0].pop("access_control")
        with self.assertRaisesRegex(KnowledgeBaseError, "缺少 access_control"):
            KnowledgeBase.from_data(data)

    def test_effective_period_changes_retrieved_rule_by_request_date(self) -> None:
        data = deepcopy(DOCUMENTS[:2])
        data[0]["effective_to"] = "2026-12-31"
        data[1]["effective_from"] = "2027-01-01"

        current = agent_from_data(data).run(
            QueryRequest("报销期限多久？", as_of_date="2026-11-06")
        )
        future = agent_from_data(data).run(
            QueryRequest("报销期限多久？", as_of_date="2027-01-01")
        )

        self.assertEqual(current.status, TaskStatus.COMPLETED)
        self.assertIn("30天", current.answer)
        self.assertEqual([item.document_id for item in current.citations], ["D01"])
        self.assertNotIn("D02", json.dumps(current.trace, ensure_ascii=False))
        self.assertEqual(current.trace["access_context"]["as_of_date"], "2026-11-06")

        self.assertEqual(future.status, TaskStatus.COMPLETED)
        self.assertIn("15天", future.answer)
        self.assertEqual([item.document_id for item in future.citations], ["D02"])
        self.assertNotIn("D01", json.dumps(future.trace, ensure_ascii=False))
        self.assertEqual(future.trace["access_context"]["as_of_date"], "2027-01-01")

    def test_document_without_effective_from_is_rejected(self) -> None:
        data = deepcopy(DOCUMENTS)
        data[0].pop("effective_from")
        with self.assertRaisesRegex(KnowledgeBaseError, "缺少非空字段 effective_from"):
            KnowledgeBase.from_data(data)

    def test_effective_period_end_cannot_precede_start(self) -> None:
        data = deepcopy(DOCUMENTS)
        data[0]["effective_from"] = "2026-12-01"
        data[0]["effective_to"] = "2026-11-30"
        with self.assertRaisesRegex(KnowledgeBaseError, "effective_to 不能早于 effective_from"):
            KnowledgeBase.from_data(data)

    def test_equivalent_duration_wording_is_not_a_conflict(self) -> None:
        data = deepcopy(DOCUMENTS[:2])
        data[0]["chunks"][0]["text"] = "员工应在费用发生之日起30天内提交报销。"
        data[1]["chunks"][0]["text"] = "员工须于费用发生后30日内完成报销提交。"

        result = agent_from_data(data).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.conflicts, [])
        self.assertEqual({item.document_id for item in result.citations}, {"D01"})

    def test_indirect_negative_wording_is_detected_as_conflict(self) -> None:
        data = deepcopy(DOCUMENTS[:2])
        for index, document in enumerate(data, 1):
            document["title"] = f"远程办公规则 {index}"
            document["topic"] = "remote_work"
            document["keywords"] = ["远程办公", "申请", "安排"]
            chunk = document["chunks"][0]
            chunk["section"] = "申请资格"
            chunk["rule_key"] = "remote_work.eligibility"
            chunk["keywords"] = ["远程办公", "申请"]
        data[0]["chunks"][0]["text"] = "符合条件的员工可以申请远程办公。"
        data[1]["chunks"][0]["text"] = "员工的远程办公申请原则上不予安排。"

        result = agent_from_data(data).run(QueryRequest("员工能申请远程办公吗？"))

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].values, ("规则立场: 允许", "规则立场: 禁止"))
        self.assertIn("规则立场", result.conflicts[0].description)
        self.assertEqual({item.document_id for item in result.citations}, {"D01", "D02"})

    def test_explicit_special_rule_wins_when_query_matches_narrow_scope(self) -> None:
        result = agent_from_data(special_override_documents()).run(
            QueryRequest("试用期员工报销期限多久？")
        )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("15天", result.answer)
        self.assertNotIn("30天内提交报销。[", result.answer)
        self.assertEqual({item.document_id for item in result.citations}, {"D01", "D02"})
        self.assertEqual(result.conflicts[0].resolution_status, "resolved")
        self.assertIn("问题命中特例范围 员工=试用期员工", result.conflicts[0].resolution_basis)

    def test_special_rule_stays_unresolved_when_query_omits_scope(self) -> None:
        result = agent_from_data(special_override_documents()).run(
            QueryRequest("报销期限多久？")
        )

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(result.conflicts[0].resolution_status, "unresolved")
        self.assertIn("问题未明确命中", result.conflicts[0].resolution_basis)
        self.assertIn("30天", result.answer)
        self.assertIn("15天", result.answer)

    def test_conflict_output_lists_scope_for_each_rule(self) -> None:
        result = agent_from_data(special_override_documents()).run(
            QueryRequest("报销期限多久？")
        )

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(
            result.conflicts[0].scope_summaries,
            (
                "员工=全部;地区=全部;业务=全部",
                "员工=试用期员工;地区=全部;业务=全部",
            ),
        )
        self.assertIn("D01（适用范围：员工=全部", result.answer)
        self.assertIn("D02（适用范围：员工=试用期员工", result.answer)

    def test_unresolved_conflict_cannot_be_misclassified_as_insufficient_evidence(self) -> None:
        model = ConflictMisclassifiedAsGapModel()
        result = agent_from_data(deepcopy(DOCUMENTS[:2]), model=model).run(
            QueryRequest("报销期限多久？")
        )

        self.assertTrue(model.attempted)
        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertTrue(
            any(
                "必须调用 ask_user" in step["observation_summary"]
                for step in result.trace["steps"]
            )
        )

    def test_regular_conflict_also_exposes_aligned_scope_metadata(self) -> None:
        result = agent_from_data(deepcopy(DOCUMENTS[:2])).run(
            QueryRequest("报销期限多久？")
        )

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(len(result.conflicts[0].scope_summaries), 2)
        self.assertEqual(len(result.conflicts[0].scope_summaries), len(result.conflicts[0].evidence_ids))
        self.assertTrue(all("员工=" in item for item in result.conflicts[0].scope_summaries))

    def test_special_override_requires_strictly_narrower_scope(self) -> None:
        data = special_override_documents()
        data[1]["scope"]["employee_types"] = ["all"]

        with self.assertRaisesRegex(KnowledgeBaseError, "特例范围必须是目标文档范围的严格子集"):
            KnowledgeBase.from_data(data)

    def test_priority_does_not_auto_resolve_different_scopes(self) -> None:
        data = special_override_documents()
        data[1]["rule_overrides"] = []
        data[0]["priority"] = 100
        data[1]["priority"] = 1

        result = agent_from_data(data).run(QueryRequest("试用期员工报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(result.conflicts[0].resolution_status, "unresolved")
        self.assertIsNone(result.conflicts[0].resolution_basis)

    def test_contradictory_claims_cannot_complete(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[2])],
            model=ContradictoryAnswerModel("multiple_claims"),
            config=config,
        ).run(QueryRequest("远程办公制度如何规定？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("互斥结论" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_contradictory_clauses_in_one_claim_cannot_complete(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[2])],
            model=ContradictoryAnswerModel("single_claim"),
            config=config,
        ).run(QueryRequest("远程办公制度如何规定？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("互斥结论" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_different_explicit_scopes_are_not_self_contradictory(self) -> None:
        result = agent_from_data(
            [deepcopy(DOCUMENTS[2])],
            model=ContradictoryAnswerModel("scoped"),
        ).run(QueryRequest("正式员工和试用期员工的远程办公规则？"))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(len(result.claims), 2)
        self.assertIn("完成转正的正式员工可以", result.answer)
        self.assertIn("试用期员工暂不适用", result.answer)

    def test_cross_chunk_dependency_is_retrieved_read_and_cited(self) -> None:
        result = agent_from_data(cross_chunk_document()).run(
            QueryRequest("设备借用申请条件是什么？")
        )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(
            {item.chunk_id for item in result.citations},
            {"D10-C01", "D10-C04"},
        )
        self.assertIn("资格定义章节", result.answer)
        self.assertIn("连续任职满6个月", result.answer)

    def test_completion_guard_forces_required_chunk_read(self) -> None:
        model = PrematureCrossChunkFinishModel()
        result = agent_from_data(cross_chunk_document(), model=model).run(
            QueryRequest("设备借用申请条件是什么？")
        )

        self.assertTrue(model.attempted)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        actions = [step["action"] for step in result.trace["steps"]]
        self.assertEqual(actions[:4], ["search_docs", "read_doc", "finish", "read_doc"])
        self.assertEqual(result.trace["steps"][3]["arguments"]["chunk_id"], "D10-C04")
        self.assertIn("跨章节原文", result.trace["steps"][3]["rationale"])

    def test_read_dependency_cannot_be_omitted_from_final_citations(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=8)
        result = agent_from_data(
            cross_chunk_document(),
            model=MissingRequiredCitationModel(),
            config=config,
        ).run(QueryRequest("设备借用申请条件是什么？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("未引用其关联证据" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_invalid_or_cyclic_chunk_dependencies_are_rejected(self) -> None:
        missing = cross_chunk_document()
        missing[0]["chunks"][0]["required_chunk_ids"] = ["D10-C99"]
        with self.assertRaisesRegex(KnowledgeBaseError, "不存在的 required_evidence_ref"):
            KnowledgeBase.from_data(missing)

        cyclic = cross_chunk_document()
        cyclic[0]["chunks"][3]["required_chunk_ids"] = ["D10-C01"]
        with self.assertRaisesRegex(KnowledgeBaseError, "循环依赖"):
            KnowledgeBase.from_data(cyclic)

    def test_cross_document_dependency_is_retrieved_read_and_cited(self) -> None:
        data = cross_document_dependency_documents()
        knowledge_base = KnowledgeBase.from_data(data)
        retriever = HybridRetriever.from_synonym_file(knowledge_base, SYNONYMS)
        hits = retriever.search(
            "设备借用申请条件是什么？",
            requester_departments=("public",),
            as_of_date="2026-11-07",
        )
        dependency_hit = next(item for item in hits if item.document_id == "D21")
        self.assertTrue(any("证据依赖" in reason for reason in dependency_hit.match_reasons))

        result = agent_from_data(data).run(
            QueryRequest("设备借用申请条件是什么？", as_of_date="2026-11-07")
        )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual({item.document_id for item in result.citations}, {"D20", "D21"})
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "read_doc", "read_doc", "finish"],
        )
        self.assertIn("连续任职满6个月", result.answer)
        self.assertEqual(len(result.support_certificates), 2)

    def test_cross_document_dependency_respects_acl_without_leaking_target(self) -> None:
        result = agent_from_data(
            cross_document_dependency_documents(target_departments=["hr"])
        ).run(
            QueryRequest(
                "设备借用申请条件是什么？",
                requester_departments=("sales",),
                as_of_date="2026-11-07",
            )
        )
        payload = result_to_dict(result)

        self.assertEqual(result.status, TaskStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.citations, [])
        self.assertNotIn("D21", json.dumps(payload, ensure_ascii=False))
        self.assertIn("当前权限或规则有效期下不可用", result.gaps[0])

    def test_cross_document_dependency_respects_effective_date_without_leak(self) -> None:
        result = agent_from_data(
            cross_document_dependency_documents(target_effective_from="2027-01-01")
        ).run(QueryRequest("设备借用申请条件是什么？", as_of_date="2026-11-07"))
        payload = result_to_dict(result)

        self.assertEqual(result.status, TaskStatus.INSUFFICIENT_EVIDENCE)
        self.assertNotIn("D21", json.dumps(payload, ensure_ascii=False))

    def test_missing_and_cyclic_cross_document_dependencies_are_rejected(self) -> None:
        missing = cross_document_dependency_documents()
        missing[0]["chunks"][0]["required_evidence_refs"][0]["document_id"] = "D99"
        with self.assertRaisesRegex(KnowledgeBaseError, "D99/D21-C01"):
            KnowledgeBase.from_data(missing)

        cyclic = cross_document_dependency_documents()
        cyclic[1]["chunks"][0]["required_evidence_refs"] = [
            {"document_id": "D20", "chunk_id": "D20-C01"}
        ]
        with self.assertRaisesRegex(KnowledgeBaseError, "跨文档或文档内循环依赖"):
            KnowledgeBase.from_data(cyclic)

    def test_step_limit_returns_collected_evidence_as_non_final_partial_result(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=2)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[4])],
            config=config,
        ).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(result.claims, [])
        self.assertEqual([item.document_id for item in result.citations], ["D05"])
        self.assertIn("已收集 1 条原文证据", result.answer)
        self.assertIn("不构成最终答案", result.answer)
        self.assertTrue(any("继续检查" in item for item in result.gaps))

    def test_step_limit_preserves_detected_conflict_and_both_quotes(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=3)
        result = agent_from_data(
            deepcopy(DOCUMENTS[:2]),
            config=config,
        ).run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual({item.document_id for item in result.citations}, {"D01", "D02"})
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].resolution_status, "unresolved")
        self.assertIn("已收集 2 条原文证据", result.answer)

    def test_retry_uses_exponential_backoff_with_cap(self) -> None:
        delays: list[float] = []
        config = replace(
            AgentConfig(),
            max_decision_steps=6,
            max_tool_calls=5,
            retry_base_delay_seconds=0.1,
            retry_max_delay_seconds=0.25,
        )
        result = agent_from_data(
            [deepcopy(DOCUMENTS[4])],
            model=RepeatedRetryModel(),
            config=config,
            failures={"search_docs": 4},
            retry_sleeper=delays.append,
        ).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.TOOL_ERROR)
        self.assertEqual(delays, [0.1, 0.2, 0.25])
        retry_summaries = [
            step["observation_summary"]
            for step in result.trace["steps"]
            if "退避" in step["observation_summary"]
        ]
        self.assertEqual(len(retry_summaries), 3)
        self.assertIn("退避 0.25 秒", retry_summaries[-1])

    def test_offline_and_live_share_canonical_tool_call_envelope(self) -> None:
        action = parse_action_payload(
            {
                "tool_call": {
                    "name": "search_docs",
                    "arguments": {"query": "密码重置", "top_k": 5},
                },
                "rationale": "需要先检索。",
            }
        )
        native = _parse_native_tool_call(
            {
                "content": "读取最高相关候选。",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_doc",
                            "arguments": json.dumps(
                                {"document_id": "D05", "chunk_id": "D05-C01"}
                            ),
                        }
                    }
                ],
            }
        )
        native_action = parse_action_payload(native)

        self.assertEqual(action.action, ActionType.SEARCH_DOCS)
        self.assertEqual(action.arguments["top_k"], 5)
        self.assertEqual(native_action.action, ActionType.READ_DOC)
        self.assertEqual(native_action.arguments["chunk_id"], "D05-C01")
        self.assertEqual(
            {item["function"]["name"] for item in _ACTION_TOOL_SCHEMAS},
            {"search_docs", "read_doc", "ask_user", "finish"},
        )

    def test_invalid_native_tool_call_is_rejected_before_execution(self) -> None:
        with self.assertRaises(ModelError):
            parse_action_payload(
                {
                    "tool_call": {
                        "name": "read_doc",
                        "arguments": "not-json",
                    }
                }
            )
        with self.assertRaises(ModelError):
            _parse_native_tool_call({"content": None, "tool_calls": []})

    def test_direct_finish_schema_is_normalized_for_compatible_models(self) -> None:
        action = parse_action_payload(
            {
                "status": "completed",
                "claims": [{"text": "已核验。", "evidence_ids": ["E1"]}],
                "gaps": [],
            }
        )

        self.assertEqual(action.action, ActionType.FINISH)
        self.assertEqual(action.arguments["status"], "completed")
        with self.assertRaises(ModelError):
            parse_action_payload({"status": "completed", "claims": []})

    def test_parallel_native_calls_are_serialized_to_one_react_action(self) -> None:
        payload = _parse_native_tool_call(
            {
                "content": None,
                "tool_calls": [
                    {"function": {"name": "read_doc", "arguments": '{"document_id":"D01","chunk_id":"D01-C01"}'}},
                    {"function": {"name": "read_doc", "arguments": '{"document_id":"D02","chunk_id":"D02-C01"}'}},
                ],
            }
        )
        action = parse_action_payload(payload)

        self.assertEqual(action.action, ActionType.READ_DOC)
        self.assertEqual(action.arguments["document_id"], "D01")
        self.assertIn("先执行第一个", action.rationale)

    def test_long_history_retrieves_bounded_verbatim_memories_with_provenance(self) -> None:
        history = tuple(
            [ChatMessage("user", "远程办公制度适用于哪些员工？")]
            + [
                ChatMessage("assistant", "我会核验制度。"),
                ChatMessage("user", "好的"),
            ]
            * 8
        )
        context = retrieve_history_context(
            history,
            "那试用期员工呢？",
            recent_limit=6,
            memory_limit=4,
        )

        self.assertEqual(len(context["recent_messages"]), 6)
        self.assertLessEqual(len(context["retrieved_memories"]), 4)
        self.assertNotIn("summary", context)
        memory = next(
            item
            for item in context["retrieved_memories"]
            if "远程办公制度" in item["content"]
        )
        self.assertEqual(memory["message_index"], 0)
        self.assertEqual(memory["content"], history[0].content)
        self.assertEqual(len(memory["content_sha256"]), 64)

    def test_long_dialogue_follow_up_recovers_substantive_topic_anchor(self) -> None:
        history = tuple(
            [ChatMessage("user", "远程办公制度适用于哪些员工？")]
            + [
                ChatMessage("assistant", "我会继续核验。"),
                ChatMessage("user", "好的"),
            ]
            * 6
        )
        result = agent_from_data([deepcopy(DOCUMENTS[2])]).run(
            QueryRequest("那试用期员工呢？", history)
        )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("远程办公制度", result.trace["standalone_query"])
        self.assertIn("试用期员工", result.trace["standalone_query"])
        self.assertIn("保留原文和消息序号", result.trace["resolution_note"])
        self.assertEqual(
            result.trace["history_context"]["memory_strategy"],
            "provenance_preserving_episodic_retrieval_v1",
        )
        self.assertIn(0, result.trace["history_context"]["retrieved_message_indexes"])

    def test_history_memory_selects_relevant_old_topic_over_newer_unrelated_topics(self) -> None:
        history = (
            ChatMessage("user", "远程办公制度适用于哪些员工？"),
            ChatMessage("assistant", "我会核验远程办公制度。"),
            ChatMessage("user", "设备质保期是多久？"),
            ChatMessage("assistant", "我会核验设备资料。"),
            ChatMessage("user", "密码如何重置？"),
            ChatMessage("assistant", "我会核验账号资料。"),
            ChatMessage("user", "好的"),
            ChatMessage("assistant", "请继续。"),
        )
        context = retrieve_history_context(
            history,
            "那试用期员工呢？",
            recent_limit=2,
            memory_limit=2,
        )

        self.assertEqual(context["primary_anchor_index"], 0)
        self.assertIn(0, context["retrieved_message_indexes"])
        self.assertEqual(context["primary_anchor"], history[0].content)

    def test_single_claim_length_limit_blocks_verbose_answer(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4, max_claim_chars=120)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[4])],
            model=VerboseAnswerModel("long"),
            config=config,
        ).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("声明 1 长度超过上限" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_claim_count_limit_blocks_repetitive_answer(self) -> None:
        config = replace(AgentConfig(), max_decision_steps=4, max_claims=6)
        result = agent_from_data(
            [deepcopy(DOCUMENTS[4])],
            model=VerboseAnswerModel("many"),
            config=config,
        ).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertTrue(
            any("声明数量超过上限 6" in step["observation_summary"] for step in result.trace["steps"])
        )

    def test_feedback_is_linked_to_run_and_automatically_attributed(self) -> None:
        completed = agent_from_data([deepcopy(DOCUMENTS[4])]).run(
            QueryRequest("忘记密码怎么重置？")
        )
        completed_payload = result_to_dict(completed)
        record = build_feedback_record(
            completed_payload,
            "unhelpful",
            created_at="2026-11-07T00:00:00+00:00",
        )

        self.assertTrue(completed.trace["run_id"].startswith("run_"))
        self.assertEqual(record["run_id"], completed.trace["run_id"])
        self.assertEqual(record["attribution"]["category"], "answer_quality")

        cases = {
            "tool_error": "tool_failure",
            "insufficient_evidence": "knowledge_gap",
            "needs_clarification": "unresolved_policy_conflict",
        }
        for status, category in cases.items():
            with self.subTest(status=status):
                attributed = build_feedback_record(
                    {"status": status, "trace": {"steps": []}},
                    "unhelpful",
                    created_at="2026-11-07T00:00:00+00:00",
                )
                self.assertEqual(attributed["attribution"]["category"], category)

    def test_feedback_store_uses_transactional_indexed_sqlite_and_is_idempotent(self) -> None:
        result = {
            "status": "completed",
            "trace": {"run_id": "run_test", "steps": []},
        }
        target = ROOT / "examples" / ".feedback-test.sqlite3"
        candidates = [target, Path(str(target) + "-wal"), Path(str(target) + "-shm")]
        for candidate in candidates:
            if candidate.exists():
                candidate.unlink()
        try:
            first = build_feedback_record(
                result,
                "helpful",
                created_at="2026-11-07T00:00:00+00:00",
            )
            second = build_feedback_record(
                result,
                "unhelpful",
                reason="citation_quality",
                comment="引用范围不够明确",
                created_at="2026-11-07T00:01:00+00:00",
            )
            store = SQLiteFeedbackStore(target)
            self.assertTrue(store.save(first))
            self.assertTrue(store.save(second))
            self.assertFalse(store.save(second))
            records = store.list_by_run("run_test")
            summary = store.summary()
            stored_second = store.get(second["feedback_id"])
            connection = sqlite3.connect(target)
            try:
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list('feedback_records')")
                }
            finally:
                connection.close()
        finally:
            for candidate in candidates:
                if candidate.exists():
                    candidate.unlink()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["attribution"]["category"], "confirmed_helpful")
        self.assertEqual(records[1]["attribution"]["category"], "citation_quality")
        self.assertEqual(records[1]["comment"], "引用范围不够明确")
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_rating"], {"helpful": 1, "unhelpful": 1})
        self.assertEqual(summary["by_status"], {"completed": 2})
        self.assertEqual(stored_second, second)
        self.assertIn("idx_feedback_run_id", indexes)
        self.assertIn("idx_feedback_category_created", indexes)

    def test_feedback_jsonl_migration_is_atomic_and_deduplicated(self) -> None:
        source = ROOT / "examples" / ".feedback-migration-test.jsonl"
        target = ROOT / "examples" / ".feedback-migration-test.sqlite3"
        broken = ROOT / "examples" / ".feedback-migration-broken.jsonl"
        empty_target = ROOT / "examples" / ".feedback-migration-empty.sqlite3"
        candidates = [
            source,
            target,
            Path(str(target) + "-wal"),
            Path(str(target) + "-shm"),
            broken,
            empty_target,
            Path(str(empty_target) + "-wal"),
            Path(str(empty_target) + "-shm"),
        ]
        for candidate in candidates:
            if candidate.exists():
                candidate.unlink()
        try:
            first = build_feedback_record(
                {"status": "completed", "trace": {"run_id": "run_migrate", "steps": []}},
                "helpful",
                created_at="2026-11-07T00:00:00+00:00",
            )
            second = build_feedback_record(
                {"status": "tool_error", "trace": {"run_id": "run_error", "steps": []}},
                "unhelpful",
                created_at="2026-11-07T00:01:00+00:00",
            )
            append_feedback_jsonl(source, first)
            append_feedback_jsonl(source, second)
            store = SQLiteFeedbackStore(target)
            self.assertEqual(
                store.import_jsonl(source),
                {"total": 2, "inserted": 2, "duplicates": 0},
            )
            self.assertEqual(
                store.import_jsonl(source),
                {"total": 2, "inserted": 0, "duplicates": 2},
            )

            broken.write_text(
                json.dumps(first, ensure_ascii=False) + "\n{not-json}\n",
                encoding="utf-8",
            )
            broken_store = SQLiteFeedbackStore(empty_target)
            with self.assertRaises(FeedbackStoreError):
                broken_store.import_jsonl(broken)
            self.assertEqual(broken_store.summary()["total"], 0)
        finally:
            for candidate in candidates:
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
