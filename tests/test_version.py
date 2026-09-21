"""HS-1 + HS-4: the version surface and the  fingerprint on /api/health.

The W39 sha-proxy tests that used to live here asserted that moving HEAD under a
running process reads stale. That was the retired behaviour (it fired on
docs-only commits), so those tests were replaced, not kept green by accident.

The staleness cases run THROUGH THE ROUTE against a temporary git checkout,
because /api/health is what the host monitor's dial reads and what the 2026-09-13
specimen was observed on. Nothing touches this repo's own .git or data/.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brief import version
from brief.window.app import create_app

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout.strip()


def _checkout(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _spawn_from(monkeypatch, repo: Path, roots: list[Path] | None = None) -> None:
    """Point brief.version's spawn readings at `repo`, as if the process had
    started there: runtime roots `brief/` (+ any planted editable root) and the
    speaker entry file `scripts/dispatch_speak.py`."""
    runtime_roots = [repo / "brief", *(roots or [])]
    runtime_files = [repo / "scripts" / "dispatch_speak.py"]
    monkeypatch.setattr(version, "_REPO", repo)
    monkeypatch.setattr(version, "_GIT", repo / ".git")
    monkeypatch.setattr(version, "_VERSION_FILE", repo / "VERSION")
    monkeypatch.setattr(version, "GIT_SHA", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(version, "VERSION", version.read_version(repo / "VERSION"))
    monkeypatch.setattr(version, "EDITABLE_ROOTS", list(roots or []))
    monkeypatch.setattr(version, "RUNTIME_ROOTS", runtime_roots)
    monkeypatch.setattr(version, "RUNTIME_FILES", runtime_files)
    monkeypatch.setattr(
        version, "STARTED_FP", version.fingerprint(runtime_roots, runtime_files)
    )
    monkeypatch.setattr(version, "_LIVE_TTL", 0.0)
    monkeypatch.setattr(version, "_live_cache", None)


def _health(client: TestClient) -> dict:
    # No lifespan (no `with`): the loops are not started, so ok may be False and
    # the status 503. The source fields are present either way.
    return client.get("/api/health").json()


@pytest.fixture
def client():
    return TestClient(create_app(world_feeds=[], window_cfg={}, news_sources=[]))


@pytest.fixture
def spawned(tmp_path, monkeypatch, client):
    repo = _checkout(
        tmp_path / "repo",
        {
            "brief/a.py": "x = 1\n",
            "scripts/dispatch_speak.py": "speak = 1\n",
            "scripts/tool.py": "tool = 1\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "README.md": "one\n",
            "VERSION": "1.0.0\n",
        },
    )
    _spawn_from(monkeypatch, repo)
    body = _health(client)
    assert body["source_stale"] is False
    assert body["head_sha"] == body["git_sha"]
    return repo


# --- staleness: the two cases the sha proxy got wrong ----------------------------


def test_health_source_stale_true_after_an_uncommitted_runtime_edit(spawned, client):
    """Code the process did not load. HEAD is unchanged, so the sha proxy read
    fresh here; the fingerprint must not."""
    (spawned / "brief" / "a.py").write_text("x = 2\n")
    body = _health(client)
    assert body["source_stale"] is True
    assert body["head_sha"] == body["git_sha"]  # the sha could not have seen it


def test_health_source_stale_false_after_a_docs_only_commit(spawned, client):
    """DISPATCH'S WILD SPECIMEN (2026-09-13): the engine ran 36fc552, HEAD moved to
    472197a with zero runtime files changed, and /api/health said
    source_stale: true. On 2026-09-08 the engine was restarted twice for that."""
    (spawned / "README.md").write_text("two\n")
    _git(spawned, "commit", "-q", "-am", "docs only")
    body = _health(client)
    assert body["head_sha"] != body["git_sha"]  # HEAD moved...
    assert body["source_stale"] is False  # ...the code did not


# ---  (HS-1c): only runtime code moves the light ------------------------------


@pytest.mark.parametrize("path", ["tests/test_a.py", "scripts/tool.py"])
def test_health_source_stale_false_after_a_tests_or_tooling_only_commit(
    spawned, client, path
):
    """DISPATCH'S  SPECIMEN (2026-09-14): HS-2's commit 81bb7ca touched only
    tests/ and scripts/livedata_check.py, and both live processes read
    source_stale: true for 45 minutes on unchanged runtime code."""
    (spawned / path).write_text("changed = 2\n")
    _git(spawned, "commit", "-q", "-am", f"only {path}")
    body = _health(client)
    assert body["head_sha"] != body["git_sha"]
    assert body["source_stale"] is False


def test_health_source_stale_true_when_the_speaker_entry_file_changes(spawned, client):
    """The speaker RUNS from scripts/dispatch_speak.py: it is runtime even though the
    rest of scripts/ is tooling."""
    (spawned / "scripts" / "dispatch_speak.py").write_text("speak = 2\n")
    assert _health(client)["source_stale"] is True


def test_health_publishes_exactly_the_roots_it_hashed(spawned, client):
    body = _health(client)
    assert body["runtime_roots"] == [
        str(spawned / "brief"),
        str(spawned / "scripts" / "dispatch_speak.py"),
    ]


def test_fingerprint_is_none_if_the_entry_file_is_unreadable(spawned):
    assert version.fingerprint([spawned / "brief"], [spawned / "absent.py"]) is None


def test_health_source_stale_true_after_an_editable_dependency_moves(
    tmp_path, monkeypatch, client
):
    """ (morse #1253): a change in an editable dependency, with the repo itself
    untouched, must flip the light. Dispatch has no such dependency today; this
    plants one."""
    repo = _checkout(
        tmp_path / "repo",
        {"brief/a.py": "x = 1\n", "scripts/dispatch_speak.py": "speak = 1\n"},
    )
    dep = _checkout(tmp_path / "dep", {"src/lib/__init__.py": "y = 1\n"})
    _spawn_from(monkeypatch, repo, roots=[dep / "src"])
    assert _health(client)["source_stale"] is False
    (dep / "src" / "lib" / "__init__.py").write_text("y = 2\n")
    _git(dep, "commit", "-q", "-am", "dep moved")
    body = _health(client)
    assert body["source_stale"] is True
    assert body["editable_roots"] == [str(dep / "src")]
    assert str(dep / "src") in body["runtime_roots"]


def test_health_says_unverifiable_never_false_when_it_cannot_tell(monkeypatch, client):
    monkeypatch.setattr(version, "STARTED_FP", None)
    body = _health(client)
    assert body["source_hash"] == "unverifiable"
    assert body["source_stale"] is None


def test_stale_is_none_unless_both_readings_exist():
    assert version.stale(None, None) is None
    assert version.stale("a", None) is None
    assert version.stale(None, "a") is None
    assert version.stale("a", "a") is False
    assert version.stale("a", "b") is True


def test_live_reading_is_cached_for_the_ttl(spawned, monkeypatch):
    monkeypatch.setattr(version, "_LIVE_TTL", 3600.0)
    first = version.live_fingerprint()
    (spawned / "brief" / "a.py").write_text("x = 3\n")
    assert version.live_fingerprint() == first  # inside the TTL: cached
    monkeypatch.setattr(version, "_LIVE_TTL", 0.0)
    assert version.live_fingerprint() != first  # TTL lapsed: re-read


# --- process identity: pid | started_at | git_sha (fable #1394) ------------------


def test_health_publishes_the_process_identity_triple(spawned, client):
    from datetime import datetime

    body = _health(client)
    assert body["pid"] == os.getpid()
    assert body["pid"] is not None
    assert datetime.fromisoformat(body["started_at"]).tzinfo is not None
    assert body["git_sha"] is not None


# --- HS-4: one version surface ----------------------------------------------------


def test_health_carries_the_version_from_the_version_file(spawned, client):
    body = _health(client)
    assert body["version"] == "1.0.0" == version.read_version(spawned / "VERSION")


def test_version_does_not_move_on_a_docs_only_commit(spawned, client):
    before = _health(client)["version"]
    (spawned / "README.md").write_text("docs\n")
    _git(spawned, "commit", "-q", "-am", "docs only")
    assert _health(client)["version"] == before


def test_the_repo_version_file_is_valid_semver():
    """The real VERSION at the repo root: one surface, MAJOR.MINOR.PATCH."""
    assert version.read_version() is not None


def test_read_version_rejects_missing_and_malformed(tmp_path):
    assert version.read_version(tmp_path / "absent") is None
    (tmp_path / "V").write_text("1.0\n")
    assert version.read_version(tmp_path / "V") is None
    (tmp_path / "V").write_text("v1.0.0\n")
    assert version.read_version(tmp_path / "V") is None
    (tmp_path / "V").write_text(" 2.3.4 \n")
    assert version.read_version(tmp_path / "V") == "2.3.4"


# --- HEAD reading: still used, as provenance --------------------------------------


def _make_git(tmp_path, *, head_ref="refs/heads/master", sha="a" * 40, packed=None):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(f"ref: {head_ref}\n")
    if packed is None:
        ref_path = git_dir / head_ref
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(sha + "\n")
    else:
        (git_dir / "packed-refs").write_text(f"{sha} {head_ref}\n")
    return git_dir


def test_reads_head_via_a_loose_ref(tmp_path, monkeypatch):
    _make_git(tmp_path, sha="b" * 40)
    monkeypatch.setattr(version, "_GIT", tmp_path / ".git")
    assert version._read_head() == "b" * 40


def test_reads_head_via_packed_refs_after_a_gc(tmp_path, monkeypatch):
    _make_git(tmp_path, sha="c" * 40, packed={})
    monkeypatch.setattr(version, "_GIT", tmp_path / ".git")
    assert version._read_head() == "c" * 40


def test_detached_head_is_the_sha_itself(tmp_path, monkeypatch):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("d" * 40 + "\n")
    monkeypatch.setattr(version, "_GIT", git_dir)
    assert version._read_head() == "d" * 40


def test_missing_git_dir_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_GIT", tmp_path / "not-a-git-dir")
    assert version._read_head() is None
