"""Children get the user's environment; git is found even when the app's PATH lacks it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ultron_worker.desktop import layout, update, winenv

SEP = os.pathsep


def no_registry() -> tuple[str, str]:
    return "", ""


def test_merge_path_keeps_order_and_drops_repeats() -> None:
    a, b, c = (str(Path("/x") / n) for n in "abc")
    assert winenv.merge_path(f"{a}{SEP}{b}", [b + os.sep, "", c], c) == SEP.join([a, b, c])


def test_child_env_refreshes_path_and_drops_launcher_vars() -> None:
    base = {"PATH": "/old", "_PYI_ARCHIVE_FILE": "x", "_MEIPASS2": "y", "USERPROFILE": "/u"}
    env = winenv.child_env(base, prepend=["/venv/Scripts"], registry=lambda: ("/sys", "/git"))
    assert env["PATH"].split(SEP) == ["/venv/Scripts", "/old", "/sys", "/git"]
    assert "_PYI_ARCHIVE_FILE" not in env and "_MEIPASS2" not in env
    assert env["USERPROFILE"] == "/u" and env["PYTHONUNBUFFERED"] == "1"


def test_child_env_keeps_windows_style_path_key() -> None:
    env = winenv.child_env({"Path": "/old"}, registry=no_registry)
    assert env["Path"] == "/old" and "PATH" not in env


def fake_which(hits: dict[str, str]):
    def which(name: str, path: str | None = None) -> str | None:
        return next((hits[d] for d in (path or "").split(SEP) if d in hits), None)

    return which


def test_find_git_on_path() -> None:
    which = fake_which({"/bin": "/bin/git"})
    assert winenv.find_git({"PATH": "/bin"}, registry=no_registry, which=which) == "/bin/git"


def test_find_git_on_the_registry_path_when_the_app_path_is_stale() -> None:
    which = fake_which({"/pf/Git/cmd": "/pf/Git/cmd/git.exe"})
    found = winenv.find_git(
        {"PATH": "/windows"},
        registry=lambda: ("/windows", "/pf/Git/cmd"),
        which=which,
        candidates=[],
    )
    assert found == "/pf/Git/cmd/git.exe"


def test_find_git_in_the_standard_install_locations() -> None:
    env = {"PATH": "", "PROGRAMFILES": r"C:\PF", "LOCALAPPDATA": r"C:\Users\d\AppData\Local"}
    cands = winenv.git_candidates(env)
    assert Path(r"C:\PF") / "Git" / "cmd" / "git.exe" in cands
    per_user = Path(r"C:\Users\d\AppData\Local") / "Programs" / "Git" / "cmd" / "git.exe"
    assert per_user in cands
    found = winenv.find_git(
        env, registry=no_registry, which=fake_which({}), is_file=lambda p: p == per_user
    )
    assert found == str(per_user)


def test_find_git_nowhere() -> None:
    assert (
        winenv.find_git({"PATH": ""}, registry=no_registry, which=fake_which({}), candidates=[])
        is None
    )


def test_update_check_without_git_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(winenv, "git_exe", lambda: None)
    st = update.check(tmp_path, "a" * 40)
    assert st.state == "error" and "git not found" in (st.error or "")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_repo_head_without_git_reads_the_git_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    head = git(repo, "rev-parse", "HEAD")
    assert layout.repo_head(repo) == head  # with git
    monkeypatch.setattr(winenv, "git_exe", lambda: None)
    assert layout.repo_head(repo) == head  # loose ref
    git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    assert layout.repo_head(repo) == head  # packed-refs
    git(repo, "checkout", "-q", "--detach")
    assert layout.repo_head(repo) == head  # detached HEAD
    assert layout.repo_head(tmp_path / "nope") is None


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


@pytest.mark.parametrize(
    "where",
    [
        "local/Microsoft/WinGet/Links/git.exe",
        "local/Microsoft/WinGet/Packages/Git.Git_Microsoft.Winget.Source_8wekyb3d8bbwe/cmd/git.exe",
        "local/GitHubDesktop/app-3.4.9/resources/app/git/cmd/git.exe",
        "programdata/chocolatey/bin/git.exe",
        "home/scoop/apps/git/current/cmd/git.exe",
        "home/scoop/shims/git.exe",
    ],
)
def test_find_git_in_package_manager_and_bundled_locations(tmp_path: Path, where: str) -> None:
    exe = _touch(tmp_path / where)
    env = {
        "PATH": "",
        "PROGRAMFILES": str(tmp_path / "pf"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "USERPROFILE": str(tmp_path / "home"),
        "PROGRAMDATA": str(tmp_path / "programdata"),
    }
    assert exe in winenv.git_candidates(env)
    assert winenv.find_git(env, registry=no_registry, which=fake_which({})) == str(exe)


def test_find_git_prefers_git_for_windows_and_the_newest_github_desktop(tmp_path: Path) -> None:
    desktop = tmp_path / "local" / "GitHubDesktop"
    tail = Path("resources/app/git/cmd/git.exe")
    old, new = _touch(desktop / "app-3.4.9" / tail), _touch(desktop / "app-3.10.1" / tail)
    env = {"PATH": "", "LOCALAPPDATA": str(tmp_path / "local"), "PROGRAMFILES": str(tmp_path)}
    cands = winenv.git_candidates(env)
    assert cands.index(new) < cands.index(old)
    gfw = _touch(tmp_path / "Git" / "cmd" / "git.exe")
    assert winenv.find_git(env, registry=no_registry, which=fake_which({})) == str(gfw)


def test_git_candidates_without_a_profile_or_folders(tmp_path: Path) -> None:
    cands = winenv.git_candidates({"PATH": "", "LOCALAPPDATA": str(tmp_path / "missing")})
    assert all(isinstance(c, Path) for c in cands)
    assert "winget" in winenv.GIT_NOT_FOUND and "GitHub Desktop" in winenv.GIT_NOT_FOUND
