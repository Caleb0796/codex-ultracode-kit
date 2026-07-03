#!/usr/bin/env python3
"""check_package.py — validate the kit before install/publish.

Behavioral checks (not just "files load"), all offline — nothing spawns codex:
- every orchestrator script py_compiles
- SKILL.md frontmatter is explicit-only ($ultracode), fits the 8192-byte Codex
  loader cap, and points at the shipped reference.md
- skeptic.toml parses (tomllib on py>=3.11) with required keys, read-only sandbox,
  UNVERIFIABLE verdict; openai.yaml exists (parsed when PyYAML is available)
- codex_workflow exposes its documented API surface and env knobs, and the
  load-bearing engine behaviors hold: _extract_json escape/prose handling, budget
  guard + budget object math, lifetime agent cap, 4096-item cap, cache soundness
  (off for writers, off on falsy values), apply_diff no-op on empty diff
- codex_patterns deterministic guards behave (verification_shallow fail-closed +
  boundary matching, reingest_findings gates incl. completeness gaps)
Exits non-zero on any failure.
"""
import json
import os
import py_compile
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("== py_compile orchestrator ==")
for f in ("codex_workflow.py", "codex_patterns.py", "mcp_frontdoor.py"):
    p = os.path.join(ROOT, "orchestrator", f)
    try:
        py_compile.compile(p, doraise=True)
        check(f"compile {f}", True)
    except Exception as e:
        check(f"compile {f}", False, str(e))

print("== SKILL.md ==")
skill_p = os.path.join(ROOT, "skills", "ultracode", "SKILL.md")
skill = open(skill_p).read()
fm = skill.split("---")[1] if "---" in skill else ""
check("name: ultracode", "name: ultracode" in fm)
check("explicit-only ($ultracode) activation", "$ultracode" in fm and "Explicit-only" in fm)
check("does NOT advertise bare-ultra activation",
      'contains "ultra"' not in fm and "standalone word" not in fm,
      "frontmatter still triggers on bare 'ultra'")
check("SKILL.md size budget (<8192 bytes)", os.path.getsize(skill_p) < 8192,
      f"{os.path.getsize(skill_p)} bytes")
ref_p = os.path.join(ROOT, "skills", "ultracode", "reference.md")
check("reference.md ships next to SKILL.md", os.path.isfile(ref_p) and os.path.getsize(ref_p) > 0)
check("SKILL.md points to reference.md", "reference.md" in skill)

print("== skeptic.toml ==")
sk_p = os.path.join(ROOT, "agents", "skeptic.toml")
try:
    import tomllib
    t = tomllib.load(open(sk_p, "rb"))
    check("skeptic.toml parses with required keys",
          {"name", "description", "developer_instructions"} <= set(t))
    check("name = skeptic", t.get("name") == "skeptic")
    check("sandbox_mode = read-only", t.get("sandbox_mode") == "read-only")
    check("UNVERIFIABLE verdict present", "UNVERIFIABLE" in t.get("developer_instructions", ""))
except ImportError:  # py<3.11 — fall back to substring checks
    print("  SKIP  tomllib unavailable — substring checks only")
    sk = open(sk_p).read()
    check("name = skeptic", 'name = "skeptic"' in sk)
    check("sandbox_mode = read-only", 'sandbox_mode = "read-only"' in sk)
    check("UNVERIFIABLE verdict present", "UNVERIFIABLE" in sk)

print("== openai.yaml ==")
oy_p = os.path.join(ROOT, "skills", "ultracode", "agents", "openai.yaml")
check("openai.yaml exists and is non-empty", os.path.isfile(oy_p) and os.path.getsize(oy_p) > 0)
try:
    import yaml
    check("openai.yaml parses", yaml.safe_load(open(oy_p)) is not None)
except ImportError:
    print("  SKIP  PyYAML unavailable — existence check only")

print("== codex_workflow engine (offline) ==")
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
try:
    import codex_workflow as wf
    for name in ("agent", "parallel", "pipeline", "workflow", "meta", "log", "phase",
                 "phases", "tokens_used", "budget", "budget_exhausted",
                 "parse_budget_directive", "apply_diff", "cleanup_worktrees",
                 "start_run", "save_result", "write_ledger", "BudgetExceeded", "ResumeStale"):
        check(f"wf.{name} exists", hasattr(wf, name))
    src = open(os.path.join(ROOT, "orchestrator", "codex_workflow.py")).read()
    # match the actual env read, not the whole file — every knob name also appears
    # in the module docstring, which would make a bare substring check vacuous
    for knob in ("CODEX_WF_CODEX", "CODEX_WF_CONCURRENCY", "CODEX_WF_MODEL", "CODEX_WF_EFFORT",
                 "CODEX_WF_CWD", "CODEX_WF_TIMEOUT", "CODEX_WF_BUDGET", "CODEX_WF_MAX_AGENTS",
                 "CODEX_WF_RESUME", "CODEX_WF_RESUME_MODE", "CODEX_WF_WORKFLOWS",
                 "CODEX_WF_SCHEMA_REPAIR", "CODEX_WF_CACHE", "CODEX_WF_RUNS"):
        check(f"knob {knob} wired", f'os.environ.get("{knob}"' in src)
    # _extract_json: escaped quote + brace inside a string must not truncate
    check("_extract_json escaped-quote case",
          json.loads(wf._extract_json('prose {"a": "say \\" } hi", "b": 1} tail'))
          == {"a": 'say " } hi', "b": 1})
    check("_extract_json prose-wrapped case",
          json.loads(wf._extract_json('note {"x": [1, 2]} end')) == {"x": [1, 2]})
    check("_extract_json prose brace before JSON",
          json.loads(wf._extract_json('brace { in prose then {"a": 1} end')) == {"a": 1})
    check("_extract_json expect=object skips prose array",
          json.loads(wf._extract_json('I checked [1, 2] then {"issues": []}', expect="object"))
          == {"issues": []})
    check("_extract_json bails on pathological input", wf._extract_json("[" * 5000) == "[" * 5000)
    # budget guard trips BEFORE any subprocess (offline by construction)
    os.environ["CODEX_WF_BUDGET"] = "1"
    wf._tokens[0] = 10
    try:
        wf._run_once("x", None, ".", "read-only", None, None)
        check("budget guard raises BudgetExceeded", False)
    except wf.BudgetExceeded:
        check("budget guard raises BudgetExceeded", True)
    except Exception as e:
        check("budget guard raises BudgetExceeded", False, f"wrong exception: {e}")
    # budget object math (Workflow-tool parity)
    os.environ["CODEX_WF_BUDGET"] = "1000"
    wf._tokens[0] = 400
    check("budget object math",
          wf.budget.total == 1000 and wf.budget.spent() == 400 and wf.budget.remaining() == 600)
    os.environ["CODEX_WF_BUDGET"] = "0"
    wf._tokens[0] = 0
    check("budget.total is None when unset",
          wf.budget.total is None and wf.budget.remaining() == float("inf"))
    # budget_exhausted() must be true even for DIRECT agent() calls (not just batches)
    os.environ["CODEX_WF_BUDGET"] = "1"
    wf._tokens[0], wf._budget_hits[0] = 10, 0
    try:
        wf.agent("x", retries=0)
        check("direct agent() budget stop counted", False)
    except wf.BudgetExceeded:
        check("direct agent() budget stop counted", wf.budget_exhausted())
    finally:
        os.environ["CODEX_WF_BUDGET"] = "0"
        wf._tokens[0], wf._budget_hits[0], wf._agent_count[0] = 0, 0, 0
    # lifetime agent cap raises before spawning anything
    old_cap, old_count = wf.MAX_AGENTS, wf._agent_count[0]
    wf.MAX_AGENTS, wf._agent_count[0] = 1, 1
    try:
        wf.agent("x")
        check("lifetime agent cap raises", False)
    except RuntimeError:
        check("lifetime agent cap raises", True)
    except Exception as e:
        check("lifetime agent cap raises", False, f"wrong exception: {e}")
    finally:
        wf.MAX_AGENTS, wf._agent_count[0] = old_cap, old_count
    # 4096-item explicit error, never truncation
    try:
        wf.parallel([(lambda: None)] * 4097)
        check("parallel 4096-item cap", False)
    except ValueError:
        check("parallel 4096-item cap", True)
    # cache soundness: off for writer sandboxes, off on falsy values
    os.environ["CODEX_WF_CACHE"] = "1"
    check("cache skipped for writer sandbox", wf._cache_dir("workspace-write") is None)
    check("cache active for read-only", wf._cache_dir("read-only") is not None)
    os.environ["CODEX_WF_CACHE"] = "0"
    check("CODEX_WF_CACHE=0 disables caching", wf._cache_dir("read-only") is None)
    os.environ.pop("CODEX_WF_CACHE", None)
    check("apply_diff('') is a no-op", wf.apply_diff("") == {"ok": True, "conflicts": [], "error": ""})
    # budget directives (Workflow-tool '+500k' parity; standalone first/last token only)
    check("parse_budget_directive trailing '+500k'", wf.parse_budget_directive("audit the repo +500k") == 500_000)
    check("parse_budget_directive leading '+1.5m'", wf.parse_budget_directive("+1.5m go deep") == 1_500_000)
    check("parse_budget_directive bare large number", wf.parse_budget_directive("go +20000") == 20_000)
    check("parse_budget_directive ignores mid-prose '+2k'",
          wf.parse_budget_directive("the diff adds +2k lines to review") is None)
    check("parse_budget_directive ignores '+11'", wf.parse_budget_directive("fix +11") is None)
    check("parse_budget_directive ignores sub-1k '+0.001k'", wf.parse_budget_directive("+0.001k") is None)
    check("parse_budget_directive ignores C++14", wf.parse_budget_directive("port to C++14") is None)
    check("parse_budget_directive none", wf.parse_budget_directive("no directive here") is None)
    # determinism lint: AST-based — catches from-imports, never fires on comments
    check("lint catches 'from time import time'",
          wf._lint_nondeterminism("from time import time\ndef run(a):\n    return time()")
          == "time.time")
    check("lint ignores comments/strings",
          wf._lint_nondeterminism("# never call time.time() here\ndef run(a):\n    return 1")
          is None)
    # saved-workflow registry: builtins resolve; unknown names raise; nesting guarded
    check("builtin workflow 'fanout' resolves",
          wf._resolve_workflow("fanout").endswith(os.path.join("workflows", "fanout.py")))
    check("builtin workflow 'review' resolves",
          wf._resolve_workflow("review").endswith(os.path.join("workflows", "review.py")))
    try:
        wf._resolve_workflow("no-such-workflow")
        check("unknown workflow raises", False)
    except ValueError:
        check("unknown workflow raises", True)
    _tok = wf._wf_depth.set(1)
    try:
        wf.workflow("fanout", {"task": "x"})
        check("workflow() nesting guard raises", False)
    except RuntimeError:
        check("workflow() nesting guard raises", True)
    finally:
        wf._wf_depth.reset(_tok)
    # determinism guard: nondeterministic workflow source refused under resume
    _wdir = tempfile.mkdtemp()
    open(os.path.join(_wdir, "nondet.py"), "w").write(
        "import time\ndef run(args):\n    return time.time()\n")
    os.environ["CODEX_WF_RESUME"] = tempfile.mkdtemp()
    try:
        wf.workflow(os.path.join(_wdir, "nondet.py"))
        check("determinism guard refuses time.time under resume", False)
    except RuntimeError as e:
        check("determinism guard refuses time.time under resume", "resume replay" in str(e))
    finally:
        os.environ.pop("CODEX_WF_RESUME", None)
    # meta contract: recorded in run.json, phases auto-land in ledgers
    _runs_bak = wf.RUNS_ROOT
    wf.RUNS_ROOT = tempfile.mkdtemp()
    wf.meta(name="t-run", description="d", phases=["a", "b"])
    wf._phases[:] = []
    wf.phase("a")
    _rd = wf.start_run("meta test")
    check("meta lands in run.json",
          json.load(open(os.path.join(_rd, "run.json"))).get("meta", {}).get("name") == "t-run")
    wf.write_ledger(_rd, {"Scope": "x"})
    check("phases auto-recorded in ledger", "## Phases" in open(os.path.join(_rd, "ledger.md")).read())
    wf.RUNS_ROOT = _runs_bak
    wf._meta_state.clear()
    wf._phases[:] = []
    wf._phase[0] = None
except Exception as e:
    check("import/run codex_workflow", False, str(e))

print("== mcp_frontdoor (offline) ==")
try:
    import mcp_frontdoor as fd
    names = sorted(t["name"] for t in fd.TOOLS)
    check("front door advertises the four tools",
          names == ["ultracode_review", "ultracode_run", "ultracode_workflow",
                    "workflow_status"])
    check("front door declares resource subscriptions",
          fd.CAPABILITIES.get("resources", {}).get("subscribe") is True)
    check("unknown saved workflow -> tool error",
          "error" in fd._call_tool("ultracode_workflow", {"name": "no-such-wf"}))
    # budget grants are RELATIVE on the long-lived server: ceiling = meter + b, re-armable
    import codex_workflow as _wf2
    _env_bak = os.environ.pop("CODEX_WF_BUDGET", None)
    _tok_bak, _wf2._tokens[0] = _wf2._tokens[0], 0
    _ub_bak, fd._USER_BUDGET = fd._USER_BUDGET, False
    try:
        check("budget grant applies from task text",
              fd._apply_budget_directive("do it +5k") == 5000
              and os.environ["CODEX_WF_BUDGET"] == "5000")
        _wf2._tokens[0] = 600_000
        check("budget grant is relative to the meter and re-arms",
              fd._apply_budget_directive("again +5k") == 5000
              and os.environ["CODEX_WF_BUDGET"] == "605000")
        fd._USER_BUDGET = True
        check("user-configured ceiling never overridden",
              fd._apply_budget_directive("more +9k") is None)
    finally:
        fd._USER_BUDGET = _ub_bak
        _wf2._tokens[0] = _tok_bak
        os.environ.pop("CODEX_WF_BUDGET", None)
        if _env_bak is not None:
            os.environ["CODEX_WF_BUDGET"] = _env_bak
    # behavioral probe: the server must survive hostile stdin ('[]' batch) with runs alive
    import subprocess as _sp
    _p = _sp.Popen([sys.executable, os.path.join(ROOT, "orchestrator", "mcp_frontdoor.py")],
                   stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True)
    try:
        def _rpc(obj):
            _p.stdin.write(json.dumps(obj) + "\n")
            _p.stdin.flush()
            return json.loads(_p.stdout.readline())
        _init = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "v", "version": "0"}}})
        check("server probe: initialize", "result" in _init)
        _p.stdin.write("[]\n")
        _p.stdin.flush()
        _batch = json.loads(_p.stdout.readline())
        check("server probe: '[]' rejected, not fatal", _batch.get("error", {}).get("code") == -32600)
        _pong = _rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        check("server probe: alive after hostile input", _pong.get("result") == {})
    finally:
        _p.stdin.close()
        _p.wait(timeout=10)
    check("fan-out tools promise IMMEDIATE run_id (async contract)",
          all("IMMEDIATELY" in t["description"] for t in fd.TOOLS
              if t["name"].startswith("ultracode_")))
    check("workflow_status rejects path traversal",
          fd._status("../../etc").get("found") is False)
    check("workflow_status unknown id -> not found",
          fd._status("1234567-abcdef").get("found") is False)
    check("empty task rejected", fd._call_tool("ultracode_run", {"task": " "}) == {"error": "task is required"})
    check("empty dimensions rejected",
          "error" in fd._call_tool("ultracode_review", {"dimensions": []}))
except Exception as e:
    check("import/run mcp_frontdoor", False, str(e))

print("== codex_patterns guards ==")
try:
    import codex_patterns as cp
    for name in ("adversarial_verify", "judge_panel", "loop_until_dry", "multi_modal_sweep",
                 "completeness_critic", "verification_shallow", "reingest_findings",
                 "review_then_verify"):
        check(f"cp.{name} exists", hasattr(cp, name))
    check("verification_shallow flags py_compile", bool(cp.verification_shallow("py_compile x.py")))
    check("verification_shallow passes pytest", cp.verification_shallow("pytest -q") == "")
    check("verification_shallow credits path-invoked runner",
          cp.verification_shallow(".venv/bin/pytest -q") == "")
    check("verification_shallow rejects lookalike path",
          cp.verification_shallow("ls tests/pytest_fixtures") != "")
    check("verification_shallow fail-closed on unrecognized",
          cp.verification_shallow("echo all good") != "")
    check("verification_shallow warns on empty", cp.verification_shallow("") != "")

    def _gate(fixture):
        d = tempfile.mkdtemp()
        json.dump(fixture, open(os.path.join(d, "worker_07.json"), "w"))
        return cp.reingest_findings(d)

    g = _gate({"status": "fail", "summary": "x"})
    check("reingest catches non-name-matching fail", g["gate"] == "fail" and g["files"] == 1)
    check("reingest passes empty dir", cp.reingest_findings(tempfile.mkdtemp())
          == {"blocking": [], "files": 0, "gate": "pass"})
    check("reingest fails on unmet requirements",
          _gate({"all_verified": False,
                 "requirements": [{"requirement": "r1", "status": "inferred"}]})["gate"] == "fail")
    check("reingest fails on completeness gaps",
          _gate({"all_verified": True, "gaps": [{"task": "sweep the CLI docs"}]})["gate"] == "fail")
    check("reingest tolerates string gaps entries",
          _gate({"all_verified": True, "gaps": ["run the tests"]})["gate"] == "fail")
    try:
        cp.adversarial_verify("x", lenses=("correctness",), threshold=5)
        check("adversarial_verify rejects unreachable threshold", False)
    except ValueError:
        check("adversarial_verify rejects unreachable threshold", True)
    check("reingest fails on high-severity finding",
          _gate({"findings": [{"severity": "high", "claim": "x"}]})["gate"] == "fail")
    check("reingest fails on BARE confirmed finding (review_then_verify shape)",
          _gate({"file": "a.py", "claim": "off-by-one", "severity": "high",
                 "dim": "correctness"})["gate"] == "fail")
    # UNVERIFIABLE honesty: all-blocked votes must read 'unverified', never 'refuted'
    _orig_parallel = cp.wf.parallel
    try:
        cp.wf.parallel = lambda thunks: [{"real": False, "unverifiable": True, "why": "blocked"}]
        r = cp.adversarial_verify("claim x")
        check("all-UNVERIFIABLE reads unverified (not refuted)",
              r["survives"] is False and r["status"] == "unverified" and r["unverifiable"] == 1,
              str(r))
        cp.wf.parallel = lambda thunks: [{"real": False, "unverifiable": False, "why": "wrong"}]
        r = cp.adversarial_verify("claim x")
        check("live refutation still reads refuted", r["status"] == "refuted", str(r))
    finally:
        cp.wf.parallel = _orig_parallel
    d = tempfile.mkdtemp()
    open(os.path.join(d, "bad.json"), "w").write("{not json")
    check("reingest fails on invalid JSON result", cp.reingest_findings(d)["gate"] == "fail")
except Exception as e:
    check("import/run codex_patterns", False, str(e))

print()
if fails:
    print(f"VALIDATION FAILED: {len(fails)} check(s) — {', '.join(fails)}")
    sys.exit(1)
print("VALIDATION PASSED")
