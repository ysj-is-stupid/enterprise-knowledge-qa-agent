from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from enterprise_qa_agent import __version__
from enterprise_qa_agent.cli import main
from enterprise_qa_agent.config import AgentConfig
from enterprise_qa_agent.models import ActionType, TaskStatus


class SkeletonSmokeTests(unittest.TestCase):
    def test_package_version(self) -> None:
        self.assertEqual(__version__, "0.1.0")

    def test_config_defaults_and_secret_redaction(self) -> None:
        config = AgentConfig(api_key="do-not-print")
        summary = config.public_summary()
        self.assertEqual(summary["max_decision_steps"], 10)
        self.assertEqual(summary["max_tool_calls"], 8)
        self.assertTrue(summary["api_key_configured"])
        self.assertNotIn("do-not-print", repr(summary))

    def test_deepseek_environment_aliases_are_supported(self) -> None:
        environment = {
            "KNOWLEDGE_QA_API_KEY": "",
            "KNOWLEDGE_QA_BASE_URL": "",
            "KNOWLEDGE_QA_MODEL": "",
            "DEEPSEEK_API_KEY": "test-only-key",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.example/v1",
            "DEEPSEEK_MODEL": "deepseek-test",
        }
        with patch.dict("os.environ", environment, clear=False):
            config = AgentConfig.from_environment(dotenv_path="missing-test-env")

        self.assertEqual(config.api_key, "test-only-key")
        self.assertEqual(config.base_url, "https://api.deepseek.example/v1")
        self.assertEqual(config.model_name, "deepseek-test")
        self.assertNotIn("test-only-key", repr(config.public_summary()))

    def test_enums_match_design_contract(self) -> None:
        self.assertEqual(ActionType.SEARCH_DOCS, "search_docs")
        self.assertEqual(TaskStatus.NEEDS_CLARIFICATION, "needs_clarification")

    def test_health_command_returns_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["health", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "core_ready")
        self.assertEqual(payload["version"], "0.1.0")

    def test_ask_command_runs_core_chain(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["ask", "--question", "忘记密码怎么重置？", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["citations"][0]["document_id"], "D05")
        self.assertEqual(
            [step["action"] for step in payload["trace"]["steps"]],
            ["search_docs", "read_doc", "finish"],
        )


if __name__ == "__main__":
    unittest.main()
