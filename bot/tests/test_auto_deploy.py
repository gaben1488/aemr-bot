"""Execute the real deploy flow against filesystem and command stubs only."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def deploy_harness(tmp_path):
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "infra").mkdir(parents=True)
    (repo / "infra" / ".env").write_text("SYNTHETIC_CONFIG=preserved\n")
    head, builds, resets = (tmp_path / name for name in ("head", "builds", "resets"))
    head.write_text("old\n")
    builds.write_text("")
    resets.write_text("")
    commands = tmp_path / "commands"
    commands.mkdir()
    stubs = {
        "git": '''#!/bin/sh
case "$1" in
 fetch) exit 0 ;;
 rev-parse)
  if [ "$2" = HEAD ]; then cat "$AEMR_TEST_HEAD"; else printf 'new\\n'; fi ;;
 status) exit 0 ;;
 reset)
  printf '%s\\n' "$3" > "$AEMR_TEST_HEAD"
  printf '%s\\n' "$3" >> "$AEMR_TEST_RESETS"
  if [ "${AEMR_TEST_REAL_INSTALL:-0}" = 1 ]; then
    cp "$AEMR_TEST_VERSIONS/$3.sh" "$AEMR_TEST_REPO/scripts/auto-deploy.sh"
  fi ;;
 verify-commit) exit "${AEMR_TEST_SIGNATURE_RC:-0}" ;;
 *) exit 99 ;;
esac
''',
        "su": '''#!/bin/sh
printf 'attempt\\n' >> "$AEMR_TEST_BUILDS"
exit "${AEMR_TEST_BUILD_RC:-0}"
''',
        "curl": '''#!/bin/sh
case "$*" in
 */livez*) exit "${AEMR_TEST_LIVE_RC:-0}" ;;
 */readyz*) exit "${AEMR_TEST_READY_RC:-0}" ;;
 *) exit 99 ;;
esac
''',
        "logger": "#!/bin/sh\nif [ $# -eq 2 ]; then cat >/dev/null; fi\n",
        "install": '#!/bin/sh\nif [ "${AEMR_TEST_REAL_INSTALL:-0}" = 1 ]; then cp "$3" "$4.tmp"; mv "$4.tmp" "$4"; fi\n',
        "chown": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        command = commands / name
        command.write_text(body)
        command.chmod(0o700)
    content = (root / "scripts/auto-deploy.sh").read_text()
    content = content.replace("REPO_DIR=/home/aemr/aemr-bot", f"REPO_DIR={repo}")
    content = content.replace("/tmp/aemr-bot.env.bak.", str(tmp_path / "env.bak."))
    content = content.replace("HEALTH_TIMEOUT_SEC=60", "HEALTH_TIMEOUT_SEC=2")
    content = content.replace("HEALTH_POLL_INTERVAL_SEC=5", "HEALTH_POLL_INTERVAL_SEC=1")
    installed = tmp_path / "installed"
    installed.mkdir()
    content = content.replace("/usr/local/bin/", str(installed) + "/")
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "new.sh").write_text(content)
    # Minimal legacy controller reproduces the old HEAD-only skip. Keep this
    # fixture self-contained: CI uses a shallow checkout without old commits.
    old = f'''#!/bin/bash
set -euo pipefail
cd "{repo}"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then exit 0; fi
git reset --hard "$REMOTE" --quiet
install -m 0755 "{repo}/scripts/auto-deploy.sh" "{installed / 'aemr-bot-autodeploy'}"
su - aemr -c "docker compose up -d --build"
'''
    (versions / "old.sh").write_text(old)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "auto-deploy.sh").write_text(content)
    (repo / "scripts" / "healthwatch.sh").write_text("#!/bin/sh\nexit 0\n")
    installed_script = installed / "aemr-bot-autodeploy"
    installed_script.write_text(content)
    script = tmp_path / "deploy.sh"
    script.write_text(content)
    env = dict(
        os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
        AEMR_TEST_HEAD=str(head), AEMR_TEST_BUILDS=str(builds),
        AEMR_TEST_RESETS=str(resets), AUTODEPLOY_ENABLED="1",
        AEMR_TEST_VERSIONS=str(versions), AEMR_TEST_REPO=str(repo),
        REQUIRE_SIGNED_COMMITS="", REQUIRE_CANONICAL_MARKER="",
    )

    def run(**overrides):
        return subprocess.run(
            ["bash", str(installed_script if overrides.get("AEMR_TEST_REAL_INSTALL") == "1" else script)], env={**env, **overrides}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=5,
        )

    return run, head, builds, resets, repo


def test_failed_build_restores_source_and_retries_same_commit(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    first = run(AEMR_TEST_BUILD_RC="42")
    assert first.returncode == 42
    assert head.read_text().strip() == "old"
    second = run()
    assert second.returncode == 0
    assert head.read_text().strip() == "new"
    assert builds.read_text().splitlines() == ["attempt", "attempt"]
    assert (repo / "infra" / ".env").read_text() == "SYNTHETIC_CONFIG=preserved\n"
    assert resets.read_text().splitlines() == ["new", "old", "new"]


def test_successful_deployment_skips_unchanged_commit(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    assert run().returncode == 0
    assert run().returncode == 0
    assert builds.read_text().splitlines() == ["attempt"]


def test_failed_liveness_still_rolls_back(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    assert run(AEMR_TEST_LIVE_RC="22").returncode == 1
    assert head.read_text().strip() == "old"
    assert builds.read_text().splitlines() == ["attempt", "attempt"]
    assert resets.read_text().splitlines() == ["new", "old"]


def test_readiness_failure_does_not_roll_back(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    assert run(AEMR_TEST_READY_RC="22").returncode == 0
    assert head.read_text().strip() == "new"
    assert builds.read_text().splitlines() == ["attempt"]


def test_installed_retry_controller_survives_two_failed_upgrades(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    assert run(AEMR_TEST_REAL_INSTALL="1", AEMR_TEST_BUILD_RC="42").returncode == 42
    assert run(AEMR_TEST_REAL_INSTALL="1", AEMR_TEST_BUILD_RC="42").returncode == 42
    assert run(AEMR_TEST_REAL_INSTALL="1").returncode == 0
    assert builds.read_text().splitlines() == ["attempt"] * 3
    assert head.read_text().strip() == "new"


def test_new_controller_retries_head_left_by_legacy_failed_build(deploy_harness):
    run, head, builds, resets, repo = deploy_harness
    # The old controller moved HEAD then failed before this controller existed.
    head.write_text('new\n')
    assert run().returncode == 0
    assert builds.read_text().splitlines() == ['attempt']
