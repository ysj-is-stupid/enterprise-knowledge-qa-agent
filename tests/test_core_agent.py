from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest

from enterprise_qa_agent.agent import EnterpriseQAAgent, result_to_dict
from enterprise_qa_agent.config import AgentConfig
from enterprise_qa_agent.models import ActionType, AgentAction, ChatMessage, QueryRequest, TaskStatus
from enterprise_qa_agent.policy import ModelError, OfflineMockModel, OpenAICompatibleModel
from enterprise_qa_agent.repository import KnowledgeBase
from enterprise_qa_agent.retrieval import HybridRetriever
from enterprise_qa_agent.tools import KnowledgeTools


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "data" / "documents.json"
SYNONYMS = ROOT / "data" / "synonyms.json"


def build_agent(
    *,
    document_path: Path = DOCUMENTS,
    knowledge_base: KnowledgeBase | None = None,
    failures: dict[str, int] | None = None,
    config: AgentConfig | None = None,
    model: OfflineMockModel | None = None,
) -> EnterpriseQAAgent:
    active_config = config or AgentConfig()
    active_knowledge_base = knowledge_base or KnowledgeBase.load(document_path)
    retriever = HybridRetriever.from_synonym_file(
        active_knowledge_base,
        SYNONYMS,
        min_score=active_config.search_min_score,
        conflict_margin=active_config.conflict_score_margin,
    )
    tools = KnowledgeTools(active_knowledge_base, retriever, injected_failures=failures)
    return EnterpriseQAAgent(active_config, model or OfflineMockModel(), active_knowledge_base, tools)


class PrematureFinishModel(OfflineMockModel):
    """Attempts to finish after one conflicting source, then follows validation."""

    def __init__(self) -> None:
        self.attempted = False

    def next_action(self, state):  # type annotation intentionally inherited from protocol
        if state.evidence_store and len(state.candidate_hits) > 1 and not self.attempted:
            self.attempted = True
            evidence_id, evidence = next(iter(state.evidence_store.items()))
            return AgentAction(
                ActionType.FINISH,
                {
                    "status": "completed",
                    "claims": [{"text": evidence.text, "evidence_ids": [evidence_id]}],
                    "gaps": [],
                },
                "尝试在只读取一份制度时提前完成。",
            )
        return super().next_action(state)


class CoreAgentTests(unittest.TestCase):
    def test_clear_answer_reads_source_and_cites_exact_quote(self) -> None:
        result = build_agent().run(QueryRequest("忘记密码怎么重置？"))
        payload = result_to_dict(result)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "read_doc", "finish"],
        )
        self.assertEqual(payload["citations"][0]["document_id"], "D05")
        self.assertIn(payload["citations"][0]["quote"], result.answer)
        self.assertEqual(payload["claims"][0]["citation_ids"], ["C1"])
        self.assertEqual(payload["support_certificates"][0]["proof_methods"], ("verbatim_clause",))
        self.assertEqual(payload["support_certificates"][0]["evidence_ids"], ("C1",))

    def test_conflict_path_reads_both_documents_and_asks_user(self) -> None:
        result = build_agent().run(QueryRequest("报销期限多久？"))

        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "read_doc", "read_doc", "ask_user"],
        )
        self.assertEqual({item.document_id for item in result.citations}, {"D01", "D02"})
        self.assertIn("30天", result.answer)
        self.assertIn("15天", result.answer)
        self.assertNotEqual(result.status, TaskStatus.COMPLETED)

    def test_out_of_scope_requires_supplementary_search(self) -> None:
        result = build_agent().run(QueryRequest("公司食堂明天吃什么？"))

        self.assertEqual(result.status, TaskStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "search_docs", "finish"],
        )
        queries = [
            step["arguments"]["query"]
            for step in result.trace["steps"]
            if step["action"] == "search_docs"
        ]
        self.assertEqual(len(set(queries)), 2)
        self.assertEqual(result.claims, [])

    def test_persistent_tool_failure_retries_then_stops_without_claims(self) -> None:
        result = build_agent(failures={"search_docs": 2}).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.TOOL_ERROR)
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "search_docs", "finish"],
        )
        self.assertIn("重试一次", result.trace["steps"][1]["rationale"])
        self.assertEqual(result.claims, [])
        self.assertEqual(result.citations, [])

    def test_transient_tool_failure_recovers_after_model_retry(self) -> None:
        result = build_agent(failures={"search_docs": 1}).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(
            [step["action"] for step in result.trace["steps"]],
            ["search_docs", "search_docs", "read_doc", "finish"],
        )

    def test_paraphrased_question_is_retrieved_by_synonym_expansion(self) -> None:
        result = build_agent().run(QueryRequest("买的设备坏了怎么办？"))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.citations[0].document_id, "D04")
        first_reasons = result.trace["steps"][0]["observation_summary"]
        self.assertIn("D04", first_reasons)

    def test_context_follow_up_becomes_standalone_query(self) -> None:
        request = QueryRequest(
            "那试用期员工呢？",
            (ChatMessage("user", "远程办公制度适用于哪些员工？"),),
        )
        result = build_agent().run(request)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("远程办公", result.trace["standalone_query"])
        self.assertIn("试用期员工", result.trace["standalone_query"])
        self.assertEqual(result.citations[0].document_id, "D03")

    def test_completion_guard_blocks_unread_conflict_candidate(self) -> None:
        model = PrematureFinishModel()
        result = build_agent(model=model).run(QueryRequest("报销期限多久？"))

        self.assertTrue(model.attempted)
        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        actions = [step["action"] for step in result.trace["steps"]]
        self.assertEqual(actions, ["search_docs", "read_doc", "finish", "read_doc", "ask_user"])
        self.assertIn("同规则候选", result.trace["steps"][2]["observation_summary"])

    def test_replacing_document_text_changes_answer_and_quote(self) -> None:
        original = build_agent().run(QueryRequest("设备保修多久？"))
        self.assertIn("12个月", original.answer)

        raw = json.loads(DOCUMENTS.read_text(encoding="utf-8"))
        for document in raw:
            if document["document_id"] == "D04":
                document["chunks"][0]["text"] = (
                    "公司采购的标准设备自签收之日起享有18个月质保；发生故障时应通过服务台提交维修申请。"
                )
        changed_base = KnowledgeBase.from_data(raw)
        changed = build_agent(knowledge_base=changed_base).run(QueryRequest("设备保修多久？"))

        self.assertEqual(changed.status, TaskStatus.COMPLETED)
        self.assertIn("18个月", changed.answer)
        self.assertNotIn("12个月", changed.answer)
        self.assertIn("18个月", changed.citations[0].quote)

    def test_tool_call_budget_returns_explicit_incomplete_status(self) -> None:
        config = replace(AgentConfig(), max_tool_calls=1)
        result = build_agent(config=config).run(QueryRequest("忘记密码怎么重置？"))

        self.assertEqual(result.status, TaskStatus.MAX_STEPS_EXCEEDED)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.claims, [])

    def test_live_model_requires_all_connection_settings(self) -> None:
        with self.assertRaises(ModelError):
            OpenAICompatibleModel(AgentConfig())


if __name__ == "__main__":
    unittest.main()
