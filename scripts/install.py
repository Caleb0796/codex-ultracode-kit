#!/usr/bin/env python3
"""install.py — install/uninstall the codex-ultracode-kit into $CODEX_HOME.

Cross-platform core (driven by install.sh / install.ps1). Installs the skill and the
skeptic agent role; prints (does NOT auto-apply) the [agents] config snippet, because
editing your config.toml is your deliberate, backed-up decision. Ships NO Codex hooks
by design: hooks do not fire under non-interactive `codex exec`, so guardrails live in
the external harness (codex_workflow.py) which we control as the subprocess parent.

Usage:
  python3 scripts/install.py [--codex-home DIR] [--dry-run]
  python3 scripts/install.py --uninstall [--codex-home DIR] [--dry-run]
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ignore(_d, names):
    return [n for n in names if n in ("__pycache__",) or n.endswith((".pyc", ".pyo"))]


def install(home, dry):
    skill_src = os.path.join(ROOT, "skills", "ultracode")
    skill_dst = os.path.join(home, "skills", "ultracode")
    role_src = os.path.join(ROOT, "agents", "skeptic.toml")
    role_dst = os.path.join(home, "agents", "skeptic.toml")
    for src in (skill_src, role_src):
        if not os.path.exists(src):
            sys.exit(f"ERROR: missing source {src} — run from a complete checkout.")
    print(f"skill : {skill_src}  ->  {skill_dst}")
    print(f"role  : {role_src}  ->  {role_dst}")
    if dry:
        print("(dry-run — nothing written)")
    else:
        os.makedirs(os.path.dirname(skill_dst), exist_ok=True)
        os.makedirs(os.path.dirname(role_dst), exist_ok=True)
        if os.path.exists(skill_dst):
            shutil.rmtree(skill_dst)              # idempotent: replace in place
        shutil.copytree(skill_src, skill_dst, ignore=_ignore)
        shutil.copy2(role_src, role_dst)
        print("installed.")
    print("\nNext steps (manual, by design):")
    print("  1) Raise the in-session agent cap — add to your config.toml (back it up first):")
    print("       [agents]\n       max_threads = 16   # default 6; no hard upper bound on the V1 path")
    print("  2) External harness for large fan-out lives in orchestrator/ — `pip install jsonschema`")
    print("     enables strict schema validation. Run it from your project with CODEX_WF_* env knobs.")
    print("  3) Verify: codex exec \"List the skills available to you, names only.\"  -> expect 'ultracode'")
    print("  Invoke with the explicit token, e.g.:  $ultracode review this branch for bugs")


def uninstall(home, dry):
    targets = [os.path.join(home, "skills", "ultracode"),
               os.path.join(home, "agents", "skeptic.toml")]
    for t in targets:
        exists = os.path.exists(t)
        print(f"remove: {t}  {'(exists)' if exists else '(absent)'}")
        if exists and not dry:
            shutil.rmtree(t) if os.path.isdir(t) else os.remove(t)
    print("(dry-run — nothing removed)" if dry else "uninstalled.")
    print("Note: your config.toml [agents] block is left untouched — edit it yourself if desired.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    print(f"CODEX_HOME = {a.codex_home}")
    (uninstall if a.uninstall else install)(a.codex_home, a.dry_run)


if __name__ == "__main__":
    main()
