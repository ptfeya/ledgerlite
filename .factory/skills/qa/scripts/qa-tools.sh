#!/usr/bin/env bash
# Browser QA tooling for CI: pinned install and preflight.
#
#   qa-tools.sh install    Trusted setup. Installs agent-browser (npm lockfile with integrity
#                          hashes, no lifecycle scripts), a pinned Chrome for Testing build
#                          (sha256-checked), and droid-control (marketplace pinned to a commit).
#                          Never fails the job: each failure is recorded and surfaces as BLOCKED
#                          in the preflight.
#   qa-tools.sh preflight  Checks that the tools and the browser actually work against the running
#                          QA server and writes qa-results/preflight.txt. Always exits 0.
#
# Both run in steps without secrets, from the base branch's copy of this script. The keyed agent
# only reads preflight.txt; it never runs this script (see the qa skill, Step 4).
#
# Pins live in one place each: agent-browser in tools/package.json (plus package-lock.json),
# the marketplace repo and commit in .factory/settings.json, Chrome for Testing below. To bump
# one, change it there and check it against the upstream source:
#   agent-browser: `npm view agent-browser@<v> dist.integrity`; the lockfile must match.
#   Chrome for Testing: https://googlechromelabs.github.io/chrome-for-testing/ ; recompute the
#     sha256 of the linux64 zip from a download you made yourself.
#   droid-control: a full commit SHA of the marketplace repo, in .factory/settings.json.
set -uo pipefail

CHROME_VERSION=157.0.8103.78
CHROME_SHA256=9223832977a13db3646201b1341c1f9cf08afa3ceae92540116d7b0c062364fb
CHROME_URL="https://storage.googleapis.com/chrome-for-testing-public/$CHROME_VERSION/linux64/chrome-linux64.zip"
DROID_CONTROL_VERSION=1.2.0

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOLS=$HOME/qa-tools
AB_BIN=$TOOLS/ab/node_modules/.bin/agent-browser
CHROME_BIN=$TOOLS/chrome/chrome-linux64/chrome
FAILURES=$TOOLS/install-failures
SAFE_PATH=/usr/local/bin:/usr/bin:/bin

AGENT_BROWSER_VERSION=$(jq -r '.dependencies["agent-browser"] // empty' "$HERE/../tools/package.json" 2>/dev/null)
_marketplace='.extraKnownMarketplaces["factory-plugins"].source'
PLUGINS_REPO=https://github.com/$(jq -r "$_marketplace.repo // empty" "$HERE/../../../settings.json" 2>/dev/null)
PLUGINS_SHA=$(jq -r "$_marketplace.sha // empty" "$HERE/../../../settings.json" 2>/dev/null)

step_agent_browser() {
  set -e
  rm -rf "$TOOLS/ab"; mkdir -p "$TOOLS/ab"
  cp "$HERE/../tools/package.json" "$HERE/../tools/package-lock.json" "$TOOLS/ab/"
  cd "$TOOLS/ab"
  npm ci --ignore-scripts --no-audit --no-fund
  [ "$("$AB_BIN" --version)" = "agent-browser $AGENT_BROWSER_VERSION" ]
}

step_chrome() {
  set -e
  rm -rf "$TOOLS/chrome"; mkdir -p "$TOOLS/chrome"
  curl -fsSL --retry 3 --connect-timeout 10 -o "$TOOLS/chrome.zip" "$CHROME_URL"
  echo "$CHROME_SHA256  $TOOLS/chrome.zip" | sha256sum -c -
  unzip -q "$TOOLS/chrome.zip" -d "$TOOLS/chrome"
  rm -f "$TOOLS/chrome.zip"
  [ -x "$CHROME_BIN" ]
}

step_droid_control() {
  set -e
  command -v droid >/dev/null
  # A 40-character SHA suffix pins the marketplace checkout; it registers as
  # factory-plugins@<first 8 of the SHA>.
  [ -n "$AGENT_BROWSER_VERSION" ] && [ "${#PLUGINS_SHA}" -eq 40 ]
  droid plugin marketplace add "$PLUGINS_REPO@$PLUGINS_SHA"
  local loc="$HOME/.factory/plugins/marketplaces/factory-plugins@${PLUGINS_SHA:0:8}"
  [ "$(git -C "$loc" rev-parse HEAD)" = "$PLUGINS_SHA" ]
  droid plugin install "droid-control@factory-plugins@${PLUGINS_SHA:0:8}"
  local f
  for f in "$HOME"/.factory/plugins/known_marketplaces/factory-plugins-"${PLUGINS_SHA:0:8}"-*.json; do
    jq '.entry.autoUpdate = false' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  done
}

install() {
  mkdir -p "$TOOLS"; : > "$FAILURES"
  local s
  for s in agent_browser chrome droid_control; do
    echo "::group::qa-tools install: $s"
    if ! bash -c 'set -euo pipefail; source "$1" ""; step_"$2"' _ "$0" "$s" 2> >(tee -a "$TOOLS/$s.log" >&2) > >(tee -a "$TOOLS/$s.log"); then
      echo "$s: install failed (see the 'Install QA browser tooling' step log)" >> "$FAILURES"
      echo "::warning::qa-tools: $s install failed; web QA will be reported BLOCKED"
    fi
    echo "::endgroup::"
  done
  [ -z "${GITHUB_PATH:-}" ] || echo "$(dirname "$AB_BIN")" >> "$GITHUB_PATH"
  [ -z "${GITHUB_ENV:-}" ] || echo "AGENT_BROWSER_EXECUTABLE_PATH=$CHROME_BIN" >> "$GITHUB_ENV"
  return 0
}

preflight() {
  local out=qa-results/preflight.txt
  local blocked=() appfail=() ok=() r
  mkdir -p "$(dirname "$out")"

  block() { blocked+=("$1: $2"); }
  fail_app() { appfail+=("$1: $2"); }

  [ -n "$AGENT_BROWSER_VERSION" ] && [ "${#PLUGINS_SHA}" -eq 40 ] \
    || block pins "could not read the agent-browser version or marketplace pin from the trusted manifests"

  while IFS= read -r r; do [ -n "$r" ] && block "install" "$r"; done < <(cat "$FAILURES" 2>/dev/null)

  # agent-browser
  if [ ! -x "$AB_BIN" ]; then
    block agent-browser "not installed at $AB_BIN"
  elif [ "$("$AB_BIN" --version 2>&1)" != "agent-browser $AGENT_BROWSER_VERSION" ]; then
    block agent-browser "version is not the pinned $AGENT_BROWSER_VERSION"
  else
    ok+=("agent-browser $AGENT_BROWSER_VERSION")
  fi

  # browser binary
  local cver=""
  if [ -x "$CHROME_BIN" ]; then
    cver=$("$CHROME_BIN" --version 2>&1 | head -1)
    case $cver in
      *"$CHROME_VERSION"*) ok+=("chrome $CHROME_VERSION") ;;
      *) block chrome "unexpected version output: ${cver:-none}" ;;
    esac
  else
    block chrome "binary missing at $CHROME_BIN"
  fi

  # droid-control: enabled and at the pinned commit
  local pl
  pl=$(droid plugin list 2>&1 | grep 'droid-control@factory-plugins' || true)
  if [ -z "$pl" ]; then
    block droid-control "plugin is not installed"
  elif ! grep -q 'Enabled' <<<"$pl"; then
    block droid-control "plugin is installed but not enabled"
  elif ! grep -q "${PLUGINS_SHA:0:12}" <<<"$pl"; then
    block droid-control "plugin is not at pinned commit ${PLUGINS_SHA:0:12}: $pl"
  else
    ok+=("droid-control $DROID_CONTROL_VERSION @ ${PLUGINS_SHA:0:12}")
  fi

  # Optional media tools, only when the config asks for them.
  if [ "${QA_NEED_FFMPEG:-false}" = true ]; then
    ffmpeg -version >/dev/null 2>&1 && ok+=(ffmpeg) || block ffmpeg "not working but required by config.yaml"
  fi
  if [ "${QA_NEED_IMAGEMAGICK:-false}" = true ]; then
    { convert -version >/dev/null 2>&1 || magick -version >/dev/null 2>&1; } \
      && ok+=(imagemagick) || block imagemagick "not working but required by config.yaml"
  fi

  # End-to-end. First prove the browser works on a page the app does not serve, so a broken
  # app can never look like broken tooling. Then check the app's login page: failures there
  # are application findings (reported FAIL), not BLOCKED. Scrubbed environment, own session.
  if [ ${#blocked[@]} -eq 0 ]; then
    local url snap code session=qa-preflight
    local probe='data:text/html,<label>Password<input type=password></label>'
    ab() { env -i PATH="$SAFE_PATH:$TOOLS/ab/node_modules/.bin" HOME="$HOME" \
      AGENT_BROWSER_EXECUTABLE_PATH="$CHROME_BIN" timeout 90 "$AB_BIN" --session "$session" "$@"; }
    if ! snap=$(ab open "$probe" 2>&1 && ab snapshot -i 2>&1); then
      block browser-launch "could not open a static test page: $(tr '\n' ' ' <<<"$snap" | cut -c1-400)"
    elif ! grep -qi 'password' <<<"$snap"; then
      block browser-render "static test page rendered without its password field: $(tr '\n' ' ' <<<"$snap" | cut -c1-400)"
    else
      ok+=("browser end-to-end: rendered a static test page")
    fi

    if [ ${#blocked[@]} -eq 0 ]; then
      url=$(env -i PATH=$SAFE_PATH HOME=/nonexistent "$HERE/qa-server.sh" url 2>&1) \
        && code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$url/ui/login") \
        || block qa-server "url or probe failed: $url"
      if [ ${#blocked[@]} -eq 0 ]; then
        if [ "$code" != 200 ]; then
          fail_app ui-login "GET $url/ui/login returned HTTP $code"
        elif ! snap=$(ab open "$url/ui/login" 2>&1 && ab snapshot -i 2>&1) \
          || ! grep -qi 'password' <<<"$snap"; then
          fail_app ui-login "login page did not render a password field: $(tr '\n' ' ' <<<"$snap" | cut -c1-400)"
        else
          ok+=("app end-to-end: rendered $url/ui/login")
        fi
      fi
    fi
    ab close >/dev/null 2>&1 || true
  fi

  {
    if [ ${#blocked[@]} -eq 0 ]; then
      echo "QA_PREFLIGHT: OK"
    else
      echo "QA_PREFLIGHT: BLOCKED"
      echo "Scope: all web (browser) flows. API flows (curl) are not affected."
      for r in "${blocked[@]}"; do echo "BLOCKED $r"; done
    fi
    for r in "${appfail[@]}"; do echo "APP_FAIL $r"; done
    for r in "${ok[@]}"; do echo "ok $r"; done
  } > "$out"

  cat "$out"
  if [ ${#blocked[@]} -gt 0 ]; then
    echo "::warning::QA browser preflight BLOCKED: ${blocked[0]}"
  fi
  if [ ${#appfail[@]} -gt 0 ]; then
    echo "::warning::QA preflight found an application failure: ${appfail[0]}"
  fi
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    { echo '### QA browser preflight'; echo '```'; cat "$out"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
  fi
  return 0
}

# Allow `bash -c 'source qa-tools.sh; step_x'` for the install sub-shells.
case ${1:-} in
  install) install ;;
  preflight) preflight ;;
  "") ;;
  *) echo "usage: qa-tools.sh install|preflight" >&2; exit 2 ;;
esac
