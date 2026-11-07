from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "examples" / "outputs"
TRACES = ROOT / "examples" / "traces"


class GeneratedExampleTests(unittest.TestCase):
    EXPECTED = {
        "acl-allowed": ("completed", ["search_docs", "read_doc", "finish"]),
        "acl-denied": ("insufficient_evidence", ["search_docs", "search_docs", "finish"]),
        "clear-answer": ("completed", ["search_docs", "read_doc", "finish"]),
        "conflict": ("needs_clarification", ["search_docs", "read_doc", "read_doc", "ask_user"]),
        "context-follow-up": ("completed", ["search_docs", "read_doc", "finish"]),
        "out-of-scope": ("insufficient_evidence", ["search_docs", "search_docs", "finish"]),
        "paraphrase": ("completed", ["search_docs", "read_doc", "finish"]),
        "tool-failure": ("tool_error", ["search_docs", "search_docs", "finish"]),
    }

    def test_generated_outputs_have_expected_status_and_actions(self) -> None:
        for case, (status, actions) in self.EXPECTED.items():
            with self.subTest(case=case):
                payload = json.loads((OUTPUTS / f"{case}.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], status)
                self.assertEqual([item["action"] for item in payload["trace"]["steps"]], actions)

    def test_separate_trace_files_match_embedded_traces(self) -> None:
        for case in self.EXPECTED:
            with self.subTest(case=case):
                payload = json.loads((OUTPUTS / f"{case}.json").read_text(encoding="utf-8"))
                trace = json.loads((TRACES / f"{case}-trace.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["trace"], trace)

    def test_every_example_citation_is_exact_source_text(self) -> None:
        documents = json.loads((ROOT / "data" / "documents.json").read_text(encoding="utf-8"))
        sources = {
            (document["document_id"], chunk["chunk_id"]): chunk["text"]
            for document in documents
            for chunk in document["chunks"]
        }
        for case in self.EXPECTED:
            payload = json.loads((OUTPUTS / f"{case}.json").read_text(encoding="utf-8"))
            citation_ids = {item["citation_id"] for item in payload["citations"]}
            for citation in payload["citations"]:
                with self.subTest(case=case, citation=citation["citation_id"]):
                    key = (citation["document_id"], citation["chunk_id"])
                    self.assertEqual(citation["quote"], sources[key])
            for claim in payload["claims"]:
                self.assertTrue(set(claim["citation_ids"]).issubset(citation_ids))


if __name__ == "__main__":
    unittest.main()
