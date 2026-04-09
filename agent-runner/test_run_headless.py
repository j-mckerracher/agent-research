#!/usr/bin/env python3
"""Unit tests for `agent-runner/run_headless.py`."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Load modules under test
# ---------------------------------------------------------------------------

_RUNNER_DIR = Path(__file__).resolve().parent

# Load run.py as "runner" so run_headless.py can import it
_run_spec = importlib.util.spec_from_file_location("run", _RUNNER_DIR / "run.py")
assert _run_spec and _run_spec.loader
_run_module = importlib.util.module_from_spec(_run_spec)
sys.modules["run"] = _run_module
_run_spec.loader.exec_module(_run_module)  # type: ignore[union-attr]

# Load run_headless.py
_hl_spec = importlib.util.spec_from_file_location(
    "run_headless", _RUNNER_DIR / "run_headless.py"
)
assert _hl_spec and _hl_spec.loader
hl = importlib.util.module_from_spec(_hl_spec)
sys.modules["run_headless"] = hl
_hl_spec.loader.exec_module(hl)  # type: ignore[union-attr]

WorktreeInfo = hl.WorktreeInfo
_make_worktree_name = hl._make_worktree_name
_resolve_base_ref = hl._resolve_base_ref
_cleanup_worktree = hl._cleanup_worktree
create_fresh_worktree = hl.create_fresh_worktree
WorkflowError = _run_module.WorkflowError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_REPO = Path("/fake/repo")


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Name generation
# ---------------------------------------------------------------------------


class TestMakeWorktreeName(unittest.TestCase):
    def test_format(self) -> None:
        name = _make_worktree_name("WI-4461550")
        # Should look like:  4461550-YYYYMMDD_HHMMSS-xxxxxx
        parts = name.split("-")
        self.assertGreaterEqual(len(parts), 3)
        # First part is the slug
        self.assertEqual(parts[0], "4461550")

    def test_uniqueness(self) -> None:
        name1 = _make_worktree_name("WI-100")
        name2 = _make_worktree_name("WI-100")
        # Random suffix must differ (astronomically unlikely to collide)
        self.assertNotEqual(name1, name2)

    def test_bare_number(self) -> None:
        name = _make_worktree_name("9999")
        self.assertTrue(name.startswith("9999-"))

    def test_non_ascii_stripped(self) -> None:
        name = _make_worktree_name("WI-123")
        self.assertRegex(name, r"^[a-z0-9\-_]+$")


# ---------------------------------------------------------------------------
# _resolve_base_ref
# ---------------------------------------------------------------------------


class TestResolveBaseRef(unittest.TestCase):
    def _patch_run_git(self, side_effect):
        return mock.patch.object(hl, "_run_git", side_effect=side_effect)

    def test_origin_head_found_immediately(self) -> None:
        calls = []

        def fake_run_git(repo_root, args, **kwargs):
            calls.append(args)
            return _cp(returncode=0, stdout="abc123")

        with self._patch_run_git(fake_run_git):
            ref = _resolve_base_ref(_FAKE_REPO)

        self.assertEqual(ref, "origin/HEAD")
        self.assertIn(["rev-parse", "--verify", "origin/HEAD"], calls)

    def test_set_head_called_when_origin_head_missing(self) -> None:
        """When origin/HEAD is absent, set-head is called and then origin/HEAD retried."""
        call_log = []

        def fake_run_git(repo_root, args, **kwargs):
            call_log.append(list(args))
            if args[0] == "rev-parse" and args[-1] == "origin/HEAD" and len(call_log) == 1:
                return _cp(returncode=128, stderr="not a valid object")
            if args == ["remote", "set-head", "origin", "-a"]:
                return _cp(returncode=0)
            # Second rev-parse for origin/HEAD succeeds
            return _cp(returncode=0, stdout="def456")

        with self._patch_run_git(fake_run_git):
            ref = _resolve_base_ref(_FAKE_REPO)

        self.assertEqual(ref, "origin/HEAD")
        self.assertIn(["remote", "set-head", "origin", "-a"], call_log)

    def test_fallback_to_origin_main(self) -> None:
        """Falls back to origin/main when origin/HEAD cannot be resolved."""
        call_log = []

        def fake_run_git(repo_root, args, **kwargs):
            call_log.append(list(args))
            if "origin/HEAD" in args:
                return _cp(returncode=128)
            if args == ["remote", "set-head", "origin", "-a"]:
                return _cp(returncode=0)
            if "origin/main" in args:
                return _cp(returncode=0, stdout="abc")
            return _cp(returncode=128)

        with self._patch_run_git(fake_run_git):
            ref = _resolve_base_ref(_FAKE_REPO)

        self.assertEqual(ref, "origin/main")

    def test_raises_when_all_refs_fail(self) -> None:
        def fake_run_git(repo_root, args, **kwargs):
            return _cp(returncode=128)

        with self._patch_run_git(fake_run_git):
            with self.assertRaises(WorkflowError):
                _resolve_base_ref(_FAKE_REPO)


# ---------------------------------------------------------------------------
# create_fresh_worktree — command sequence
# ---------------------------------------------------------------------------


class TestCreateFreshWorktree(unittest.TestCase):
    def test_correct_command_sequence(self) -> None:
        """Verifies git commands are issued in the expected order."""
        issued = []

        def fake_run_git(repo_root, args, **kwargs):
            issued.append(list(args))
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return _cp(returncode=0, stdout="true")
            if args[0] == "rev-parse" and "origin/HEAD" in args:
                return _cp(returncode=0, stdout="abc123")
            if args[0] == "worktree":
                return _cp(returncode=0)
            return _cp(returncode=0)

        with (
            mock.patch.object(hl, "_run_git", side_effect=fake_run_git),
            mock.patch.object(Path, "mkdir"),
            mock.patch.object(Path, "exists", return_value=False),
        ):
            info = create_fresh_worktree(_FAKE_REPO, "WI-999")

        # First call must verify it's inside a git work tree
        self.assertEqual(issued[0], ["rev-parse", "--is-inside-work-tree"])
        # worktree add must appear somewhere in issued commands
        worktree_add_calls = [c for c in issued if c[:2] == ["worktree", "add"]]
        self.assertEqual(len(worktree_add_calls), 1)
        wt_cmd = worktree_add_calls[0]
        self.assertIn("-b", wt_cmd)
        self.assertIn("origin/HEAD", wt_cmd)

        # Returned info must be consistent
        self.assertTrue(info.branch.startswith("worktree-"))
        self.assertEqual(info.base_ref, "origin/HEAD")
        self.assertIn(".claude/worktrees", str(info.path))

    def test_raises_when_not_git_repo(self) -> None:
        def fake_run_git(repo_root, args, **kwargs):
            return _cp(returncode=128, stdout="", stderr="not a git repo")

        with mock.patch.object(hl, "_run_git", side_effect=fake_run_git):
            with self.assertRaises(WorkflowError):
                create_fresh_worktree(_FAKE_REPO, "WI-1")


# ---------------------------------------------------------------------------
# _cleanup_worktree
# ---------------------------------------------------------------------------


class TestCleanupWorktree(unittest.TestCase):
    def _make_info(self) -> WorktreeInfo:
        return WorktreeInfo(
            path=Path("/fake/repo/.claude/worktrees/test-name"),
            name="test-name",
            branch="worktree-test-name",
            base_ref="origin/HEAD",
        )

    def test_calls_worktree_remove_and_branch_delete(self) -> None:
        issued = []

        def fake_run_git(repo_root, args, **kwargs):
            issued.append(list(args))
            return _cp(returncode=0)

        info = self._make_info()
        with mock.patch.object(hl, "_run_git", side_effect=fake_run_git):
            _cleanup_worktree(_FAKE_REPO, info)

        remove_calls = [c for c in issued if c[:2] == ["worktree", "remove"]]
        branch_delete_calls = [c for c in issued if c[:2] == ["branch", "-D"]]
        self.assertEqual(len(remove_calls), 1)
        self.assertEqual(len(branch_delete_calls), 1)
        self.assertIn(str(info.path), remove_calls[0])
        self.assertIn(info.branch, branch_delete_calls[0])

    def test_errors_are_logged_not_raised(self) -> None:
        """Cleanup must not propagate exceptions."""

        def fake_run_git(repo_root, args, **kwargs):
            raise WorkflowError("simulated failure")

        info = self._make_info()
        with mock.patch.object(hl, "_run_git", side_effect=fake_run_git):
            # Should not raise
            _cleanup_worktree(_FAKE_REPO, info)


if __name__ == "__main__":
    unittest.main()
