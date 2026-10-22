#!/usr/bin/env python3
"""DeepSWE v1.1 task verifier — one shared script, entered via tests/test.sh.

Shared verbatim by every task (canonical copy: tools/verifier/grader.py,
synced + CI-checked by tools/sync_verifier.py). All per-task data lives in
config.json next to this file:

  base_commit    str   the upstream commit the task is built at (provenance)
  p2p_node_ids   [str] pass-to-pass whitelist (must keep passing)
  f2p_node_ids   [str] fail-to-pass whitelist (prove the task is solved);
                       both materialized from the oracle-vs-nop differential
  graded_test_files [str] base-existing p2p files reset before grading
  graded_test_files_by_node_id {id: path} exact authoring-time provenance
  grade          {...} how to READ the reports test.sh produced (see below)

Subcommands:
  grader.py prepare                setup, apply test.patch
  grader.py grade                  reports -> reward.json (+ ctrf.json)
  grader.py patch-paths <patch>    print unique file paths a diff touches

$TESTS_DIR (default /tests), $VERIFIER_DIR (default /logs/verifier),
$APP_DIR (default /app) and $ARTIFACTS_DIR (default /logs/artifacts) are
overridable for testing/replays.

== prepare ==

Runs in $APP_DIR (the agent's repo with its committed work already in place;
image build steps may have modified tracked files in-tree, so resets are
per-file, never repo-wide): reset every file test.patch touches to its state
at the ORIGINAL base commit (config.json base_commit, NOT the agent-moved
HEAD — the agent commits its work, so HEAD may carry a file it created at the
exact path test.patch adds), then apply test.patch loudly (a failure here is
an infrastructure error: nonzero exit, no reward.json, so the test.sh trap
writes the reward.txt=-1 crash sentinel). The agent's own changes are graded
as-is — the verifier never applies a model.patch.

== grade: whitelisted node ids -> reward.json ==

An id missing from every report counts as FAILED (absence == failure), as
does a skipped test. Duplicate ids across/within reports merge
worst-status-wins (passed < skipped < failed). Whitelist ids and report
names are both whitespace-stripped; any further name canonicalization a
reporter needs is a task-local fixup in test.sh, BEFORE grade runs.

  reward    binary 0/1 (ranking): 1 iff |f2p| > 0, every f2p passes AND
            no p2p fails
  f2p_total / f2p_passed / p2p_total / p2p_passed   raw counts
  f2p       f2p_passed / f2p_total   (0.0 if the bucket is empty: no
                                      fail-to-pass evidence = nothing solved)
  p2p       p2p_passed / p2p_total   (1.0 vacuously if empty)
  partial   (f2p_passed + p2p_passed) / (f2p_total + p2p_total)

  config keys (under "grade"):
    format      "ctrf" | "junit"     report parser
    node_id     "suite.name" | "name"  (ctrf only) id derivation; junit
                                     always derives classname.name
    tool_label  str                  tool.name written into the synthesized
                                     ctrf.json (required CTRF provenance)
    reports     [path...]            parsed in order
"""
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TESTS_DIR = Path(os.environ.get("TESTS_DIR", "/tests"))
VERIFIER_DIR = Path(os.environ.get("VERIFIER_DIR", "/logs/verifier"))
APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/logs/artifacts"))
RANK = {"passed": 0, "skipped": 1, "failed": 2}
TEST_FILE_SUFFIXES = frozenset({
    ".c", ".cc", ".cjs", ".clj", ".cljs", ".cpp", ".cs", ".cxx", ".dart", ".erl",
    ".ex", ".exs", ".fs", ".fsx", ".go", ".groovy", ".h", ".hh", ".hpp", ".hrl",
    ".java", ".jl", ".js", ".jsx", ".kt", ".kts", ".lua", ".m", ".mjs", ".mm",
    ".php", ".pl", ".pm", ".py", ".pyi", ".r", ".rb", ".rs", ".scala", ".sh",
    ".sol", ".sql", ".svelte", ".swift", ".t", ".ts", ".tsx", ".vue", ".zig",
})


def log(msg):
    print(f"[verifier] {msg}", flush=True)


def load_config():
    return json.loads((TESTS_DIR / "config.json").read_text())


def safe_repo_path(value, legacy=False):
    if not isinstance(value, str):
        return None
    path = value.strip()
    if legacy:
        path = path.lstrip("/")
        if path.startswith("./"):
            path = path[2:]
    if (not path or path.startswith("/") or "\\" in path
            or any(ord(char) < 32 for char in path)
            or any(part in ("", ".", "..") or part.casefold() == ".git"
                   for part in path.split("/"))):
        return None
    return path


# --- patch helpers ---------------------------------------------------------

def patch_paths(path):
    """Return safe paths from Git's NUL-delimited patch metadata."""
    seen, out = set(), []
    for reverse in (False, True):
        args = ["git", "apply"]
        if reverse:
            args.append("--reverse")
        args.extend(("--allow-empty", "--numstat", "-z", str(path)))
        result = subprocess.run(args, capture_output=True, check=False)
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(detail or "git could not parse patch metadata")

        records = result.stdout.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                raise ValueError("git returned malformed patch metadata")
            raw_paths = [fields[2]]
            if not fields[2]:
                if index + 1 >= len(records):
                    raise ValueError("git returned incomplete rename metadata")
                raw_paths = records[index:index + 2]
                index += 2
            for raw in raw_paths:
                try:
                    candidate = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("patch path is not valid UTF-8") from exc
                if safe_repo_path(candidate) != candidate:
                    raise ValueError(f"unsafe patch path: {candidate!r}")
                if candidate not in seen:
                    seen.add(candidate)
                    out.append(candidate)
    return out


# --- prepare ---------------------------------------------------------------

def git(*args, **kw):
    return subprocess.run(["git", *args], cwd=APP_DIR, **kw)


def resolve_base_ref():
    try:
        cfg = load_config()
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("task_family") == "injected_bug_v1" and "task_baseline" not in cfg:
        raise ValueError("injected task baseline is missing")
    if "task_baseline" in cfg:
        baseline = cfg["task_baseline"]
        if not isinstance(baseline, dict) or baseline.get("kind") != "injected_bug_v1":
            raise ValueError("invalid injected task baseline")
        commit, tree = baseline.get("commit"), baseline.get("tree")
        if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v) for v in (commit, tree)):
            raise ValueError("invalid injected task commit/tree")
        actual = git("rev-parse", "--verify", f"{commit}^{{tree}}", capture_output=True, text=True)
        parents = git("rev-list", "--parents", "-n", "1", commit, capture_output=True, text=True)
        if actual.returncode or actual.stdout.strip() != tree or parents.stdout.strip() != commit:
            raise ValueError("injected task baseline is missing, has parents, or has the wrong tree")
        return commit
    # The reset target is the ORIGINAL base commit the task image was built at,
    # NOT HEAD: the agent commits its work (the instruction tells it to), so
    # HEAD carries agent commits and a file the agent created at the exact path
    # test.patch adds would survive a checkout-HEAD reset and then collide with
    # `git apply`. config.json base_commit is that pinned base; the git
    # time-travel Dockerfile checks the default branch out AT it, so it is an
    # ancestor of HEAD and resolvable here. Fall back to HEAD only for a
    # missing/unresolvable base (degenerate input), never crashing prepare.
    base = str(cfg.get("base_commit") or "").strip()
    if base and git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL).returncode == 0:
        return base
    log(f"WARNING: base_commit {base or '(none)'} unresolvable; "
        "resetting touched paths against HEAD")
    return "HEAD"


def remove_worktree_path(path):
    """Remove a repo-relative path without following a planted symlink ancestor."""
    current = APP_DIR
    for part in path.split("/")[:-1]:
        current /= part
        if current.is_symlink():
            current.unlink()
            return

    target = APP_DIR / path
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def reset_paths(paths, ref):
    # Reset every path test.patch touches to its state at the base ref so the
    # patch's preimage is exactly what git apply sees. Present at base -> restore
    # base contents (undo any agent edit/delete or in-tree build mutation);
    # absent at base -> the agent created the path (committed or not), so purge
    # it from BOTH the worktree and the index so a new-file diff cannot collide
    # with either. Paths the patch does not touch keep their agent-image state.
    for f in paths:
        if safe_repo_path(f) != f:
            raise ValueError(f"unsafe patch path: {f!r}")
        exists = git("cat-file", "-e", f"{ref}:{f}",
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL).returncode == 0
        remove_worktree_path(f)
        if exists:
            git("checkout", "-q", ref, "--", f, stderr=subprocess.DEVNULL)
        else:
            git("rm", "-q", "--cached", "--ignore-unmatch", "--", f,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def graded_test_paths(cfg, ref, patch_owned_paths=()):
    """Return base-existing graded files to reset before the verifier runs."""
    if not isinstance(cfg, dict):
        raise ValueError("config.json must contain an object")

    def at_base(p):
        return git("cat-file", "-e", f"{ref}:{p}",
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL).returncode == 0

    patch_owned = set(patch_owned_paths)
    if "task_baseline" in cfg:
        origins = cfg.get("test_origins")
        mapping = cfg.get("graded_test_files_by_node_id")
        ids = list(cfg.get("f2p_node_ids") or []) + list(cfg.get("p2p_node_ids") or [])
        if not isinstance(origins, dict) or not isinstance(mapping, dict) or set(origins) != set(ids):
            raise ValueError("injected tasks require an exact test_origins and node-to-file map")
        protected = set()
        for node in ids:
            path = safe_repo_path(mapping.get(node))
            if not path or origins[node] not in ("baseline", "test_patch"):
                raise ValueError("invalid injected test origin/path")
            if origins[node] == "baseline":
                if not at_base(path) or path in patch_owned:
                    raise ValueError("baseline test must exist in M and be untouched by test.patch")
                protected.add(path)
            elif path not in patch_owned:
                raise ValueError("test_patch origin must belong to test.patch")
        declared = cfg.get("graded_test_files")
        if not isinstance(declared, list) or not protected <= set(declared):
            raise ValueError("graded_test_files must protect every baseline-origin test")
        return sorted(protected)
    exact = "graded_test_files_by_node_id" in cfg
    declared = cfg.get("graded_test_files")
    if isinstance(declared, list):
        paths = {
            path for value in declared
            if (path := safe_repo_path(value, legacy=not exact))
        }
        if len(paths) != len(declared):
            raise ValueError("graded_test_files contains an unsafe or duplicate path")
        if paths:
            missing = sorted(path for path in paths if path not in patch_owned and not at_base(path))
            if missing:
                raise ValueError("graded_test_files contains path(s) absent at base: "
                                 + ", ".join(missing))
            return sorted(paths - patch_owned)
    elif declared is not None and exact:
        raise ValueError("graded_test_files must be a list")

    p2p = cfg.get("p2p_node_ids")
    if exact:
        if not isinstance(cfg["graded_test_files_by_node_id"], dict):
            raise ValueError("graded_test_files_by_node_id must be an object")
        if not isinstance(p2p, list):
            raise ValueError("p2p_node_ids must be a list")
        if any(str(value).strip() for value in p2p):
            raise ValueError("p2p tests require a non-empty graded_test_files reset boundary")
        return []

    # Compatibility for tasks emitted before exact node-to-file metadata existed.
    cands = set()
    for nid in list(cfg.get("f2p_node_ids") or []) + list(cfg.get("p2p_node_ids") or []):
        s = str(nid).strip()
        if not s:
            continue
        if "::" in s:
            prefix = s.split("::", 1)[0]
            if "/" in prefix or Path(prefix).suffix.lower() in TEST_FILE_SUFFIXES:
                path = safe_repo_path(prefix, legacy=True)
                if path:
                    cands.add(path)
            continue
        parts = s.split(".")
        for i in range(len(parts) - 1, 0, -1):
            path = safe_repo_path("/".join(parts[:i]) + ".py", legacy=True)
            if path:
                cands.add(path)
    return sorted(p for p in cands if at_base(p))


def cmd_prepare(argv):
    if not APP_DIR.is_dir():
        VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
        sys.exit(6)
    os.chdir(APP_DIR)
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                    str(APP_DIR)], stderr=subprocess.DEVNULL)

    test_patch = TESTS_DIR / "test.patch"
    try:
        base = resolve_base_ref()
        tp_paths = patch_paths(test_patch)
        log(f"Resetting files touched by test.patch to base {base}")
        reset_paths(tp_paths, base)
        graded_paths = graded_test_paths(load_config(), base, tp_paths)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        sys.exit(6)
    extra = [p for p in graded_paths if p not in set(tp_paths)]
    if extra:
        log(f"Resetting {len(extra)} pre-existing graded test file(s) to base")
        reset_paths(extra, base)
    log("Applying test.patch")
    r = git("apply", "--whitespace=nowarn", "--allow-empty", str(test_patch),
            capture_output=True, text=True)
    if r.returncode != 0:
        log("ERROR: test.patch failed to apply")
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit(r.returncode)
    try:
        inner = APP_DIR / "test.sh"
        inner.chmod(inner.stat().st_mode | 0o111)
    except OSError:
        pass


# --- grade -----------------------------------------------------------------

def norm_status(raw):
    raw = str(raw or "").strip().lower()
    if raw == "passed":
        return "passed"
    if raw in ("skipped", "pending", "other"):
        return "skipped"
    return "failed"


def add(res, nid, st, msg=""):
    # worst-status-wins: failed > skipped > passed; keep the failing entry's
    # full message. value is a (status, message) tuple.
    cur = res.get(nid)
    msg = msg or ""
    if cur is None or RANK[st] > RANK[cur[0]]:
        res[nid] = (st, msg if st != "passed" else "")
    elif RANK[st] == RANK[cur[0]] and st != "passed" and not cur[1] and msg:
        res[nid] = (st, msg)


def parse_ctrf(path, cfg):
    """report path -> {node_id: (status, failure_message)}"""
    res = {}
    try:
        doc = json.loads(Path(path).read_text())
        tests = (doc.get("results") or {}).get("tests") or []
        if not isinstance(tests, list):
            return res
    except Exception:
        return res
    for tc in tests:
        if not isinstance(tc, dict):
            continue
        nm = str(tc.get("name") or "").strip()
        if not nm:
            continue
        su_raw = tc.get("suite")
        if isinstance(su_raw, list) and su_raw:
            su = str(su_raw[0]).strip()
        elif isinstance(su_raw, str):
            su = su_raw.strip()
        else:
            su = ""
        nid = f"{su}.{nm}" if (cfg.get("node_id") == "suite.name" and su) else nm
        st = norm_status(tc.get("status"))
        msg = ""
        if st != "passed":
            msg = str(tc.get("message") or tc.get("trace") or "").strip()
        add(res, nid, st, msg)
    return res


def junit_status_msg(tc):
    st, msg = "passed", ""
    for ch in tc:
        tag = ch.tag.rsplit("}", 1)[-1]
        if tag in ("failure", "error"):
            parts = [(ch.get("message") or "").strip(), (ch.text or "").strip()]
            return "failed", "\n".join(p for p in parts if p).strip()
        if tag == "skipped":
            st = "skipped"
    return st, msg


def parse_junit(path, cfg):
    """report path -> {node_id: (status, failure_message)}"""
    res = {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return res
    for tc in root.iter("testcase"):
        cn = (tc.attrib.get("classname", "") or "").strip()
        nm = (tc.attrib.get("name", "") or "").strip()
        if not nm:
            continue
        nid = f"{cn}.{nm}" if cn else nm
        st, msg = junit_status_msg(tc)
        add(res, nid, st, msg)
    return res


PARSERS = {"ctrf": parse_ctrf, "junit": parse_junit}


def cmd_grade(argv):
    full = load_config()
    cfg = full.get("grade", {})
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)

    def load_ids(key):
        ids, seen = [], set()
        for line in full.get(key, []):
            s = str(line).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            ids.append(s)
        return ids

    p2p = load_ids("p2p_node_ids")
    f2p = load_ids("f2p_node_ids")

    def stats(fp, pp):
        total = len(f2p) + len(p2p)
        return {"f2p_total": len(f2p), "f2p_passed": fp,
                "p2p_total": len(p2p), "p2p_passed": pp,
                "f2p": fp / len(f2p) if f2p else 0.0,
                "p2p": pp / len(p2p) if p2p else 1.0,
                "partial": (fp + pp) / total if total else 0.0}

    parse = PARSERS[cfg.get("format", "ctrf")]
    seen = {}
    for rep in cfg["reports"]:
        for k, (st, msg) in parse(rep, cfg).items():
            add(seen, k, st, msg)

    def bucket(ids):
        p = f = 0
        rows = []
        for nid in ids:
            entry = seen.get(nid)
            if entry is None:
                rows.append({"name": nid, "status": "failed",
                             "message": "missing from report (test did not run "
                                        "or produced no result — see raw output)"})
                f += 1
            elif entry[0] == "passed":
                rows.append({"name": nid, "status": "passed"})
                p += 1
            else:
                rows.append({"name": nid, "status": entry[0], "message": entry[1]})
                f += 1
        return p, f, rows

    pp, pf, pr = bucket(p2p)
    fp, ff, fr = bucket(f2p)
    binary = 1 if (len(f2p) > 0 and ff == 0 and pf == 0) else 0

    def ctrf_test(t, b):
        d = {"name": f"[{b}] {t['name']}", "status": t["status"]}
        if t.get("message"):
            d["message"] = t["message"]
        return d

    ctrf = {"reportFormat": "CTRF", "specVersion": "1.0.0", "results": {
        "tool": {"name": cfg.get("tool_label", "unknown")},
        "summary": {"tests": len(p2p)+len(f2p), "passed": pp+fp,
                    "failed": pf+ff, "skipped": 0, "pending": 0, "other": 0},
        "tests": [ctrf_test(t, "p2p") for t in pr]
                + [ctrf_test(t, "f2p") for t in fr]}}
    if "task_baseline" in full:
        # Injection causality must see ALL observed tests, including damage an author omitted
        # from the whitelist. Missing IDs are not evidence of a behavioral failure at M.
        observed = []
        for nid, (status, message) in sorted(seen.items()):
            bucket_name = "f2p" if nid in f2p else "p2p" if nid in p2p else None
            row = {"name": f"[{bucket_name}] {nid}" if bucket_name else nid, "status": status}
            if message:
                row["message"] = message
            observed.append(row)
        for nid in sorted((set(f2p) | set(p2p)) - set(seen)):
            observed.append({"name": nid, "status": "skipped", "message": "missing from raw report"})
        ctrf["results"]["tests"] = observed
        ctrf["results"]["summary"] = {
            "tests": len(observed),
            **{key: sum(row["status"] == status for row in observed)
               for key, status in (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped"))},
            "pending": 0, "other": 0,
        }
    (VERIFIER_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2))

    out = {"reward": binary, **stats(fp, pp)}
    (VERIFIER_DIR / "reward.json").write_text(json.dumps(out))

    # Surface WHY each whitelisted test failed (lands in test-stdout.txt via the
    # harness capture). Reasons come from the report message; if absent, the raw
    # suite output catted by the frame is the fallback.
    fails = ([("p2p", t) for t in pr if t["status"] != "passed"]
             + [("f2p", t) for t in fr if t["status"] != "passed"])
    if fails:
        print(f"[verifier] ===== FAILURES ({len(fails)}) =====")
        for b, t in fails:
            print(f"[verifier] ✗ [{b}] {t['name']}")
            for line in (t.get("message") or "(no message)").splitlines():
                print(f"    {line}")
    print(f"P2P {pp}/{len(p2p)} pass {pf} fail; F2P {fp}/{len(f2p)} pass {ff} fail; "
          + f"PARTIAL {out['partial']}; BINARY {binary}")


def cmd_patch_paths(argv):
    for path in patch_paths(argv[0]):
        print(path)


def main():
    cmds = {"prepare": cmd_prepare, "grade": cmd_grade,
            "patch-paths": cmd_patch_paths}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(f"usage: grader.py {{{'|'.join(cmds)}}} [args]", file=sys.stderr)
        sys.exit(2)
    cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
