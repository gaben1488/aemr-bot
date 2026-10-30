"""Exercise the commit gate against real Git index blobs, without touching this repo."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[2] / "scripts/check_staged_size.py"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "commit", "--allow-empty", "-qm", "initial")
    return tmp_path


def stage(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    git(repo, "add", "--", name)


def check(repo, *args):
    return subprocess.run(
        [sys.executable, str(CHECKER), *args], cwd=repo, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "name", ["source.py", "a file.md", "line\nbreak.py", "docs/manual.md", "tests/case.py"]
)
def test_rejects_oversized_staged_text(repo, name):
    stage(repo, name, b"x" * 20001)
    result = check(repo)
    assert result.returncode == 1, result.stderr
    assert "20001" in result.stdout
    assert repr(name) in result.stdout
    assert "Split by responsibility" in result.stdout
    assert "imports" in result.stdout and "tests" in result.stdout
    assert "Stage the extraction" in result.stdout
    assert "explicit exception" in result.stdout


def test_inclusive_byte_threshold_and_utf8(repo):
    stage(repo, "fine.md", "я".encode() * 10000)
    assert check(repo).returncode == 0
    stage(repo, "fine.md", "я".encode() * 10001)
    result = check(repo)
    assert result.returncode == 1
    assert "20002" in result.stdout


def test_checks_staged_blob_despite_unstaged_shrink(repo):
    stage(repo, "partial.py", b"x" * 20001)
    (repo / "partial.py").write_bytes(b"small")
    assert check(repo).returncode == 1


def test_does_not_check_unstaged_growth_or_untracked_files(repo):
    stage(repo, "partial.py", b"small")
    (repo / "partial.py").write_bytes(b"x" * 20001)
    (repo / "untracked.py").write_bytes(b"x" * 20001)
    assert check(repo).returncode == 0


def test_rename_checks_destination_and_deletions_are_ignored(repo):
    stage(repo, "old.py", b"x" * 20001)
    git(repo, "commit", "-qm", "legacy")
    git(repo, "mv", "old.py", "new name.py")
    result = check(repo)
    assert result.returncode == 1
    assert "new name.py" in result.stdout
    git(repo, "rm", "-f", "new name.py")
    assert check(repo).returncode == 0


@pytest.mark.parametrize(
    "name,content",
    [
        ("aemr-bot-index.md", b"x" * 20001),
        ("docs/site/index.html", b"x" * 20001),
        ("bot/uv.lock", b"x" * 20001),
        ("docs/audits/IMPLEMENTATION_INVENTORY_2026-10.md", b"x" * 20001),
        ("docs/manual.pdf", b"x" * 20001),
        ("assets/data.bin", b"\0" + b"x" * 20001),
    ],
    ids=["index", "wiki", "lock", "generated-inventory", "pdf", "nul-binary"],
)
def test_explicit_artifact_exemptions(repo, name, content):
    stage(repo, name, content)
    assert check(repo).returncode == 0
    stage(repo, "actual-source.py", b"x" * 20001)
    assert check(repo).returncode == 1


def test_similarly_named_files_are_not_exempt(repo):
    stage(repo, "other/uv.lock", b"x" * 20001)
    assert check(repo).returncode == 1


def baseline(repo, limits):
    stage(repo, "size-baseline.json", json.dumps(limits).encode())


def test_baseline_is_opt_in_and_read_from_index(repo):
    stage(repo, "legacy.py", b"x" * 25000)
    git(repo, "commit", "-qm", "legacy")
    baseline(repo, {"legacy.py": 25000})
    stage(repo, "legacy.py", b"y" * 24000)
    assert check(repo).returncode == 1
    (repo / "size-baseline.json").write_text("{}")
    assert check(repo, "--baseline", "size-baseline.json").returncode == 0


def test_baseline_cannot_hide_growth_or_new_files(repo):
    stage(repo, "legacy.py", b"x" * 25000)
    git(repo, "commit", "-qm", "legacy")
    baseline(repo, {"legacy.py": 25000, "new.py": 30000})
    stage(repo, "legacy.py", b"y" * 25001)
    assert check(repo, "--baseline", "size-baseline.json").returncode == 1
    git(repo, "restore", "--staged", "legacy.py")
    stage(repo, "new.py", b"x" * 25000)
    assert check(repo, "--baseline", "size-baseline.json").returncode == 1


def test_renamed_legacy_needs_its_own_review(repo):
    stage(repo, "old.py", b"x" * 25000)
    git(repo, "commit", "-qm", "legacy")
    baseline(repo, {"old.py": 25000})
    git(repo, "mv", "old.py", "new.py")
    assert check(repo, "--baseline", "size-baseline.json").returncode == 1


@pytest.mark.parametrize("content", [b"invalid", b"[]", b'{"file.py": true}', b'{"file.py": -1}'])
def test_invalid_baseline_fails_closed(repo, content):
    stage(repo, "size-baseline.json", content)
    result = check(repo, "--baseline", "size-baseline.json")
    assert result.returncode == 2
    assert "baseline" in result.stderr.lower()


def test_missing_index_baseline_fails_even_if_worktree_copy_exists(repo):
    (repo / "size-baseline.json").write_text("{}")
    assert check(repo, "--baseline", "size-baseline.json").returncode == 2


def test_ci_base_ref_reads_head_not_index_or_worktree(repo):
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    stage(repo, "committed.py", b"x" * 20001)
    git(repo, "commit", "-qm", "oversized")
    stage(repo, "committed.py", b"small")
    result = check(repo, "--base-ref", base)
    assert result.returncode == 1
    assert "20001" in result.stdout
    assert "committed.py" in result.stdout


def test_ci_baseline_reads_head_not_index(repo):
    stage(repo, "legacy.py", b"x" * 25000)
    baseline(repo, {"legacy.py": 25000})
    git(repo, "commit", "-qm", "legacy")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    stage(repo, "legacy.py", b"y" * 24000)
    git(repo, "commit", "-qm", "maintenance")
    baseline(repo, {})
    result = check(repo, "--base-ref", base, "--baseline", "size-baseline.json")
    assert result.returncode == 0, result.stderr
    assert check(repo, "--base-ref", base).returncode == 1


def test_no_baseline_inflation_allows_legacy_growth(repo):
    stage(repo, "legacy.py", b"x" * 25000)
    git(repo, "commit", "-qm", "legacy")
    baseline(repo, {"legacy.py": 100000})
    stage(repo, "legacy.py", b"y" * 26000)
    assert check(repo, "--baseline", "size-baseline.json").returncode == 1


def test_unborn_repository_checks_new_files(tmp_path):
    git(tmp_path, "init", "-q")
    stage(tmp_path, "new.py", b"x" * 20001)
    assert check(tmp_path).returncode == 1


def test_ci_cannot_inflate_baseline_to_hide_committed_growth(repo):
    stage(repo, "legacy.py", b"x" * 25000)
    git(repo, "commit", "-qm", "legacy")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    baseline(repo, {"legacy.py": 100000})
    stage(repo, "legacy.py", b"y" * 26000)
    git(repo, "commit", "-qm", "growth")
    result = check(repo, "--base-ref", base, "--baseline", "size-baseline.json")
    assert result.returncode == 1
    assert "25000 allowed bytes" in result.stdout


def test_baseline_cannot_reactivate_after_file_fits(repo):
    stage(repo, "former.py", b"small")
    git(repo, "commit", "-qm", "reduced")
    baseline(repo, {"former.py": 25000})
    stage(repo, "former.py", b"x" * 21000)
    assert check(repo, "--baseline", "size-baseline.json").returncode == 1


def test_default_mode_works_from_repository_subdirectory(repo):
    stage(repo, "source.py", b"x" * 20001)
    nested = repo / "nested"
    nested.mkdir()
    result = check(nested)
    assert result.returncode == 1
    assert "source.py" in result.stdout
