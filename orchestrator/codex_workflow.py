#!/usr/bin/env python3
"""codex_workflow.py — a deterministic orchestrator for OpenAI Codex.

Mirrors Claude Code's Workflow tool. The model never decides how many agents
to spawn — THIS script does, deterministically. Each agent() is one
`codex exec` subprocess; parallel()/pipeline() control concurrency.

Primitives (Workflow-tool parity):
  agent(prompt, schema=, role=, isolation='worktree', retries=, label=, phase=)
                            -> str|dict|worktree-dict
  parallel(thunks)          -> list   barrier; concurrent; failed thunk -> None
  pipeline(items, *stages)  -> list   per-item chains, no barrier
  workflow(name|path, args) -> any    run a SAVED workflow (a .py defining run(args),
                                      optional META header); shares this process's
                                      caps, budget, and counters; one nesting level
  meta(name=, description=, phases=)  self-describing run header: recorded in
                                      run.json, phases auto-land in ledgers
  log(msg)                  -> None   progress line (mirrors Workflow log())
  phase(title)              -> None   progress grouping stamped into agent log tags
  budget                    -> obj    .total (None=off) / .spent() / .remaining()
  budget_exhausted()        -> bool   True once the ceiling stopped any agent
  parse_budget_directive(t) -> int|None   "+500k" in a user message -> 500000
  tokens_used()             -> int    rough budget meter (parsed from codex output)
  apply_diff(diff)          -> dict   apply a worktree agent's diff onto the base repo
  cleanup_worktrees()       -> None   remove worktrees created by isolation='worktree'

Budget patterns (Workflow-tool parity — the ceiling is enforced at launch; it stops
NEW agents but cannot claw back in-flight spend, so leave headroom):
  while wf.budget.total and wf.budget.remaining() > 50_000: ...      # loop-until-budget
  fleet = int(wf.budget.total // 100_000) if wf.budget.total else 5  # fleet scaling

Env knobs:
  CODEX_WF_CODEX        path to the codex executable (default "codex" from PATH) — set it
                        when the PATH shim is broken or endpoint security only allows a
                        specific build (e.g. /Applications/Codex.app/Contents/Resources/codex)
  CODEX_WF_CONCURRENCY  max concurrent codex exec processes (default min(16, cores-2))
  CODEX_WF_MODEL        model for agents (default gpt-5.5)
  CODEX_WF_EFFORT       reasoning effort (default medium; per-agent via agent(effort=))
  CODEX_WF_CWD          base working dir / git repo agents run in (default $PWD)
  CODEX_WF_TIMEOUT      per-agent timeout seconds (default 1800)
  CODEX_WF_BUDGET       soft token ceiling; stops launching new agents once exceeded (0 = off)
  CODEX_WF_MAX_AGENTS   lifetime agent cap, a runaway-loop backstop (default 1000; <=0 off)
  CODEX_WF_RESUME       run dir for the resume journal: read-only agent results replay by
                        content+occurrence; refuses across a changed HEAD unless
                        CODEX_WF_RESUME_FORCE=1. Writer/worktree agents always run live.
  CODEX_WF_RESUME_MODE  "prefix" (default): the first journal miss switches the REST of
                        the run live — an edited call invalidates everything after it
                        (spec semantics); "content": pure content+occurrence replay
  CODEX_WF_WORKFLOWS    saved-workflow dir (default $CWD/.codex/ultracode/workflows;
                        names also resolve against the kit's orchestrator/workflows/)
  CODEX_WF_SCHEMA_REPAIR "0" disables the one-shot repair call when an agent's output
                        fails schema validation (default on: cheaper than a full re-run)
  CODEX_WF_CACHE        legacy idempotent memoizer for READ-ONLY agents ("1" or a dir);
                        identical calls collapse to one result and results go stale after
                        any repo mutation — prefer CODEX_WF_RESUME. "0"/"false"/"off" = off.
  CODEX_WF_RUNS         run-ledger root (default $CWD/.codex/ultracode/runs)
"""
import ast
import collections
import contextvars
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CODEX_BIN = os.environ.get("CODEX_WF_CODEX", "codex")
MODEL = os.environ.get("CODEX_WF_MODEL", "gpt-5.5")
EFFORT = os.environ.get("CODEX_WF_EFFORT", "medium")
CONCURRENCY = int(os.environ.get("CODEX_WF_CONCURRENCY",
                                 str(max(1, min(16, (os.cpu_count() or 4) - 2)))))
CWD = os.environ.get("CODEX_WF_CWD", os.getcwd())
TIMEOUT = int(os.environ.get("CODEX_WF_TIMEOUT", "1800"))
RUNS_ROOT = os.environ.get("CODEX_WF_RUNS", os.path.join(CWD, ".codex", "ultracode", "runs"))
MAX_AGENTS = int(os.environ.get("CODEX_WF_MAX_AGENTS", "1000"))  # lifetime runaway backstop; <=0 disables
MAX_ITEMS = 4096  # per parallel()/pipeline() call: explicit error, never silent truncation

# Global cap on CONCURRENT codex exec processes. We do NOT use a single shared
# ThreadPoolExecutor: parallel() is called recursively (a pipeline stage calls
# parallel(), which calls adversarial_verify(), which calls parallel()), and a
# shared pool deadlocks when outer tasks hold every worker while waiting on inner
# tasks that can never get a slot. Instead parallel() spawns an ephemeral pool per
# call, and this semaphore (acquired around the actual subprocess) bounds real
# process concurrency regardless of nesting depth.
_sem = threading.Semaphore(CONCURRENCY)
_tok_lock = threading.Lock()
_tokens = [0]
_agent_count = [0]
_budget_hits = [0]
_worktrees = []
_phase = [None]
_phases = []
_occ_lock = threading.Lock()
_occ = collections.Counter()          # per-process occurrence index for resume replay
_resume_state = {"checked": False, "error": None,  # error is STICKY — see _resume_slot
                 "resuming": False}  # journal had prior entries at startup (vs fresh recording)
_resume_live = [False]                # prefix mode: True after the first journal miss
_meta_state = {}                      # meta() header, consumed by the next start_run()
_wf_depth = contextvars.ContextVar("cxwf_depth", default=0)  # workflow() nesting guard;
# parallel() propagates the caller's context into pool threads so the guard survives
# the parallel()->workflow() path (a plain threading.local would reset to 0 there)
_BUILTIN_WORKFLOWS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")


def _budget():
    """Read the token ceiling live (env can change between runs). 0 = unlimited."""
    return int(os.environ.get("CODEX_WF_BUDGET", "0"))


def _check_budget():
    """Reserve-before-spend: raise once the meter has crossed the ceiling. Called
    before every LIVE launch (journal/cache replays cost nothing and are allowed
    after exhaustion) AND before creating a worktree (so exhaustion can't leak
    empty worktrees). Increments the exhaustion counter here so budget_exhausted()
    is true even for direct agent() calls outside parallel()/pipeline()."""
    b = _budget()
    if b:
        with _tok_lock:
            if _tokens[0] >= b:
                _budget_hits[0] += 1
                raise BudgetExceeded(f"token budget {b} reached ({_tokens[0]} used)")

try:
    import jsonschema as _jsonschema
except ImportError:
    _jsonschema = None


class BudgetExceeded(Exception):
    pass


class ResumeStale(RuntimeError):
    """CODEX_WF_RESUME journal was recorded at a different HEAD. Never retried —
    the mismatch is deterministic, and retrying would silently skip the guard."""
    pass


class _Budget:
    """Workflow-tool budget parity. total is None when CODEX_WF_BUDGET is unset/0;
    remaining() is math.inf then — guard loops on budget.total, exactly like the
    Workflow tool's `while (budget.total && budget.remaining() > 50_000)`."""
    @property
    def total(self):
        return _budget() or None

    def spent(self):
        return tokens_used()

    def remaining(self):
        t = self.total
        return math.inf if t is None else max(0, t - self.spent())


budget = _Budget()


def budget_exhausted():
    """True once any agent was stopped by the CODEX_WF_BUDGET ceiling this run —
    lets ledgers report 'INCOMPLETE: budget exhausted' instead of a false clean pass."""
    with _tok_lock:
        return _budget_hits[0] > 0


def log(msg):
    """Progress narration, mirrors the Workflow tool's log(). Goes to stderr."""
    print(f"[wf] {msg}", file=sys.stderr, flush=True)


def phase(title):
    """Start a named phase (Workflow-tool phase() parity): logged as a divider and
    stamped into subsequent agent log tags."""
    _phase[0] = title
    _phases.append(title)
    log(f"=== {title} ===")


def phases():
    """Phase titles seen so far — write_ledger also records them automatically."""
    return list(_phases)


def meta(name=None, description=None, phases=None):
    """Self-describing run header (Workflow-tool `meta` parity). Call before
    start_run(): it is CONSUMED by the next start_run() (recorded into that run's
    run.json, never leaking into later runs). Declared phases show up in the ledger
    when run() never calls phase() itself. Saved workflows declare it as a
    module-level META dict."""
    _meta_state.clear()
    _meta_state.update({k: v for k, v in (("name", name), ("description", description),
                                          ("phases", phases)) if v is not None})


_BUDGET_TOKEN = re.compile(r"^\+(\d+(?:\.\d+)?)([kKmM])?$")


def parse_budget_directive(text):
    """Parse a '+500k'-style token target out of a user message (Workflow-tool budget
    directive parity): '+500k' -> 500000, '+1.5m' -> 1500000. To avoid firing on
    incidental prose ('the diff adds +2k lines'), only a STANDALONE first or last
    whitespace-token counts, and sub-1000 totals are ignored. Returns int or None."""
    toks = (text or "").split()
    if not toks:
        return None
    for tok in (toks[0], toks[-1]):
        m = _BUDGET_TOKEN.match(tok)
        if m:
            mult = {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
            val = int(float(m.group(1)) * mult)
            if val >= 1000:
                return val
    return None


_NONDET_EXACT = {"time.time", "time.time_ns", "time.monotonic", "datetime.datetime.now",
                 "datetime.datetime.utcnow", "datetime.date.today", "uuid.uuid1",
                 "uuid.uuid4", "os.urandom"}
_NONDET_PREFIX = ("random.", "secrets.")


def _lint_nondeterminism(src):
    """AST-based determinism guard (spec parity for the Date.now/Math.random ban):
    returns the offending dotted call name, or None. Resolves import aliases —
    `from time import time; time()` is caught — and, being AST-based, never
    false-positives on comments or strings."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None  # the module loader will surface the real error
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    def dotted(fn):
        parts = []
        while isinstance(fn, ast.Attribute):
            parts.append(fn.attr)
            fn = fn.value
        if isinstance(fn, ast.Name):
            parts.append(aliases.get(fn.id, fn.id))
            return ".".join(reversed(parts))
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name and (name in _NONDET_EXACT
                         or any(name.startswith(p) for p in _NONDET_PREFIX)):
                return name
    return None


def _resolve_workflow(name_or_path):
    """Resolve a saved-workflow name to its file: an explicit path wins, else
    <CODEX_WF_WORKFLOWS>/<name>.py, else the kit's builtin orchestrator/workflows/."""
    s = str(name_or_path)
    if os.path.sep in s or s.endswith(".py"):
        if os.path.isfile(s):
            return s
        raise ValueError(f"workflow file not found: {s}")
    wdir = os.environ.get("CODEX_WF_WORKFLOWS",
                          os.path.join(CWD, ".codex", "ultracode", "workflows"))
    cand = [os.path.join(wdir, f"{s}.py"), os.path.join(_BUILTIN_WORKFLOWS, f"{s}.py")]
    for p in cand:
        if os.path.isfile(p):
            return p
    raise ValueError(f"workflow {s!r} not found (looked in: {', '.join(cand)})")


def workflow(name_or_path, args=None):
    """Run a SAVED workflow (Workflow-tool workflow() parity): a .py file defining
    run(args) and optionally META = {'name','description','phases'}. It executes in
    THIS process, so it shares the concurrency semaphore, agent counter, and token
    budget by construction. One nesting level: a workflow calling workflow() raises.

    Determinism guard (spec parity): under CODEX_WF_RESUME, a workflow whose source
    CALLS time.time/datetime.now/random.*/uuid.uuid4/os.urandom (AST-resolved, so
    `from time import time` is caught and comments never false-positive) is
    REFUSED — nondeterministic prompt content breaks replay identity; pass
    timestamps/seeds in via args."""
    if _wf_depth.get() >= 1:
        raise RuntimeError("workflow() nesting is limited to one level (Workflow-tool parity)")
    path = _resolve_workflow(name_or_path)
    src = open(path).read()
    if os.environ.get("CODEX_WF_RESUME"):
        bad = _lint_nondeterminism(src)
        if bad:
            raise RuntimeError(
                f"workflow {os.path.basename(path)} calls {bad}() — nondeterministic "
                f"values break resume replay identity; pass them via args")
    spec = importlib.util.spec_from_file_location(
        "cxwf_saved_" + _cache_key([os.path.realpath(path)])[:12], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise ValueError(f"workflow {path} must define run(args)")
    m = getattr(mod, "META", None)
    if isinstance(m, dict):
        meta(m.get("name"), m.get("description"), m.get("phases"))
        log(f"workflow: {m.get('name') or os.path.basename(path)}"
            + (f" — {m['description']}" if m.get("description") else ""))
    token = _wf_depth.set(_wf_depth.get() + 1)
    try:
        return mod.run(args)
    finally:
        _wf_depth.reset(token)


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


def _balanced_end(text, start):
    """Index of the char that balances text[start] ('{' or '['), honoring JSON string
    and escape state; None if the blob never balances."""
    open_c, close_c = text[start], "}" if text[start] == "{" else "]"
    depth, instr, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i
    return None


_MAX_EXTRACT_CANDIDATES = 64  # each candidate is an O(n) scan — cap keeps pathological
                              # (truncated, never-balancing) output from stalling a slot


def _extract_json(text, expect=None):
    """Extract the first complete, PARSEABLE JSON object/array embedded in text
    (robust to prose around it). Scans candidate starts: a prose brace before the
    real JSON, or a balanced-but-invalid blob, moves the scan to the next '{'/'['.
    expect='object'/'array' restricts candidates to that root type, so a prose
    artifact like '[1, 2]' can't win over the intended object. Gives up after
    _MAX_EXTRACT_CANDIDATES starts and returns the original text, so the caller's
    json.loads raises cleanly instead of scanning O(n^2) forever."""
    opens = {"object": "{", "array": "["}.get(expect, "{[")
    pos = 0
    for _ in range(_MAX_EXTRACT_CANDIDATES):
        start = next((i for i in range(pos, len(text)) if text[i] in opens), None)
        if start is None:
            return text
        end = _balanced_end(text, start)
        if end is not None:
            cand = text[start:end + 1]
            try:
                json.loads(cand)
                return cand
            except json.JSONDecodeError:
                pass
        pos = start + 1
    return text


def _cache_key(parts):
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()


_CACHE_OFF = ("", "0", "false", "off", "no")


def _cache_dir(sandbox):
    """Resolve the legacy content-hash cache dir; None when disabled. The cache is
    only sound for read-only agents — a cached writer would replay its result text
    WITHOUT re-applying its edits (empty diff, silently) — so any other sandbox
    disables it. Falsy values ("0"/"false"/"off") disable rather than becoming a
    literal directory name."""
    if sandbox != "read-only":
        return None
    v = os.environ.get("CODEX_WF_CACHE", "")
    if v.strip().lower() in _CACHE_OFF:
        return None
    return v if v not in ("1", "true", "on") else os.path.join(RUNS_ROOT, "..", "cache")


def _atomic_write_json(path, obj):
    """Journal/cache writes must be atomic: the resume feature's core scenario is
    'the previous run died mid-flight', and a half-written entry would poison every
    future resume of that occurrence."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_replay(path, kind, tag):
    """Load a journal/cache entry; a corrupt (half-written) entry is a MISS that
    gets logged and overwritten by the live run, never a crash."""
    try:
        with open(path) as f:
            value = json.load(f)["value"]
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log(f"{tag}corrupt {kind} entry {os.path.basename(path)} ({e}) — running live")
        return None, False
    log(f"{tag}{kind} replay" if kind == "resume" else f"{tag}cache hit")
    return value, True


def _resume_slot(prompt, schema, cwd, sandbox, model, effort):
    """Resume journal (CODEX_WF_RESUME=<run dir>): content+occurrence replay for
    READ-ONLY agents. Unlike a global cache, the occurrence index keeps N identical
    redundant calls (independent skeptic votes) as N distinct journal entries, and
    an edited prompt hash-misses and runs live. Trade-off vs the Workflow tool's
    prefix journal: calls AFTER an edited one are not auto-invalidated — resume from
    the same HEAD or start a fresh run dir. Returns the journal path (or None).

    Called ONCE per agent() call (never per retry attempt — a retry must reuse its
    occurrence index, or the journaled result could never be found on resume and a
    later identical call would replay the wrong entry). The stale-HEAD refusal is
    STICKY: every subsequent call re-raises, so a swallowed first error can't let
    the rest of the run silently replay stale results."""
    jd = os.environ.get("CODEX_WF_RESUME")
    if not jd or sandbox != "read-only":
        return None
    with _occ_lock:
        if _resume_state["error"]:
            raise ResumeStale(_resume_state["error"])
        jdir = os.path.join(jd, "journal")
        os.makedirs(jdir, exist_ok=True)
        if not _resume_state["checked"]:
            _resume_state["checked"] = True
            # a journal with prior entries means we're RESUMING; a fresh dir is just
            # recording, where first-time misses are normal and must not flip prefix mode
            _resume_state["resuming"] = any(f.endswith(".json") and f != "meta.json"
                                            for f in os.listdir(jdir))
            head = subprocess.run(["git", "-C", CWD, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            meta_p = os.path.join(jdir, "meta.json")
            if os.path.exists(meta_p):
                try:
                    meta = json.load(open(meta_p))
                except Exception:
                    meta = {}
                if meta.get("head") and head and meta["head"] != head:
                    if os.environ.get("CODEX_WF_RESUME_FORCE") != "1":
                        _resume_state["error"] = (
                            f"CODEX_WF_RESUME journal was recorded at HEAD {meta['head'][:12]} but "
                            f"the repo is now at {head[:12]} — replayed results may be stale. Start "
                            f"a fresh run dir or set CODEX_WF_RESUME_FORCE=1 to override.")
                        raise ResumeStale(_resume_state["error"])
                    log(f"WARNING: resuming across HEADs ({meta['head'][:12]} -> {head[:12]}) — "
                        f"replayed results may be stale")
            else:
                _atomic_write_json(meta_p, {"head": head,
                                            "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        h = _cache_key([prompt, json.dumps(schema, sort_keys=True), model or MODEL,
                        effort or EFFORT, sandbox, os.path.realpath(cwd)])
        _occ[h] += 1
        return os.path.join(jdir, f"{h[:40]}-{_occ[h]}.json")


def _run_once(prompt, schema, cwd, sandbox, model, effort, tag="", jkey=None, _repair=False):
    # Replays first, budget second: a journal/cache hit spends zero new tokens, so an
    # exhausted budget must not discard results that were already paid for.
    # jkey is allocated ONCE in agent() (see _resume_slot) so retries reuse their slot.
    if jkey:
        prefix_mode = os.environ.get("CODEX_WF_RESUME_MODE", "prefix").strip().lower() != "content"
        if prefix_mode and _resume_live[0]:
            pass  # divergence already seen — prefix invalidated, run live (entry still written)
        elif os.path.exists(jkey):
            value, ok = _read_replay(jkey, "resume", tag)
            if ok:
                return value
        elif prefix_mode and _resume_state["resuming"]:
            # a miss while RESUMING = an edited/new call: spec semantics invalidate
            # everything after it, so the REST of the run goes live (conservative under
            # threads: wall-clock-after, never a stale replay). Misses during a fresh
            # recording run are normal and never flip this.
            with _occ_lock:
                if not _resume_live[0]:
                    _resume_live[0] = True
                    log("resume: divergence detected — prefix invalidated, the rest of "
                        "the run goes LIVE (CODEX_WF_RESUME_MODE=content for pure "
                        "content replay)")
    # Legacy content-hash cache (read-only agents only — see _cache_dir).
    cdir = _cache_dir(sandbox)
    ckey = None
    if cdir:
        os.makedirs(cdir, exist_ok=True)
        ckey = os.path.join(cdir, _cache_key([prompt, json.dumps(schema, sort_keys=True),
                                              model or MODEL, effort or EFFORT, sandbox,
                                              os.path.realpath(cwd)]) + ".json")
        if os.path.exists(ckey):
            value, ok = _read_replay(ckey, "cache", tag)
            if ok:
                return value
    # Soft budget: reserve-before-spend. The true cost is only known after the run,
    # so this is a soft cap — it stops LAUNCHING new agents once exceeded, it can't
    # claw back in-flight spend. Checked under the lock just before each launch.
    _check_budget()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.txt")
        logf = os.path.join(td, "log.txt")
        # -a never and -s are GLOBAL flags — must precede `exec` (codex rejects -a after exec).
        # mcp_servers={} disables MCP: faster startup AND restores --output-schema, which codex
        # silently ignores when an MCP server is active (openai/codex#15451).
        cmd = [
            CODEX_BIN, "-a", "never", "-s", sandbox,
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
        # Child stdin -> DEVNULL, never inherited: codex READS stdin, and inside the MCP
        # front door the inherited stdin IS the client's JSON-RPC pipe — a worker would
        # steal protocol bytes mid-run and corrupt the session (found by live e2e).
        # _sem bounds the number of concurrent codex processes across all nesting levels.
        with open(logf, "w") as lf, _sem:
            subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                           stdout=lf, stderr=lf, timeout=TIMEOUT)
        # rough token accounting from the codex footer. Require a leading digit (a bare
        # comma can't match) and take the LAST occurrence — the genuine footer is final,
        # so 'total 999999' in agent prose earlier in the log can't inflate the meter.
        try:
            with open(logf) as f:
                blob = f.read()
            toks = re.findall(r"(?:tokens used|total)[\s=:]*(\d[\d,]*)", blob, re.I)
            if toks:
                with _tok_lock:
                    _tokens[0] += int(toks[-1].replace(",", ""))
        except Exception:
            pass
        with open(out) as f:
            text = f.read().strip()
        if schema is None:
            result = text
        else:
            # --output-schema yields pure JSON; parse directly, fall back to balanced
            # extraction constrained to the schema's root type (so a prose artifact
            # like '[1, 2]' can't win over the intended object).
            expect = schema.get("type") if isinstance(schema, dict) else None
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                try:
                    result = json.loads(_extract_json(text, expect=expect))
                except json.JSONDecodeError:
                    raise ValueError(f"{tag}agent returned no parseable JSON "
                                     f"(first 200 chars): {text[:200]!r}")
            try:
                _validate_result(result, schema, expect, tag)
            except Exception as ve:
                # tool-call-layer retry parity: one cheap REPAIR pass (feed the invalid
                # output + error back) instead of re-running the whole expensive agent;
                # a failed repair raises into agent()'s normal retry loop
                if _repair or os.environ.get("CODEX_WF_SCHEMA_REPAIR",
                                             "1").strip().lower() in _CACHE_OFF:
                    raise
                log(f"{tag}schema validation failed — one repair pass ({str(ve)[:120]})")
                result = _run_once(
                    "Your previous output failed JSON-schema validation.\n"
                    f"Validation error: {str(ve)[:400]}\n"
                    f"Required schema: {json.dumps(schema)}\n"
                    f"Your previous output: {json.dumps(result)[:2000]}\n"
                    "Return ONLY the corrected JSON conforming to the schema — no prose.",
                    schema, cwd, "read-only", model, effort,
                    tag=tag + "repair: ", _repair=True)
        for path in (ckey, jkey):
            if path:
                _atomic_write_json(path, {"value": result})
        return result


def _validate_result(result, schema, expect, tag):
    """Root-type check (works without jsonschema — a wrong-typed prose fragment must
    never be silently returned) plus full jsonschema validation when available."""
    if expect == "object" and not isinstance(result, dict):
        raise ValueError(f"{tag}schema expects an object, agent returned "
                         f"{type(result).__name__}")
    if expect == "array" and not isinstance(result, list):
        raise ValueError(f"{tag}schema expects an array, agent returned "
                         f"{type(result).__name__}")
    if _jsonschema is not None:
        _jsonschema.validate(result, schema)


# Subagent output is consumed by THIS script, not read by a human (Workflow-tool parity).
_RETURN_RULE = ("Your final message is consumed verbatim by a program as your return value. "
                "Output raw data only — no preamble, no prose framing, no markdown headers "
                "unless asked.\n\n")

# Writer agents edit files only: in exec mode under an on-request/cloud policy, shell
# commands (tests/builds) are auto-REJECTED ("approval not supported in exec mode"), so
# verification is the orchestrator's job after collecting diffs.
_WRITER_RULE = ("Make your changes by editing files only. Do NOT run shell commands "
                "(tests, builds, installers) — they are rejected in this mode; the "
                "orchestrator runs verification after collecting your diff.\n\n")


def agent(prompt, schema=None, cwd=None, sandbox="read-only",
          model=None, effort=None, role=None, isolation=None, retries=1,
          label=None, phase=None):
    """One codex exec subagent.
      schema     JSON Schema dict -> returns a validated dict (else returns text).
      role       short role framing prepended to the prompt (explorer/skeptic/etc).
      effort     per-agent model_reasoning_effort override (default CODEX_WF_EFFORT).
      isolation  'worktree' -> run in a fresh git worktree off cwd (sandbox forced
                 workspace-write); returns {'result','worktree','branch','diff'} so
                 parallel writers never collide. A no-change run is torn down
                 immediately and returns worktree=None (Workflow-tool parity).
      retries    extra attempts on nonzero exit / parse failure (default 1).
                 BudgetExceeded is never retried — exhaustion is deterministic.
      label      display-only tag stamped into this agent's log lines.
      phase      display-only phase override (defaults to the current phase() title).
    Raises after exhausting retries; parallel()/pipeline() convert that to None."""
    with _tok_lock:
        _agent_count[0] += 1
        if MAX_AGENTS > 0 and _agent_count[0] > MAX_AGENTS:
            raise RuntimeError(f"lifetime agent cap ({MAX_AGENTS}) reached — runaway loop? "
                               f"(override: CODEX_WF_MAX_AGENTS)")
    if role:
        prompt = f"You are acting as: {role}.\n\n{prompt}"
    prompt = _RETURN_RULE + prompt
    ph = phase or _phase[0]
    tag = (f"[{ph}: {label}] " if ph and label
           else f"[{label}] " if label
           else f"[{ph}] " if ph else "")
    # Journal slot allocated ONCE per agent() call: retries must reuse the same
    # occurrence index, or the recorded result could never be replayed on resume
    # and a later identical call would replay this call's entry as its own.
    jkey = None
    if isolation != "worktree":
        jkey = _resume_slot(prompt, schema, cwd or CWD, sandbox, model, effort)
    last = None
    for _ in range(retries + 1):
        try:
            if isolation == "worktree":
                return _agent_worktree(prompt, schema, model, effort, base=cwd or CWD, tag=tag)
            return _run_once(prompt, schema, cwd or CWD, sandbox, model, effort, tag=tag,
                             jkey=jkey)
        except (BudgetExceeded, ResumeStale):
            raise  # deterministic — retrying cannot help; parallel() reports it distinctly
        except Exception as e:
            last = e
    log(f"{tag}failed after {retries + 1} attempt(s): {last}")
    raise last


def _agent_worktree(prompt, schema, model, effort, base=None, tag=""):
    _check_budget()  # before creating the worktree, so exhaustion can't leak empty trees
    base = base or CWD
    wt = tempfile.mkdtemp(prefix="cxwt-")
    branch = "cxwf/" + os.path.basename(wt)
    add = subprocess.run(["git", "-C", base, "worktree", "add", "-q", "-b", branch, wt, "HEAD"],
                         capture_output=True, text=True)
    if add.returncode != 0:
        shutil.rmtree(wt, ignore_errors=True)  # don't leak the empty tempdir
        raise RuntimeError(f"git worktree add failed (is {base} a git repo with a HEAD?): "
                           f"{add.stderr.strip()[:200]}")
    try:
        result = _run_once(_WRITER_RULE + prompt, schema, wt, "workspace-write", model, effort, tag=tag)
        subprocess.run(["git", "-C", wt, "add", "-A"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # --binary: without it, binary changes become an unappliable "Binary files
        # differ" placeholder and apply_diff would fail on them
        diff = subprocess.run(["git", "-C", wt, "diff", "--cached", "--binary"],
                              capture_output=True, text=True).stdout
        if not diff.strip():
            # agent changed nothing — remove the worktree immediately (Workflow-tool parity)
            subprocess.run(["git", "-C", base, "worktree", "remove", "--force", wt],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", base, "branch", "-D", branch],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"result": result, "worktree": None, "branch": None, "diff": ""}
        _worktrees.append((base, wt, branch))  # register only on success, for collection
        return {"result": result, "worktree": wt, "branch": branch, "diff": diff}
    except Exception:
        # leak-proof: tear down this worktree+branch before the caller's retry makes a new one
        subprocess.run(["git", "-C", base, "worktree", "remove", "--force", wt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", base, "branch", "-D", branch],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise


def apply_diff(diff, base=None):
    """Apply a diff returned by agent(isolation='worktree') onto the base repo.
    Call BEFORE cleanup_worktrees() (3-way blobs are guaranteed reachable then).
    An empty diff (the worker made no changes) is a successful no-op — git apply
    would otherwise exit nonzero on empty input. --3way leaves conflict markers in
    files and reports the conflicted paths instead of failing outright.
    Returns {'ok': bool, 'conflicts': [paths], 'error': str}."""
    base = base or CWD
    if not (diff or "").strip():
        return {"ok": True, "conflicts": [], "error": ""}
    p = subprocess.run(["git", "-C", base, "apply", "--3way"], input=diff,
                       capture_output=True, text=True)
    if p.returncode == 0:
        return {"ok": True, "conflicts": [], "error": ""}
    conf = subprocess.run(["git", "-C", base, "diff", "--name-only", "--diff-filter=U"],
                          capture_output=True, text=True).stdout.split()
    return {"ok": False, "conflicts": conf, "error": p.stderr.strip()[:400]}


def cleanup_worktrees():
    """Remove worktrees AND their branches created by isolation='worktree'.
    Failure-path and no-change cleanup is automatic; SUCCESS-path cleanup is yours —
    call this after you've collected/applied the diffs (see apply_diff)."""
    for base, wt, branch in _worktrees:
        subprocess.run(["git", "-C", base, "worktree", "remove", "--force", wt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", base, "branch", "-D", branch],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _worktrees.clear()


def parallel(thunks):
    """Run thunks (0-arg callables) concurrently. Promise.allSettled semantics: a thunk
    that raises yields None, never aborts the batch. Uses an EPHEMERAL pool per call so
    nested parallel() (a pipeline stage calling parallel(), which calls adversarial_verify(),
    which calls parallel()) can't deadlock on a shared pool's exhausted slots; real process
    concurrency is bounded by the module-global _sem around each subprocess instead.
    Budget exhaustion is reported distinctly (never mistaken for agent failures)."""
    thunks = list(thunks)
    if len(thunks) > MAX_ITEMS:
        raise ValueError(f"parallel(): {len(thunks)} thunks exceeds the {MAX_ITEMS}-item cap")
    if not thunks:
        return []
    results, stopped = [], 0
    with ThreadPoolExecutor(max_workers=max(1, min(CONCURRENCY * 2, len(thunks)))) as pool:
        # propagate the caller's context (one copy per thunk) so contextvars like the
        # workflow() nesting guard survive into pool threads
        for f in [pool.submit(contextvars.copy_context().run, t) for t in thunks]:
            try:
                results.append(f.result())
            except BudgetExceeded:
                results.append(None)
                stopped += 1
            except Exception as e:
                results.append(None)
                log(f"agent failed: {e}")
    if stopped:
        # _check_budget already counted each hit; this is just the batch-level summary
        log(f"BUDGET EXHAUSTED — {stopped} of {len(thunks)} thunks did not run")
    return results


def pipeline(items, *stages):
    """Each item flows through all stages independently (no barrier between stages).
    Stage signature: stage(prev_result, original_item, index). A stage that raises
    drops that item to None and skips its remaining stages; budget exhaustion is
    logged distinctly."""
    items = list(items)
    if len(items) > MAX_ITEMS:
        raise ValueError(f"pipeline(): {len(items)} items exceeds the {MAX_ITEMS}-item cap")

    def run_chain(item, idx):
        val = item
        for stage in stages:
            try:
                val = stage(val, item, idx)
            except BudgetExceeded:
                log(f"item {idx} skipped: budget exhausted")
                return None
            except Exception as e:
                log(f"item {idx} dropped: {e}")
                return None
        return val
    return parallel([(lambda it=it, i=i: run_chain(it, i)) for i, it in enumerate(items)])


# --- Durable run ledger: a human-auditable trail on disk -----------------------------
# A real process orchestrator should leave the same audit trail an artifact-scaffolding
# tool does. start_run() makes runs/<ts-sha-pid-rand>/; write_ledger() rewrites ledger.md
# idempotently; save_result() drops a worker result for reingest_findings() to gate on.
# review_then_verify() wires all three together when passed run_dir=.


def start_run(task, mode="auto"):
    """Create runs/<timestamp>-<sha1(task)[:8]>-<pid>-<rand>/ with run.json + results/.
    The pid+random suffix prevents collisions between concurrent same-second runs of the
    same task (sha1(task) alone is NOT unique). Returns the dir."""
    rid = "-".join([time.strftime("%Y%m%d-%H%M%S"), hashlib.sha1(task.encode()).hexdigest()[:8],
                    str(os.getpid()), os.urandom(2).hex()])
    d = os.path.join(RUNS_ROOT, rid)
    os.makedirs(os.path.join(d, "results"), exist_ok=True)
    rec = {"run_id": rid, "task": task, "mode": mode,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if _meta_state:
        rec["meta"] = dict(_meta_state)  # meta() is CONSUMED by the run it describes
        _meta_state.clear()              # — it must never leak into a later run
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump(rec, f, indent=2)
    # a new run starts a fresh phase trail (phase()/meta() are process-scoped: in a
    # multi-run process like the MCP front door, CONCURRENT runs still interleave —
    # sequential runs no longer inherit each other's trail)
    _phases[:] = []
    _phase[0] = None
    log(f"run dir: {d}")
    return d


def save_result(run_dir, item_id, result):
    """Persist one worker/skeptic result as results/<item_id>.json (for reingest_findings).
    item_id is sanitized so a slash/`..` can't escape the results/ dir."""
    safe = re.sub(r"[^\w.-]", "_", str(item_id)).strip(".") or "item"
    p = os.path.join(run_dir, "results", f"{safe}.json")
    with open(p, "w") as f:
        json.dump(result, f, indent=2)
    return p


def write_ledger(run_dir, sections):
    """Idempotently (re)write ledger.md from {heading: body} — overwrites, never appends
    duplicates. Conventional headings: Route, Scope, Phases, Coverage, Findings, Changes,
    Verification, Adversarial gate, Unresolved risks, Next action. Phase() titles are
    recorded automatically when the caller doesn't supply a Phases section; if no
    phase() ran, the run's DECLARED meta phases (run.json) stand in."""
    if "Phases" not in sections:
        trail = list(_phases)
        if not trail:  # fall back to the phases the run DECLARED via meta()/META
            try:
                with open(os.path.join(run_dir, "run.json")) as f:
                    trail = json.load(f).get("meta", {}).get("phases") or []
            except (OSError, json.JSONDecodeError):
                trail = []
            trail = trail or list(_meta_state.get("phases") or [])
        if trail:
            sections = dict(sections)
            sections["Phases"] = " → ".join(str(t) for t in trail)
    order = ["Route", "Scope", "Phases", "Coverage", "Findings", "Changes", "Verification",
             "Adversarial gate", "Unresolved risks", "Next action"]
    keys = [k for k in order if k in sections] + [k for k in sections if k not in order]
    body = "# Ultracode run ledger\n\n" + "\n\n".join(
        f"## {k}\n\n{sections[k]}" for k in keys)
    with open(os.path.join(run_dir, "ledger.md"), "w") as f:
        f.write(body + "\n")
    return os.path.join(run_dir, "ledger.md")
