from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ultron_worker.desktop import update
from ultron_worker.desktop.update import UpdateError, compare, parse_left_right


@pytest.mark.parametrize(
    ("ahead", "behind", "state"),
    [(0, 0, "equal"), (0, 3, "behind"), (2, 0, "ahead"), (1, 4, "diverged")],
)
def test_compare(ahead: int, behind: int, state: str) -> None:
    assert compare(ahead, behind) == state


def test_compare_rejects_negative() -> None:
    with pytest.raises(ValueError):
        compare(-1, 0)


def test_parse_left_right() -> None:
    assert parse_left_right("2\t5\n") == (2, 5)
    assert parse_left_right("0 0") == (0, 0)
    for bad in ("", "3", "a\tb", "1 2 3"):
        with pytest.raises(UpdateError):
            parse_left_right(bad)


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, name: str, subject: str) -> str:
    (repo / name).write_text(subject)
    sh(repo, "add", name)
    sh(repo, "commit", "-q", "-m", subject)
    return sh(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo (David's GitHub) and a clone (the checkout on Ultron)."""
    up = tmp_path / "upstream"
    up.mkdir()
    sh(up, "init", "-q", "-b", "main")
    commit(up, "a.txt", "first")
    clone = tmp_path / "clone"
    sh(tmp_path, "clone", "-q", str(up), str(clone))
    return up, clone


def test_check_equal(repos) -> None:
    _, clone = repos
    st = update.check(clone, sh(clone, "rev-parse", "HEAD"))
    assert st.state == "equal" and not st.can_update
    assert st.current == st.available and st.current.subject == "first"


def test_check_behind_shows_both_commits(repos) -> None:
    up, clone = repos
    installed = sh(clone, "rev-parse", "HEAD")
    commit(up, "b.txt", "second")
    new = commit(up, "c.txt", "third: trivial change")
    st = update.check(clone, installed)
    assert st.state == "behind" and st.behind == 2 and st.ahead == 0 and st.can_update
    assert st.current.short == installed[:7] and st.current.subject == "first"
    assert st.available.sha == new and st.available.subject == "third: trivial change"
    j = st.to_json()
    assert j["available"]["short"] == new[:7] and j["can_update"] is True


def test_check_ahead(repos) -> None:
    _, clone = repos
    local = commit(clone, "x.txt", "local only")
    st = update.check(clone, local)
    assert st.state == "ahead" and st.ahead == 1 and not st.can_update


def test_check_unknown_installed_sha(repos) -> None:
    _, clone = repos
    assert update.check(clone, None).state == "unknown"
    st = update.check(clone, "f" * 40)
    assert st.state == "unknown" and st.can_update and "not in repo" in (st.error or "")


def test_check_errors_never_raise(tmp_path) -> None:
    assert update.check(None, "abc").state == "error"
    st = update.check(tmp_path, "abc")
    assert st.state == "error" and st.error


def test_pull_fast_forwards(repos) -> None:
    up, clone = repos
    new = commit(up, "b.txt", "second")
    update.git(clone, "fetch", "-q", "origin", "main")
    update.pull(clone, new)
    assert sh(clone, "rev-parse", "HEAD") == new


def test_pull_refuses_local_changes(repos) -> None:
    up, clone = repos
    new = commit(up, "b.txt", "second")
    update.git(clone, "fetch", "-q", "origin", "main")
    (clone / "a.txt").write_text("edited on Ultron")
    with pytest.raises(UpdateError, match="local changes"):
        update.pull(clone, new)


def test_pull_refuses_other_branch(repos) -> None:
    up, clone = repos
    new = commit(up, "b.txt", "second")
    update.git(clone, "fetch", "-q", "origin", "main")
    sh(clone, "checkout", "-q", "-b", "experiment")
    with pytest.raises(UpdateError, match="experiment"):
        update.pull(clone, new)
