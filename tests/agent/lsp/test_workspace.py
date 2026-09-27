"""Tests for workspace + project-root resolution."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.lsp.workspace import (
    clear_cache,
    find_git_worktree,
    is_inside_workspace,
    nearest_root,
    normalize_path,
    resolve_workspace_for_file,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()




def test_find_git_worktree_finds_dotgit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_git_worktree(str(sub)) == str(repo)








def test_nearest_root_finds_first_marker(tmp_path: Path):
    root = tmp_path / "p"
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    found = nearest_root(str(deep / "mod.py"), ["pyproject.toml"])
    assert found == str(root)






def test_resolve_workspace_for_file_uses_cwd_first(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    file_path = repo / "x.py"
    file_path.write_text("")
    # cwd is inside the repo
    monkeypatch.chdir(str(repo))
    root, gated = resolve_workspace_for_file(str(file_path))
    assert root == str(repo)
    assert gated is True






def test_normalize_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    p = normalize_path("~/x.py")
    assert p == os.path.abspath("/home/user/x.py")


# ---------------------------------------------------------------------------
# Dangling process cwd (deleted scratch / kanban workspace, GC mid-session)
# ---------------------------------------------------------------------------


def _raise_missing_cwd(*_args, **_kwargs):
    raise FileNotFoundError(2, "No such file or directory")


def test_resolve_workspace_for_file_survives_deleted_cwd(tmp_path: Path, monkeypatch):
    """A deleted process cwd must degrade to the file-anchored walk.

    ``os.getcwd()`` raises ``FileNotFoundError`` once the directory the
    process was launched in is removed — which is what a long-lived agent
    session hits after its scratch/kanban workspace is garbage-collected.
    The file itself is still a valid anchor, so resolution must not raise.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    file_path = repo / "x.py"
    file_path.write_text("")

    monkeypatch.setattr(os, "getcwd", _raise_missing_cwd)

    root, gated = resolve_workspace_for_file(str(file_path))
    assert root == str(repo)
    assert gated is True


def test_resolve_workspace_for_file_deleted_cwd_no_worktree(tmp_path: Path, monkeypatch):
    """Same degraded state, file outside any worktree → (None, False), not a raise."""
    outside = tmp_path / "plain"
    outside.mkdir()
    file_path = outside / "x.py"
    file_path.write_text("")

    monkeypatch.setattr(os, "getcwd", _raise_missing_cwd)

    assert resolve_workspace_for_file(str(file_path)) == (None, False)


def test_resolve_workspace_for_file_explicit_cwd_still_wins(tmp_path: Path, monkeypatch):
    """An explicit ``cwd`` argument is unaffected by a broken process cwd."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    file_path = repo / "x.py"
    file_path.write_text("")

    monkeypatch.setattr(os, "getcwd", _raise_missing_cwd)

    root, gated = resolve_workspace_for_file(str(file_path), cwd=str(repo))
    assert root == str(repo)
    assert gated is True
