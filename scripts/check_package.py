#!/usr/bin/env python3
"""check_package.py — validate the kit before install/publish.

Behavioral checks (not just "files load"):
- every orchestrator script py_compiles
- SKILL.md frontmatter: name==ultracode AND description is explicit-only ($ultracode),
  and does NOT advertise bare-"ultra" activation
- skeptic.toml: required keys, read-only sandbox, UNVERIFIABLE verdict present
- codex_patterns deterministic guards actually behave (verification_shallow, reingest_findings)
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

print("== SKILL.md frontmatter ==")
skill = open(os.path.join(ROOT, "skills", "ultracode", "SKILL.md")).read()
fm = skill.split("---")[1] if "---" in skill else ""
check("name: ultracode", "name: ultracode" in fm)
check("explicit-only ($ultracode) activation", "$ultracode" in fm and "Explicit-only" in fm)
check("does NOT advertise bare-ultra activation",
      'contains "ultra"' not in fm and "standalone word" not in fm,
      "frontmatter still triggers on bare 'ultra'")

print("== skeptic.toml ==")
sk = open(os.path.join(ROOT, "agents", "skeptic.toml")).read()
check("name = skeptic", 'name = "skeptic"' in sk)
check("sandbox_mode = read-only", 'sandbox_mode = "read-only"' in sk)
check("UNVERIFIABLE verdict present", "UNVERIFIABLE" in sk)

print("== codex_patterns deterministic guards ==")
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
try:
    import codex_patterns as cp
    check("verification_shallow flags py_compile", bool(cp.verification_shallow("py_compile x.py")))
    check("verification_shallow passes pytest", cp.verification_shallow("pytest -q") == "")
    d = tempfile.mkdtemp()
    json.dump({"status": "fail", "summary": "x"}, open(os.path.join(d, "worker_07.json"), "w"))
    g = cp.reingest_findings(d)
    check("reingest_findings catches non-name-matching fail", g["gate"] == "fail" and g["files"] == 1)
except Exception as e:
    check("import/run codex_patterns", False, str(e))

print()
if fails:
    print(f"VALIDATION FAILED: {len(fails)} check(s) — {', '.join(fails)}")
    sys.exit(1)
print("VALIDATION PASSED")
