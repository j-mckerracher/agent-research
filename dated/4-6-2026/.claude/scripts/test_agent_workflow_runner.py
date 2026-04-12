#!/usr/bin/env python3
"""Unit tests for `.claude/scripts/agent_workflow_runner.py`."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ASSETS_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(__file__).resolve().with_name("agent_workflow_runner.py")

spec = importlib.util.spec_from_file_location("agent_workflow_runner", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class DiscoverAgentsTests(unittest.TestCase):
    """Verify agent discovery and alias resolution."""

    def test_discover_agents_indexes_numbered_and_named_agents(self) -> None:
        agents = module.discover_agents(WORKFLOW_ASSETS_ROOT)
        numbered = agents["01-intake"]
        named = agents["intake-agent"]
        self.assertEqual(numbered.path, named.path)
        self.assertEqual(numbered.name, "intake-agent")


class EvaluationHelpersTests(unittest.TestCase):
    """Verify evaluation parsing helpers."""

    def test_read_evaluation_result_reports_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "eval.json"
            path.write_text(json.dumps({"overall_result": "pass", "issues": []}), encoding="utf-8")
            passed, payload = module.read_evaluation_result(path)
            self.assertTrue(passed)
            self.assertEqual(payload["overall_result"], "pass")


class DryRunWorkflowTests(unittest.TestCase):
    """Verify the full dry-run control flow."""

    def test_main_dry_run_completes_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as artifact_root:
            exit_code = module.main(
                [
                    "--repo-root",
                    str(REPO_ROOT),
                    "--artifact-root",
                    artifact_root,
                    "--change-id",
                    "WI-DRY-RUN",
                    "--context",
                    "Dry-run workflow context for unit testing.",
                    "--dry-run",
                    "--json",
                ]
            )
            self.assertEqual(exit_code, 0)
            base = Path(artifact_root) / "WI-DRY-RUN"
            self.assertTrue((base / "intake" / "story.yaml").is_file())
            self.assertTrue((base / "planning" / "assignments.json").is_file())
            self.assertTrue((base / "execution" / "UOW-001" / "impl_report.yaml").is_file())
            self.assertTrue((base / "qa" / "qa_report.yaml").is_file())
            self.assertTrue((base / "summary" / "lessons_optimizer_report.yaml").is_file())
            self.assertTrue((base / "logs" / "workflow_runner").is_dir())


if __name__ == "__main__":
    unittest.main()
