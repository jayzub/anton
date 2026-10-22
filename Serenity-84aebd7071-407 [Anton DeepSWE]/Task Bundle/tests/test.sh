#!/bin/bash
# Verifier entrypoint (shared frame; synced by tools/sync_verifier.py).
# Patching and grading live in tests/grader.py. This script owns the
# task-specific part: run the suites, write reports under /logs/verifier/,
# and apply any report fixups before grading.
#
# ┌─ SHARED FRAME PREFIX (identical across all 113 tasks) ─────────────────────
set -uo pipefail
# Discard any reward artifact that exists BEFORE grading starts. The trap below treats an
# existing reward file as "grading already scored this", so a reward present on entry is
# either stale or planted -- a solver that writes /logs/verifier/reward.json and then aborts
# the test process would otherwise have its forgery preserved rather than overwritten. The
# only legitimate writer is `grader.py grade`, which runs later in this script.
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.json /logs/verifier/reward.txt
trap 'if [ ! -f /logs/verifier/reward.json ] && [ ! -f /logs/verifier/reward.txt ]; then mkdir -p /logs/verifier; echo -1 > /logs/verifier/reward.txt; fi' EXIT
log() { echo "[verifier] $*"; }
# Grading resolves its own tools before anything the solver may have put on PATH. The
# reporter here produces the record grader.py trusts, so picking up a solver-supplied
# `go`, `python3` or reporter would let the graded result be authored by the thing
# being graded.
# Trusted prefix first. The inherited PATH is kept as a SUFFIX only so a toolchain installed
# somewhere non-standard by the per-task block still resolves — `require_cmd` is what makes
# that safe, by rejecting any tool that resolves somewhere the solver could have written.
export PATH="/opt/ctrf/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"
cd /app || { mkdir -p /logs/verifier; exit 6; }

# Reap the solver's processes BEFORE touching the repo. Grading shares the container with the
# agent phase, so a backgrounded solver process can restore a malicious git config after the
# sanitizer rewrites it and before `grader.py prepare` runs git — a TOCTOU a point-in-time
# rewrite cannot close, and it reproduces reliably.
#
# pid 1 is spared: the image ends `USER solver` with `CMD ["/bin/bash"]`, so container init runs
# as solver, and killing it stops the container and takes this root verifier exec with it.
#
# /proc is walked directly because procps (pgrep/pkill) is not guaranteed in the base image.
# Our own ancestry runs as root and so is never matched.
_proc_uid()   { awk '/^Uid:/{print $2; exit}'   "$1/status" 2>/dev/null; }
_proc_state() { awk '/^State:/{print $2; exit}' "$1/status" 2>/dev/null; }
_reap_solver() {
  _suid=$(id -u solver 2>/dev/null) || return 0
  [ -n "$_suid" ] || return 0
  # Signal EVERY solver-owned pid regardless of state. A signal to a true zombie is a no-op,
  # but a thread-group leader can be Z while its worker threads still run, and those threads
  # are reachable ONLY by signalling the tgid — they are not listed under /proc/[0-9]*.
  for _sig in TERM KILL; do
    for _pd in /proc/[0-9]*; do
      _pid=${_pd#/proc/}
      [ "$_pid" = "1" ] && continue
      [ -r "$_pd/status" ] || continue
      [ "$(_proc_uid "$_pd")" = "$_suid" ] || continue
      kill -"$_sig" "$_pid" 2>/dev/null || true
    done
  done
  # A pid is gone only when every THREAD is gone, so the survivor check is per-thread: a Z
  # leader hiding a live worker must not pass, while a fully-dead process (all threads Z,
  # awaiting a wait() that container init may never issue) must not block grading.
  _left=""
  for _pd in /proc/[0-9]*; do
    _pid=${_pd#/proc/}
    [ "$_pid" = "1" ] && continue
    [ -r "$_pd/status" ] || continue
    [ "$(_proc_uid "$_pd")" = "$_suid" ] || continue
    for _td in "$_pd"/task/[0-9]*; do
      [ -r "$_td/status" ] || continue
      [ "$(_proc_state "$_td")" = "Z" ] && continue
      _left="$_left ${_pid}/${_td##*/}"
    done
  done
  [ -z "$_left" ] || { log "ERROR: live solver thread(s) survived SIGKILL:$_left; refusing to grade"; exit 10; }
}
_reap_solver

# The solver owns /app, and therefore /app/.git — it can write, replace or delete the repo
# config, and git executes what that config names (filter/textconv/fsmonitor/sshCommand/
# pager/url.insteadOf/merge drivers/gpg.program/sequence.editor) as WHOEVER RUNS GIT. The
# verifier below runs git as ROOT during `grader.py prepare`, so an unsanitized repo config is
# root code execution for the solver. Root-owning the file does not help: the sticky bit
# exempts the directory owner, and `git config` rewrites via lock+rename.
#
# Rebuild the config from an allowlist, validating VALUES as well as keys: configparser accepts
# multiline values, so a crafted `core.filemode` can carry a newline that opens a
# `[filter "evil"]` section when written back and push the appended hooksPath outside [core].
# Refuse redirected layouts (`gitdir:` pointer, `commondir`, alternates) rather than sanitizing
# a file that is not the one git will read. This script FAILS CLOSED and the caller checks it,
# because this file runs under `set -uo pipefail` with no `set -e`.
#
# `.gitattributes` is deliberately kept: repos legitimately ship one (line-ending and linguist
# rules) and removing it changes what checkout produces, hence what the tests see. A binding
# whose driver no longer exists is inert. `.git/info/attributes` is removed — no real repo
# ships that, and it is inside the solver-owned .git.
python3 - <<'GIT_SANITIZE_EOF'
import configparser, os, re, sys

GITDIR, CFG = "/app/.git", "/app/.git/config"
ALLOWED = {
    "repositoryformatversion": r"\A[01]\Z",
    "filemode": r"\A(true|false)\Z",
    "bare": r"\A(true|false)\Z",
    "logallrefupdates": r"\A(true|false|always)\Z",
    "ignorecase": r"\A(true|false)\Z",
    "precomposeunicode": r"\A(true|false)\Z",
    "symlinks": r"\A(true|false)\Z",
    "autocrlf": r"\A(true|false|input)\Z",
    "eol": r"\A(lf|crlf|native)\Z",
}

def die(msg):
    print("[verifier] git config sanitize FAILED: " + msg, file=sys.stderr)
    raise SystemExit(3)

if not os.path.isdir(GITDIR) or os.path.islink(GITDIR):
    die(GITDIR + " is not a plain directory (gitdir: pointer or worktree link)")
for redirect in ("commondir", "objects/info/alternates"):
    if os.path.exists(os.path.join(GITDIR, redirect)):
        die(redirect + " exists and redirects repository state")
if os.path.islink(CFG):
    die(CFG + " is a symlink")

kept = {}
parser = configparser.RawConfigParser(strict=False)
try:
    with open(CFG, encoding="utf-8", errors="replace") as fh:
        parser.read_file(fh)
except FileNotFoundError:
    pass
except Exception:
    kept = {}
else:
    for section in parser.sections():
        if section.split('"')[0].strip().lower() != "core":
            continue
        for key, raw in parser.items(section):
            pat = ALLOWED.get(key.lower())
            if pat and raw is not None and re.match(pat, raw.strip(), re.I):
                kept[key.lower()] = raw.strip()

lines = ["[core]"]
lines += ["\t%s = %s" % (k, v) for k, v in sorted(kept.items()) if k != "hookspath"]
lines += ["\thooksPath = /dev/null", "[user]", "\tname = verifier",
          "\temail = verifier@localhost", "[safe]", "\tdirectory = /app", ""]
body = "\n".join(lines)

check = configparser.RawConfigParser(strict=False)
try:
    check.read_string(body)
except Exception as exc:
    die("rebuilt config does not parse: %s" % exc)
extra = {s.split('"')[0].strip().lower() for s in check.sections()} - {"core", "user", "safe"}
if extra:
    die("rebuilt config grew unexpected sections: %s" % sorted(extra))
if check.get("core", "hooksPath", fallback=None) != "/dev/null":
    die("hooksPath did not land in [core]")

tmp = CFG + ".sanitized"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(body)
os.replace(tmp, CFG)
os.chmod(CFG, 0o644)
print("[verifier] git config sanitized (kept %s)" % sorted(kept))
GIT_SANITIZE_EOF
# Explicit, because this file has no `set -e`: an unsanitized repo must not be graded.
[ $? -eq 0 ] || { log "ERROR: git config sanitize failed; refusing to grade"; exit 8; }
rm -f /app/.git/info/attributes

python3 /tests/grader.py prepare || exit $?

# Canonical raw-output log. The task middle SHOULD send every suite's combined
# stdout+stderr here so the reason a test failed is never lost -- use run_log,
# or pipe through `tee -a "$RUN_LOG"` when feeding a reporter. Never 2>/dev/null
# a test run. FRAME_SUFFIX cats this (and any other raw logs) into test-stdout.
export RUN_LOG=/logs/verifier/run.log
: > "$RUN_LOG" 2>/dev/null || true
run_log() { echo "+ $*" >> "$RUN_LOG" 2>/dev/null; "$@" 2>&1 | tee -a "$RUN_LOG"; return "${PIPESTATUS[0]}"; }
# └────────────────────────────────────────────────────────────────────────────

# >>> RUN TESTS (task-specific) <<<
# (scan-config rationale:)
# Cheating signal (recorded only): test-logger.targets, Directory.Build.props, Directory.Build.targets, *.sln, *.slnx, *.csproj, global.json, nuget.config.
# Out-of-scope signal (recorded only): paths outside src/services/**, src/core/**.

# Verify what PATH RESOLVED TO, rather than trusting PATH order.
_nonroot_writable() {
  _m=$(printf %s "$1" | tail -c 3)
  _g=$(printf %s "$_m" | cut -c2); _o=$(printf %s "$_m" | cut -c3)
  case "$_g" in 2|3|6|7) return 0 ;; esac
  case "$_o" in 2|3|6|7) return 0 ;; esac
  return 1
}
_require_one() {
  _p=$(command -v "$1" 2>/dev/null) || { log "ERROR: missing $1; PATH=$PATH"; exit 127; }
  case "$_p" in /*) ;; *) log "ERROR: $1 is not a file path ($_p)"; exit 9 ;; esac
  _rp=$(readlink -f "$_p" 2>/dev/null); [ -n "$_rp" ] || _rp="$_p"
  _own=$(stat -Lc %u "$_rp" 2>/dev/null || echo 1)
  _fmode=$(stat -Lc %a "$_rp" 2>/dev/null || echo 777)
  if [ "$_own" != "0" ]; then
    log "ERROR: $1 -> $_rp is not root-owned (uid=$_own); refusing to grade"; exit 9
  fi
  if _nonroot_writable "$_fmode"; then
    log "ERROR: $1 -> $_rp mode $_fmode is writable by non-root; refusing to grade"; exit 9
  fi
  for _cand in "$_p" "$_rp"; do
    _d=$(dirname "$_cand")
    _down=$(stat -Lc %u "$_d" 2>/dev/null || echo 1)
    _dmode=$(stat -Lc %a "$_d" 2>/dev/null || echo 777)
    if [ "$_down" != "0" ]; then
      log "ERROR: $1 -> directory $_d is not root-owned (uid=$_down); refusing to grade"; exit 9
    fi
    if _nonroot_writable "$_dmode"; then
      log "ERROR: $1 -> directory $_d mode $_dmode is writable by non-root; refusing to grade"; exit 9
    fi
  done
}
require_cmd() { for _tool in "$@"; do _require_one "$_tool"; done; }
require_cmd dotnet python3

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
CUSTOM_TARGETS="/opt/test-logger/test-logger.targets"

set +e
dotnet test tests/Serenity.Net.Tests/Serenity.Net.Tests.csproj --no-restore \
  --filter "FullyQualifiedName!~ListRequestHandlerKeysetPagingTests&(FullyQualifiedName~ListRequestHandler|FullyQualifiedName~ListHandlerProxy|FullyQualifiedName~ServiceHelperTests)" \
  --logger "junit;LogFilePath=/logs/verifier/base.xml" \
  /p:CustomAfterMicrosoftCommonTargets="$CUSTOM_TARGETS" \
  2>&1 | tee -a "$RUN_LOG"

dotnet test tests/Serenity.Net.Tests/Serenity.Net.Tests.csproj --no-restore \
  --filter "FullyQualifiedName~ListRequestHandlerKeysetPagingTests" \
  --logger "junit;LogFilePath=/logs/verifier/new.xml" \
  /p:CustomAfterMicrosoftCommonTargets="$CUSTOM_TARGETS" \
  2>&1 | tee -a "$RUN_LOG"
set -e
# >>> END RUN TESTS <<<

# ┌─ SHARED FRAME SUFFIX (identical across all 113 tasks) ─────────────────────
# Surface raw suite output into our stdout (the harness captures it into
# test-stdout.txt) so failures are debuggable even when the framework report
# omits the reason (e.g. cargo-nextest). Reasons-per-test come from grade below.
_seen=""
for _rl in "$RUN_LOG" /logs/verifier/*_run.log /logs/verifier/*-run.log /logs/verifier/*-mocha.log /logs/verifier/*.log /logs/verifier/*.out; do
  [ -f "$_rl" ] && [ -s "$_rl" ] || continue
  case " $_seen " in *" $_rl "*) continue ;; esac
  case "${_rl##*/}" in *convert*.log|ctrf*.log|junit*.log) continue ;; esac
  _seen="$_seen $_rl"
  echo "===== raw suite output: ${_rl##*/} ====="
  cat "$_rl"
done 2>/dev/null
echo "===== grade ====="

python3 /tests/grader.py grade
log "reward.json=$(cat /logs/verifier/reward.json 2>/dev/null)"

# Uniform top level: keep only the canonical artifacts at /logs/verifier and
# tuck every framework-native report/log under reports/ (full provenance, no
# data dropped -- just moved). Canonical: reward.json, ctrf.json, run.log, and
# the harness-written test-stdout.txt.
mkdir -p /logs/verifier/reports 2>/dev/null
for _f in /logs/verifier/*; do
  case "${_f##*/}" in
    reward.json|reward.txt|ctrf.json|run.log|test-stdout.txt|reports) continue ;;
  esac
  [ -f "$_f" ] && mv -f "$_f" /logs/verifier/reports/ 2>/dev/null
done
# └────────────────────────────────────────────────────────────────────────────
