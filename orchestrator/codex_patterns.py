#!/usr/bin/env python3
"""codex_patterns.py — the ultracode *methodology* on top of codex_workflow.

The Workflow tool is just an engine; "ultracode" is the set of quality patterns
you run on it (adversarial verification, judge panels, loop-until-dry, completeness
critic). These are the same patterns Claude Code's ultracode uses, ported to Codex.

Verification discipline here is environment-correct for Codex: skeptics are
read-only and use STATIC lenses (they cannot run shell under a cloud OnRequest
policy — a sandbox-blocked check is UNVERIFIABLE, never a refutation). Anything
that needs running code is the root's job; `verification_shallow` guards against
calling a parse/compile check "proof".
"""
import glob
import json
import os

import codex_workflow as wf

_VERDICT = {
    "type": "object",
    "properties": {"real": {"type": "boolean"}, "why": {"type": "string"}},
}

# Static lenses only — each is something a read-only skeptic can actually check.
# "reproduce"/"does-it-run" is deliberately absent: sub-agents can't run commands
# under OnRequest, so runtime confirmation is the root's job, not a lens.
LENSES = {
    "correctness": "logic errors, wrong conditions, off-by-one, missed cases, contradicted invariants",
    "security": "injection, unsafe deserialization, secrets, path traversal, missing authz",
    "caller-impact": "callers/imports of every changed symbol — are they all updated or broken?",
    "contract": "exact filenames, CLI flags, install/build commands, public API signatures, and docs "
                "match reality (the small-detail failure class: wrong flag, stale README, renamed file)",
}


def adversarial_verify(claim, lenses=("correctness",), threshold=None, evidence=""):
    """Spawn one read-only `skeptic` per lens, each trying to REFUTE the claim. The
    claim survives only if a majority vote it real. Independence is the point — a
    finder grading its own work is theater. Paste the claim + its evidence INTO the
    prompt (don't make the skeptic hunt for it). Returns {'survives','votes'}."""
    threshold = threshold or (len(lenses) // 2 + 1)
    votes = wf.parallel([
        (lambda lens=lens: wf.agent(
            f"Adversarially verify this claim through the {lens} lens "
            f"({LENSES.get(lens, lens)}). Try to REFUTE it; default to real=false if the "
            f"evidence does not clearly hold. If confirming would require running code you "
            f"cannot run, say so in `why` and return real=false (UNVERIFIABLE is not a pass)."
            f"\n\nClaim: {claim}\n\nEvidence:\n{evidence or '(none supplied — reason from the repo)'}",
            schema=_VERDICT, role="skeptic"))
        for lens in lenses
    ])
    real = [v for v in votes if v and v.get("real")]
    return {"survives": len(real) >= threshold, "votes": votes}


def judge_panel(task, approaches, schema=None):
    """N independent attempts from different angles, then one judge scores them.
    Beats one-attempt-iterated when the solution space is wide. Returns the winner."""
    attempts = wf.parallel([
        (lambda a=a: {"approach": a, "answer": wf.agent(f"{task}\n\nApproach: {a}.", schema=schema)})
        for a in approaches
    ])
    attempts = [a for a in attempts if a]
    judged = wf.agent(
        "Score these attempts 0-10 for correctness and completeness; return the index of the best.\n\n"
        + "\n\n".join(f"[{i}] ({a['approach']}): {a['answer']}" for i, a in enumerate(attempts)),
        schema={"type": "object", "properties": {"best_index": {"type": "integer"},
                                                  "why": {"type": "string"}}}, role="judge")
    idx = judged.get("best_index", 0) if judged else 0
    winner = attempts[idx] if attempts and 0 <= idx < len(attempts) else (attempts[0] if attempts else None)
    return {"winner": winner, "why": (judged or {}).get("why"), "attempts": attempts}


def loop_until_dry(finder, key, max_dry=2, max_rounds=10):
    """Run finder() in rounds until `max_dry` consecutive rounds surface nothing new.
    finder() returns a list of items; `key(item)` dedupes across rounds. For unknown-size
    discovery (bugs, edge cases) where a fixed count misses the tail."""
    seen, found, dry, rnd = set(), [], 0, 0
    while dry < max_dry and rnd < max_rounds:
        rnd += 1
        batch = finder(rnd) or []
        fresh = [x for x in batch if key(x) not in seen]
        for x in fresh:
            seen.add(key(x))
        found += fresh
        dry = dry + 1 if not fresh else 0
        wf.log(f"round {rnd}: +{len(fresh)} new ({len(found)} total), dry={dry}")
    return found


def completeness_critic(original_request, work_summary):
    """Map each explicit requirement to a 5-state status — a retrieval fix for the
    'lost in the middle' problem, not a self-grade. States must not be collapsed:
      verified  — proven by executed evidence
      detected  — a check exists but was not run
      inferred  — believed from reading, not proven
      needs-confirmation — conflicting/uncertain
      unresolved — not addressed
    Anything not `verified` is not done."""
    return wf.agent(
        "Re-read the original request and the work done. For EACH explicit requirement, give "
        "one status: verified | detected | inferred | needs-confirmation | unresolved. Do not "
        "collapse statuses (a parse check that ran is 'detected', not 'verified'). List every "
        "requirement that is not `verified`.\n\n"
        f"REQUEST:\n{original_request}\n\nWORK:\n{work_summary}",
        schema={"type": "object", "properties": {
            "requirements": {"type": "array", "items": {"type": "object", "properties": {
                "requirement": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["verified", "detected", "inferred", "needs-confirmation", "unresolved"]},
                "evidence": {"type": "string"}}}},
            "all_verified": {"type": "boolean"}}},
        role="completeness critic")


# --- Deterministic anti-theater guards (no model call; safe to run anywhere) -------

_SHALLOW = ("py_compile", "--noemit", "--no-emit", "tsc --noemit", "compileall",
            "ast.parse", "syntax", "lint", "ruff", "flake8", "import ")


def verification_shallow(commands):
    """Given the command(s) that 'verified' a change, return a warning if the only
    thing that ran was a parse/compile/lint check — which proves files parse, NOT
    that behavior is correct. `commands` is a string or list of strings.
    Returns "" when at least one real behavioral check (a test runner) ran."""
    cmds = [commands] if isinstance(commands, str) else list(commands or [])
    blob = " \n ".join(cmds).lower()
    if not blob.strip():
        return "no verification ran — behavior is unproven"
    ran_tests = any(t in blob for t in ("pytest", "unittest", "go test", "cargo test",
                                        "jest", "vitest", "npm test", "npm run test", "mocha", "rspec"))
    if ran_tests:
        return ""
    if any(s in blob for s in _SHALLOW):
        return ("verification was shallow — only parse/compile/lint ran; this proves files "
                "parse, not that behavior is correct. Run the test suite before claiming success.")
    return ""


def reingest_findings(results_dir):
    """Read EVERY *.json worker/skeptic result under results_dir (not just files whose
    name matches 'adversarial' — that filename filter is a real gap in lookalike tools)
    and surface blocking findings. A result that only exists (empty/no findings) does
    NOT pass the gate. Returns {'blocking': [...], 'files': n, 'gate': 'pass'|'fail'}."""
    blocking, n = [], 0
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        n += 1
        try:
            data = json.load(open(path))
        except Exception:
            blocking.append({"file": os.path.basename(path), "severity": "high",
                             "claim": "result file is not valid JSON"})
            continue
        recs = data if isinstance(data, list) else [data]
        for r in recs:
            if not isinstance(r, dict):
                continue
            if str(r.get("status", "")).lower() in ("fail", "failed", "blocked"):
                blocking.append({"file": os.path.basename(path), "severity": "high",
                                 "claim": f"worker status={r.get('status')}: {str(r.get('summary', ''))[:160]}"})
            # completeness_critic output: anything not all-verified blocks the gate
            if r.get("all_verified") is False:
                unmet = [q.get("requirement", "?") for q in (r.get("requirements") or [])
                         if isinstance(q, dict) and q.get("status") != "verified"]
                blocking.append({"file": os.path.basename(path), "severity": "high",
                                 "claim": f"not all requirements verified: {', '.join(unmet)[:160]}"})
            for f in (r.get("findings") or []):
                if isinstance(f, dict) and str(f.get("severity", "")).lower() in ("critical", "high"):
                    blocking.append({"file": os.path.basename(path), "severity": f.get("severity"),
                                     "claim": str(f.get("claim", ""))[:160]})
    return {"blocking": blocking, "files": n, "gate": "fail" if blocking else "pass"}


def review_then_verify(dimensions, find_schema, target="the code in this directory", run_dir=None):
    """Canonical ultracode review: one reviewer per dimension, each finding then
    adversarially verified with STATIC lenses (pipeline = no barrier, each verifies
    as soon as its review lands).

    If `run_dir` is given (from wf.start_run), every confirmed finding is persisted via
    wf.save_result and a ledger is written via wf.write_ledger — so the audit trail is
    actually produced, not just available as an API."""
    def review(dim, _o, _i):
        return wf.agent(f"Review {target} for {dim}. Return your findings.",
                        schema=find_schema, role=f"{dim} reviewer")

    def verify(rev, dim, _i):
        items = (rev or {}).get("findings", [])
        return wf.parallel([
            (lambda f=f: {**f, "dim": dim, "verdict": adversarial_verify(
                json.dumps(f), lenses=("correctness", "security", "contract"))})
            for f in items])

    out = wf.pipeline(dimensions, review, verify)
    confirmed = [f for sub in out if sub for f in sub if f and f["verdict"]["survives"]]
    if run_dir:
        for i, f in enumerate(confirmed):
            wf.save_result(run_dir, f"finding_{i}", f)
        wf.write_ledger(run_dir, {
            "Scope": f"{target}; dimensions: {', '.join(map(str, dimensions))}",
            "Findings": "\n".join(f"- [{f.get('dim')}] {str(f)[:200]}" for f in confirmed) or "none survived verification",
            "Adversarial gate": f"{len(confirmed)} confirmed (static lenses: correctness/security/contract)",
        })
    return confirmed
