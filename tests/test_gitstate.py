"""Adaptador opcional de Git: ausência é resultado, comandos são fixos e somente leitura, o núcleo não exige Git."""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, TempDirCase
from core import gitstate
from core.gitstate import (
    ERROR,
    NOT_A_REPOSITORY,
    NOT_INSTALLED,
    TIMEOUT,
    GitState,
    GitStateProbe,
    GitUnavailable,
    capture_git_state,
    git_drift,
    subprocess_runner,
)

HEAD = "0123456789abcdef0123456789abcdef01234567"
HEAD2 = "fedcba9876543210fedcba9876543210fedcba98"


class FakeGit:
    """Executor falso: responde por subcomando e registra o que foi pedido."""

    def __init__(self, *, repo: bool = True, head: str | None = HEAD, branch: str | None = "main", status: bytes = b"", fail: str | None = None) -> None:
        self.repo, self.head, self.branch, self.status, self.fail = repo, head, branch, status, fail
        self.calls: list[list] = []

    def __call__(self, args: list, cwd: str) -> "tuple[int, bytes]":
        self.calls.append(list(args))
        if self.fail:
            raise GitUnavailable(self.fail)
        rest = [a for a in args if a != "--no-optional-locks"]
        if rest[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return (0, b"true\n") if self.repo else (128, b"")
        if rest[0] == "rev-parse":
            return (0, (self.head + "\n").encode()) if self.head else (1, b"")
        if rest[0] == "symbolic-ref":
            return (0, (self.branch + "\n").encode()) if self.branch else (1, b"")
        if rest[0] == "status":
            return 0, self.status
        raise AssertionError(f"comando inesperado: {args}")


class Capture(unittest.TestCase):
    def test_clean_repository(self) -> None:
        state = capture_git_state(".", runner=FakeGit())
        self.assertEqual((state.available, state.head, state.branch, state.dirty, state.tracked_changes, state.untracked_count), (True, HEAD, "main", False, (), 0))

    def test_dirty_tracked_files_and_untracked_files_are_told_apart(self) -> None:
        status = b" M src/a.py\0A  new.txt\0?? scratch.tmp\0?? other.tmp\0"
        state = capture_git_state(".", runner=FakeGit(status=status))
        self.assertTrue(state.dirty)
        self.assertEqual(state.tracked_changes, ("new.txt", "src/a.py"))
        self.assertEqual(state.untracked_count, 2)

    def test_untracked_files_alone_do_not_make_the_repository_dirty(self) -> None:
        state = capture_git_state(".", runner=FakeGit(status=b"?? only.tmp\0"))
        self.assertFalse(state.dirty)
        self.assertEqual(state.untracked_count, 1)

    def test_renames_consume_their_origin_entry(self) -> None:
        state = capture_git_state(".", runner=FakeGit(status=b"R  new-name.txt\0old-name.txt\0 M other.txt\0"))
        self.assertEqual(state.tracked_changes, ("new-name.txt", "other.txt"))

    def test_ignored_entries_are_not_changes(self) -> None:
        self.assertFalse(capture_git_state(".", runner=FakeGit(status=b"!! build/\0")).dirty)

    def test_paths_that_cannot_be_logical_are_only_counted(self) -> None:
        state = capture_git_state(".", runner=FakeGit(status=b" M ok.txt\0 M bad:name.txt\0 M trailing.\0"))
        self.assertEqual(state.tracked_changes, ("ok.txt",))
        self.assertEqual(state.unportable_changes, 2)
        self.assertTrue(state.dirty)

    def test_repository_without_commits(self) -> None:
        state = capture_git_state(".", runner=FakeGit(head=None, branch="main"))
        self.assertTrue(state.available)
        self.assertIsNone(state.head)

    def test_detached_head_has_no_branch(self) -> None:
        self.assertIsNone(capture_git_state(".", runner=FakeGit(branch=None)).branch)

    def test_scope_becomes_a_pathspec_after_a_separator(self) -> None:
        fake = FakeGit()
        capture_git_state(".", scope=["docs", "src/a.py"], runner=fake)
        status = next(call for call in fake.calls if "status" in call)
        self.assertEqual(status[status.index("--") + 1:], ["docs", "src/a.py"])

    def test_invalid_scope_degrades_to_an_error_result(self) -> None:
        state = capture_git_state(".", scope=["../outside"], runner=FakeGit())
        self.assertEqual((state.available, state.reason), (False, ERROR))


class Unavailability(unittest.TestCase):
    """Ausência de Git, de repositório ou de resposta NUNCA levanta."""

    def test_git_not_installed(self) -> None:
        state = capture_git_state(".", runner=FakeGit(fail=NOT_INSTALLED))
        self.assertEqual((state.available, state.reason), (False, NOT_INSTALLED))

    def test_not_a_repository(self) -> None:
        state = capture_git_state(".", runner=FakeGit(repo=False))
        self.assertEqual((state.available, state.reason), (False, NOT_A_REPOSITORY))

    def test_timeout(self) -> None:
        self.assertEqual(capture_git_state(".", runner=FakeGit(fail=TIMEOUT)).reason, TIMEOUT)

    def test_runner_raising_unexpected_os_errors_becomes_error(self) -> None:
        def broken(args: list, cwd: str):
            raise OSError("boom")

        self.assertEqual(capture_git_state(".", runner=broken).reason, ERROR)

    def test_status_command_failure_becomes_error(self) -> None:
        def runner(args: list, cwd: str):
            if "status" in args:
                return 1, b""
            return (0, b"true\n") if "--is-inside-work-tree" in args else (0, (HEAD + "\n").encode())

        self.assertEqual(capture_git_state(".", runner=runner).reason, ERROR)

    def test_unavailable_state_carries_no_repository_data(self) -> None:
        with self.assertRaises(ValueError):
            GitState(False, NOT_INSTALLED, head=HEAD)
        with self.assertRaises(ValueError):
            GitState(False, "WHATEVER")
        with self.assertRaises(ValueError):
            GitState(True, NOT_INSTALLED, dirty=False, untracked_count=0)
        with self.assertRaises(ValueError):
            GitState(True, None, head="zz", dirty=False, untracked_count=0)

    def test_real_runner_maps_missing_executable_to_not_installed(self) -> None:
        with mock.patch("core.gitstate.subprocess.run", side_effect=FileNotFoundError()):
            self.assertEqual(capture_git_state(".").reason, NOT_INSTALLED)

    def test_real_runner_maps_timeouts_and_os_errors(self) -> None:
        with mock.patch("core.gitstate.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertEqual(capture_git_state(".").reason, TIMEOUT)
        with mock.patch("core.gitstate.subprocess.run", side_effect=PermissionError()):
            self.assertEqual(capture_git_state(".").reason, ERROR)

    def test_a_missing_git_executable_does_not_break_a_real_call(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": os.fspath(Path(sys.executable).parent.parent / "no-such-dir")}):
            state = capture_git_state(Path.cwd())
        if shutil.which("git") is None:
            self.assertEqual(state.reason, NOT_INSTALLED)
        else:
            self.assertIn(state.reason, (NOT_INSTALLED, NOT_A_REPOSITORY, None))


class CommandSafety(unittest.TestCase):
    """Só leitura: lista de argumentos, sem shell, sem rede, sem escrita."""

    FORBIDDEN = {"push", "pull", "fetch", "clone", "remote", "commit", "add", "checkout", "reset", "clean", "rm", "merge", "rebase",
                 "stash", "tag", "config", "init", "apply", "restore", "switch", "gc", "prune", "update-index"}

    def test_every_command_is_read_only_and_avoids_optional_locks(self) -> None:
        fake = FakeGit(status=b" M a.txt\0")
        capture_git_state(".", scope=["a.txt"], runner=fake)
        self.assertGreaterEqual(len(fake.calls), 4)
        for call in fake.calls:
            with self.subTest(call):
                self.assertEqual(call[0], "--no-optional-locks")
                self.assertFalse(self.FORBIDDEN & set(call))

    def test_subprocess_is_invoked_without_a_shell_and_with_a_list_and_a_timeout(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b"true\n")
        with mock.patch("core.gitstate.subprocess.run", return_value=completed) as run:
            subprocess_runner(7)(["rev-parse", "--is-inside-work-tree"], "somewhere")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][0], "git")
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_the_module_has_no_network_or_hosting_logic(self) -> None:
        tree = ast.parse((REPO / "core" / "gitstate.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "dataclasses", "os", "re", "subprocess", "typing"})
        docstrings = {id(node.body[0].value) for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings}
        self.assertFalse(self.FORBIDDEN & literals, "comando de escrita ou de rede entre as constantes")
        for literal in literals:
            self.assertNotIn("github", literal.lower())
            self.assertFalse(literal.lower().startswith(("http", "ssh", "git@")), literal)


class Drift(unittest.TestCase):
    def state(self, **over) -> GitState:
        base = dict(available=True, reason=None, head=HEAD, branch="main", dirty=False, tracked_changes=(), untracked_count=0)
        base.update(over)
        return GitState(**base)

    def test_no_change(self) -> None:
        self.assertEqual(git_drift(self.state(), self.state()), ())

    def test_each_kind_of_change_has_a_stable_code(self) -> None:
        self.assertEqual(git_drift(self.state(), self.state(head=HEAD2)), ("HEAD_CHANGED",))
        self.assertEqual(git_drift(self.state(), self.state(branch="other")), ("BRANCH_CHANGED",))
        self.assertEqual(git_drift(self.state(), self.state(dirty=True, tracked_changes=("a",))), ("DIRTY_CHANGED", "TRACKED_CHANGES_CHANGED"))
        self.assertEqual(git_drift(self.state(dirty=True, tracked_changes=("a",)), self.state(dirty=True, tracked_changes=("b",))), ("TRACKED_CHANGES_CHANGED",))

    def test_availability_change(self) -> None:
        gone = GitState(False, NOT_INSTALLED)
        self.assertEqual(git_drift(self.state(), gone), ("AVAILABILITY_CHANGED",))
        self.assertEqual(git_drift(gone, gone), ())

    def test_round_trip_through_the_portable_dictionary(self) -> None:
        for state in (self.state(), self.state(head=None, branch=None), GitState(False, TIMEOUT), self.state(dirty=True, tracked_changes=("a", "b"), untracked_count=3)):
            self.assertEqual(GitState.from_dict(state.to_dict()), state)
        with self.assertRaises(ValueError):
            GitState.from_dict({})

    def test_probe_adapts_the_adapter_to_the_orchestrator_interface(self) -> None:
        probe = GitStateProbe(".", runner=FakeGit())
        before = probe.capture()
        self.assertEqual(probe.compare(before, before), [])
        other = GitStateProbe(".", runner=FakeGit(head=HEAD2)).capture()
        self.assertEqual(probe.compare(before, other), ["HEAD_CHANGED"])


@unittest.skipIf(shutil.which("git") is None, "git não está instalado neste ambiente; o teste real é opcional")
class RealGit(TempDirCase):
    """Repositório real e descartável. Nenhuma configuração global é tocada: identidade só por `-c`."""

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
                       cwd=self.repo, check=True, capture_output=True)

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("one")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "first")

    def test_clean_then_dirty_then_untracked(self) -> None:
        clean = capture_git_state(self.repo)
        self.assertTrue(clean.available)
        self.assertRegex(clean.head, r"^[0-9a-f]{40,64}$")
        self.assertFalse(clean.dirty)
        (self.repo / "a.txt").write_text("two")
        dirty = capture_git_state(self.repo)
        self.assertEqual((dirty.dirty, dirty.tracked_changes), (True, ("a.txt",)))
        (self.repo / "b.txt").write_text("new")
        self.assertEqual(capture_git_state(self.repo).untracked_count, 1)
        self.assertEqual(git_drift(clean, dirty), ("DIRTY_CHANGED", "TRACKED_CHANGES_CHANGED"))

    def test_a_new_commit_moves_head(self) -> None:
        before = capture_git_state(self.repo)
        (self.repo / "a.txt").write_text("three")
        self.git("commit", "-q", "-am", "second")
        self.assertIn("HEAD_CHANGED", git_drift(before, capture_git_state(self.repo)))

    def test_capturing_does_not_modify_the_repository(self) -> None:
        (self.repo / "a.txt").write_text("changed")
        index = self.repo / ".git" / "index"
        before = (index.stat().st_mtime_ns, index.read_bytes())
        capture_git_state(self.repo)
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), before)
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_a_plain_directory_is_not_a_repository(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        # O diretório temporário pode estar dentro de algum repositório; só vale afirmar quando não está.
        outside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=plain, capture_output=True).returncode != 0
        if not outside:
            self.skipTest("o diretório temporário está dentro de um repositório")
        self.assertEqual(capture_git_state(plain).reason, NOT_A_REPOSITORY)

    def test_scope_restricts_the_reported_changes(self) -> None:
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "d.txt").write_text("1")
        self.git("add", "docs/d.txt")
        self.git("commit", "-q", "-m", "docs")
        (self.repo / "a.txt").write_text("changed")
        (self.repo / "docs" / "d.txt").write_text("changed too")
        self.assertEqual(capture_git_state(self.repo, scope=["docs"]).tracked_changes, ("docs/d.txt",))


if __name__ == "__main__":
    unittest.main()
