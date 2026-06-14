#!/usr/bin/env python3
"""codex_workflow.py — a deterministic orchestrator for OpenAI Codex.

Mirrors Claude Code's Workflow tool. The model never decides how many agents
to spawn — THIS script does, deterministically. Each agent() is one
`codex exec` subprocess; parallel()/pipeline() control concurrency.

Primitives (Workflow-tool parity):
  agent(prompt, schema=, role=, isolation='worktree', retries=) -> str|dict|worktree-dict
  parallel(thunks)          -> list   barrier; concurrent; failed thunk -> None
  pipeline(items, *stages)  -> list   per-item chains, no barrier
  log(msg)                  -> None   progress line (mirrors Workflow log())
  tokens_used()             -> int    rough budget meter (parsed from codex output)
  cleanup_worktrees()       -> None   remove worktrees created by isolation='worktree'

Env knobs:
  CODEX_WF_CONCURRENCY  max concurrent codex exec processes (default 8)
  CODEX_WF_MODEL        model for agents (default gpt-5.5)
  CODEX_WF_EFFORT       reasoning effort (default medium)
  CODEX_WF_CWD          base working dir / git repo agents run in (default $PWD)
  CODEX_WF_TIMEOUT      per-agent timeout seconds (default 1800)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

MODEL = os.environ.get("CODEX_WF_MODEL", "gpt-5.5")
EFFORT = os.environ.get("CODEX_WF_EFFORT", "medium")
CONCURRENCY = int(os.environ.get("CODEX_WF_CONCURRENCY", "8"))
CWD = os.environ.get("CODEX_WF_CWD", os.getcwd())
TIMEOUT = int(os.environ.get("CODEX_WF_TIMEOUT", "1800"))
BUDGET = int(os.environ.get("CODEX_WF_BUDGET", "0"))  # token ceiling; 0 = unlimited

_pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
_tok_lock = threading.Lock()
_tokens = [0]
_worktrees = []

try:
    import jsonschema as _jsonschema
except ImportError:
    _jsonschema = None


class BudgetExceeded(Exception):
    pass


def log(msg):
    """Progress narration, mirrors the Workflow tool's log(). Goes to stderr."""
    print(f"[wf] {msg}", file=sys.stderr, flush=True)


def tokens_used():
    """Approximate total tokens across all agents this run (parsed from codex output)."""
    with _tok_lock:
        return _tokens[0]


def _strict(node):
    """OpenAI structured output is strict: every object needs additionalProperties:false
    and all its properties listed as required. Walk the schema and enforce that."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node.setdefault("additionalProperties", False)
            node["required"] = list(node["properties"].keys())
        for v in node.values():
            _strict(v)
    elif isinstance(node, list):
        for v in node:
            _strict(v)
    return node


def _extract_json(text):
    """Balanced-brace extraction of the first complete JSON object/array in text
    (robust to prose around it — unlike a greedy {.*} regex which merges blobs)."""
    start = next((i for i, c in enumerate(text) if c in "{["), None)
    if start is None:
        return text
    open_c, close_c = text[start], "}" if text[start] == "{" else "]"
    depth, instr, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if instr:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                instr = False
        elif c == '"':
            instr = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _run_once(prompt, schema, cwd, sandbox, model, effort):
    if BUDGET and tokens_used() >= BUDGET:
        raise BudgetExceeded(f"token budget {BUDGET} reached ({tokens_used()} used)")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.txt")
        logf = os.path.join(td, "log.txt")
        # -a never and -s are GLOBAL flags — must precede `exec` (codex rejects -a after exec).
        # mcp_servers={} disables MCP: faster startup AND restores --output-schema, which codex
        # silently ignores when an MCP server is active (openai/codex#15451).
        cmd = [
            "codex", "-a", "never", "-s", sandbox,
            "exec", "--skip-git-repo-check",
            "-c", f"model_reasoning_effort={effort or EFFORT}",
            "-c", "notify=[]",
            "-c", "mcp_servers={}",
            "-m", model or MODEL,
            "--cd", cwd,
            "-o", out,
        ]
        if schema is not None:
            sf = os.path.join(td, "schema.json")
            with open(sf, "w") as f:
                json.dump(_strict(json.loads(json.dumps(schema))), f)
            cmd += ["--output-schema", sf]
        cmd.append(prompt)
        # Child output -> FILE, never PIPE: codex emits enough text that pipe buffers (~64KB)
        # fill under concurrency and subprocess.run deadlocks. The result comes from -o anyway.
        with open(logf, "w") as lf:
            subprocess.run(cmd, check=True, stdout=lf, stderr=lf, timeout=TIMEOUT)
        # rough token accounting from the codex console footer ("tokens used  N")
        try:
            toks = re.findall(r"tokens used[\s:]*([\d,]+)", open(logf).read())
            if toks:
                with _tok_lock:
                    _tokens[0] += int(toks[-1].replace(",", ""))
        except Exception:
            pass
        text = open(out).read().strip()
        if schema is None:
            return text
        # --output-schema yields pure JSON; parse directly, fall back to balanced extraction.
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = json.loads(_extract_json(text))
        if _jsonschema is not None:
            _jsonschema.validate(obj, schema)  # raises -> caller's retry loop re-runs
        return obj


def agent(prompt, schema=None, cwd=None, sandbox="read-only",
          model=None, effort=None, role=None, isolation=None, retries=1):
    """One codex exec subagent.
      schema     JSON Schema dict -> returns a validated dict (else returns text).
      role       short role framing prepended to the prompt (explorer/skeptic/etc).
      isolation  'worktree' -> run in a fresh git worktree (sandbox forced workspace-write);
                 returns {'result','worktree','branch','diff'} so parallel writers never collide.
      retries    extra attempts on nonzero exit / parse failure (default 1).
    Raises after exhausting retries; parallel()/pipeline() convert that to None."""
    if role:
        prompt = f"You are acting as: {role}.\n\n{prompt}"
    last = None
    for _ in range(retries + 1):
        try:
            if isolation == "worktree":
                return _agent_worktree(prompt, schema, model, effort)
            return _run_once(prompt, schema, cwd or CWD, sandbox, model, effort)
        except Exception as e:
            last = e
    raise last


# Writer agents edit files only: in exec mode under an on-request/cloud policy, shell
# commands (tests/builds) are auto-REJECTED ("approval not supported in exec mode"), so
# verification is the orchestrator's job after collecting diffs.
_WRITER_RULE = ("Make your changes by editing files only. Do NOT run shell commands "
                "(tests, builds, installers) — they are rejected in this mode; the "
                "orchestrator runs verification after collecting your diff.\n\n")


def _agent_worktree(prompt, schema, model, effort, base=None):
    base = base or CWD
    wt = tempfile.mkdtemp(prefix="cxwt-")
    branch = "cxwf/" + os.path.basename(wt)
    subprocess.run(["git", "-C", base, "worktree", "add", "-q", "-b", branch, wt, "HEAD"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = _run_once(_WRITER_RULE + prompt, schema, wt, "workspace-write", model, effort)
        subprocess.run(["git", "-C", wt, "add", "-A"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff = subprocess.run(["git", "-C", wt, "diff", "--cached"], capture_output=True, text=True).stdout
        _worktrees.append((base, wt, branch))  # register only on success, for collection
        return {"result": result, "worktree": wt, "branch": branch, "diff": diff}
    except Exception:
        # leak-proof: tear down this worktree+branch before the caller's retry makes a new one
        subprocess.run(["git", "-C", base, "worktree", "remove", "--force", wt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", base, "branch", "-D", branch],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise


def cleanup_worktrees():
    """Remove worktrees AND their branches created by isolation='worktree'.
    Call after you've collected/applied the diffs."""
    for base, wt, branch in _worktrees:
        subprocess.run(["git", "-C", base, "worktree", "remove", "--force", wt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", base, "branch", "-D", branch],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _worktrees.clear()


def parallel(thunks):
    """Run thunks (0-arg callables) concurrently, capped at CONCURRENCY.
    Promise.allSettled semantics: a thunk that raises yields None, never aborts the batch."""
    futs = [_pool.submit(t) for t in thunks]
    results = []
    for f in futs:
        try:
            results.append(f.result())
        except Exception as e:
            results.append(None)
            log(f"agent failed: {e}")
    return results


def pipeline(items, *stages):
    """Each item flows through all stages independently (no barrier between stages).
    Stage signature: stage(prev_result, original_item, index). A stage that raises
    drops that item to None and skips its remaining stages."""
    def run_chain(item, idx):
        val = item
        for stage in stages:
            try:
                val = stage(val, item, idx)
            except Exception as e:
                log(f"item {idx} dropped: {e}")
                return None
        return val
    return parallel([(lambda it=it, i=i: run_chain(it, i)) for i, it in enumerate(items)])


# --- Durable run ledger: a human-auditable trail on disk -----------------------------
# A real process orchestrator should leave the same audit trail an artifact-scaffolding
# tool does. start_run() makes runs/<ts-sha>/; write_ledger() rewrites ledger.md
# idempotently; save_result() drops a worker result for reingest_findings() to gate on.
import hashlib
import time

RUNS_ROOT = os.environ.get("CODEX_WF_RUNS", os.path.join(CWD, ".codex", "ultracode", "runs"))


def start_run(task, mode="auto"):
    """Create runs/<timestamp>-<sha1(task)[:8]>/ with run.json + results/. Returns the dir."""
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(task.encode()).hexdigest()[:8]
    d = os.path.join(RUNS_ROOT, rid)
    os.makedirs(os.path.join(d, "results"), exist_ok=True)
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump({"run_id": rid, "task": task, "mode": mode,
                   "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=2)
    log(f"run dir: {d}")
    return d


def save_result(run_dir, item_id, result):
    """Persist one worker/skeptic result as results/<item_id>.json (for reingest_findings)."""
    p = os.path.join(run_dir, "results", f"{item_id}.json")
    with open(p, "w") as f:
        json.dump(result, f, indent=2)
    return p


def write_ledger(run_dir, sections):
    """Idempotently (re)write ledger.md from {heading: body} — overwrites, never appends
    duplicates. Conventional headings: Route, Scope, Findings, Changes, Verification,
    Adversarial gate, Unresolved risks, Next action."""
    order = ["Route", "Scope", "Findings", "Changes", "Verification",
             "Adversarial gate", "Unresolved risks", "Next action"]
    keys = [k for k in order if k in sections] + [k for k in sections if k not in order]
    body = "# Ultracode run ledger\n\n" + "\n\n".join(
        f"## {k}\n\n{sections[k]}" for k in keys)
    with open(os.path.join(run_dir, "ledger.md"), "w") as f:
        f.write(body + "\n")
    return os.path.join(run_dir, "ledger.md")
