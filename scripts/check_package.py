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
for f in ("codex_workflow.py", "codex_patterns.py"):
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
    for name in ("agent", "parallel", "pipeline", "log", "phase", "phases", "tokens_used",
                 "budget", "budget_exhausted", "apply_diff", "cleanup_worktrees",
                 "start_run", "save_result", "write_ledger", "BudgetExceeded", "ResumeStale"):
        check(f"wf.{name} exists", hasattr(wf, name))
    src = open(os.path.join(ROOT, "orchestrator", "codex_workflow.py")).read()
    # match the actual env read, not the whole file — every knob name also appears
    # in the module docstring, which would make a bare substring check vacuous
    for knob in ("CODEX_WF_CONCURRENCY", "CODEX_WF_MODEL", "CODEX_WF_EFFORT", "CODEX_WF_CWD",
                 "CODEX_WF_TIMEOUT", "CODEX_WF_BUDGET", "CODEX_WF_MAX_AGENTS",
                 "CODEX_WF_RESUME", "CODEX_WF_CACHE", "CODEX_WF_RUNS"):
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
except Exception as e:
    check("import/run codex_workflow", False, str(e))

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
