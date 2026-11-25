"""Offline tests for the QA browser tooling script's reporting contract.

The script runs against stub executables (no network, no browser), so these check how
install failures, missing tools, browser failures and application failures are reported.
"""

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "darwin", reason="Requires Linux (Bash 4+ and the Linux Chrome build)."
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO / ".factory" / "skills" / "qa" / "scripts"
SETTINGS = (REPO / ".factory" / "settings.json").read_text()
PACKAGE = (REPO / ".factory" / "skills" / "qa" / "tools" / "package.json").read_text()
CHROME_VERSION = re.search(
    r"^CHROME_VERSION=(\S+)", (SCRIPT_DIR / "qa-tools.sh").read_text(), re.M
)[1]


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.stubs = root / "stubs"
        skill = root / ".factory" / "skills" / "qa"
        (skill / "scripts").mkdir(parents=True)
        (skill / "tools").mkdir()
        shutil.copy(SCRIPT_DIR / "qa-tools.sh", skill / "scripts" / "qa-tools.sh")
        (root / ".factory" / "settings.json").write_text(SETTINGS)
        (skill / "tools" / "package.json").write_text(PACKAGE)
        (skill / "tools" / "package-lock.json").write_text("{}")
        self.script = skill / "scripts" / "qa-tools.sh"
        self.sha = re.search(r'"sha": "([0-9a-f]{40})"', SETTINGS)[1]
        self.version = re.search(r'"agent-browser": "([^"]+)"', PACKAGE)[1]
        tools = self.home / "qa-tools"
        _exe(
            tools / "ab/node_modules/.bin/agent-browser",
            f"""
            [ "$1" = --version ] && {{ echo "agent-browser {self.version}"; exit 0; }}
            shift 2
            case $1 in
              open)
                echo "$2" > "$HOME/last-url"
                [ -f "$HOME/ab-launch-fails" ] && {{ echo "cannot launch chrome" >&2; exit 1; }}
                exit 0 ;;
              snapshot)
                if [ -f "$HOME/ab-hide-password" ] || {{ [ -f "$HOME/ab-app-broken" ] \\
                  && [[ $(cat "$HOME/last-url") == http* ]]; }}; then
                  echo "heading Internal Server Error"
                else
                  echo 'textbox "Password"'
                fi ;;
            esac
            """,
        )
        _exe(
            tools / "chrome/chrome-linux64/chrome", f'echo "Chrome for Testing {CHROME_VERSION}"\n'
        )
        _exe(skill / "scripts" / "qa-server.sh", 'echo "http://localhost:8000"\n')
        _exe(
            self.stubs / "droid",
            f"""
            if [ "$1 $2" = "plugin list" ]; then
              [ -f "$HOME/droid-missing" ] \\
                || echo "droid-control@factory-plugins Enabled {self.sha[:12]}"
              exit 0
            fi
            exit 1
            """,
        )
        _exe(
            self.stubs / "curl",
            """
            for a in "$@"; do
              [ "$a" = "-w" ] && { printf '%s' "${STUB_HTTP_CODE:-200}"; exit 0; }
            done
            exit 22
            """,
        )
        _exe(self.stubs / "npm", "exit 1\n")

    def flag(self, name: str) -> None:
        (self.home / name).write_text("")

    def run(self, command: str, **env: str) -> tuple[subprocess.CompletedProcess, str]:
        full_env = {
            "PATH": f"{self.stubs}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            **env,
        }
        proc = subprocess.run(
            [str(self.script), command],
            cwd=self.root,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        report = self.root / "qa-results" / "preflight.txt"
        return proc, report.read_text() if report.exists() else ""


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def test_healthy_tooling_reports_ok(box):
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert report.splitlines()[0] == "QA_PREFLIGHT: OK"
    assert "BLOCKED" not in report and "APP_FAIL" not in report
    assert "app end-to-end: rendered http://localhost:8000/ui/login" in report


def test_missing_browser_binary_is_blocked(box):
    (box.home / "qa-tools/chrome/chrome-linux64/chrome").unlink()
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert report.splitlines()[0] == "QA_PREFLIGHT: BLOCKED"
    assert "BLOCKED chrome: binary missing" in report


def test_missing_droid_control_is_blocked(box):
    box.flag("droid-missing")
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert "BLOCKED droid-control: plugin is not installed" in report


def test_recorded_install_failures_are_blocked(box):
    failures = box.home / "qa-tools" / "install-failures"
    failures.write_text("chrome: install failed (see log)\n")
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert report.splitlines()[0] == "QA_PREFLIGHT: BLOCKED"
    assert "BLOCKED install: chrome: install failed" in report


def test_unlaunchable_browser_is_blocked_even_with_a_healthy_app(box):
    box.flag("ab-launch-fails")
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert report.splitlines()[0] == "QA_PREFLIGHT: BLOCKED"
    assert "BLOCKED browser-launch: could not open a static test page" in report
    assert "APP_FAIL" not in report


@pytest.mark.parametrize("mode", ["http500", "no-password-field"])
def test_application_rendering_failure_is_a_fail_not_blocked(box, mode):
    if mode == "http500":
        env = {"STUB_HTTP_CODE": "500"}
    else:
        box.flag("ab-app-broken")
        env = {}
    proc, report = box.run("preflight", **env)
    assert proc.returncode == 0
    assert report.splitlines()[0] == "QA_PREFLIGHT: OK"
    assert "BLOCKED" not in report
    assert "APP_FAIL ui-login:" in report
    assert "browser end-to-end: rendered a static test page" in report


def test_unreadable_pins_are_blocked(box):
    (box.root / ".factory" / "settings.json").write_text("{}")
    proc, report = box.run("preflight")
    assert proc.returncode == 0
    assert "BLOCKED pins:" in report


def test_install_never_fails_records_failures_and_exports_locations(box):
    path_file, env_file = box.root / "gh-path", box.root / "gh-env"
    proc, _ = box.run("install", GITHUB_PATH=str(path_file), GITHUB_ENV=str(env_file))
    assert proc.returncode == 0
    recorded = (box.home / "qa-tools" / "install-failures").read_text()
    for step in ("agent_browser", "chrome", "droid_control"):
        assert f"{step}: install failed" in recorded
    assert path_file.read_text().strip() == str(box.home / "qa-tools/ab/node_modules/.bin")
    chrome = box.home / "qa-tools/chrome/chrome-linux64/chrome"
    assert env_file.read_text().strip() == f"AGENT_BROWSER_EXECUTABLE_PATH={chrome}"


def test_script_does_not_inherit_overrides(box):
    elsewhere = box.root / "elsewhere"
    proc, _ = box.run("preflight", QA_TOOLS_DIR=str(elsewhere), QA_PREFLIGHT_OUT=str(elsewhere))
    assert proc.returncode == 0
    assert not elsewhere.exists()
    assert os.path.exists(box.root / "qa-results" / "preflight.txt")
