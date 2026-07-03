#!/usr/bin/env python3
"""codex_patterns.py — the ultracode *methodology* on top of codex_workflow.

The Workflow tool is just an engine; "ultracode" is the set of quality patterns
you run on it (adversarial verification, judge panels, loop-until-dry, multi-modal
sweeps, completeness critic). These are the same patterns Claude Code's ultracode
uses, ported to Codex.

Verification discipline here is environment-correct for Codex: skeptics are
read-only and use STATIC lenses (they cannot run shell under a cloud OnRequest
policy — a sandbox-blocked check is UNVERIFIABLE, never a refutation). Anything
that needs running code is the root's job; `verification_shallow` guards against
calling a parse/compile check "proof".
"""
import glob
import json
import os
import re

import codex_workflow as wf

_VERDICT = {
    "type": "object",
    "properties": {"real": {"type": "boolean"}, "unverifiable": {"type": "boolean"},
                   "why": {"type": "string"}},
}

# Static lenses only — each is something a read-only skeptic can actually check.
# "reproduce"/"does-it-run" is deliberately absent: sub-agents can't run commands
# under OnRequest, so runtime confirmation is the root's job, not a lens.
LENSES = {
    "correctness": "logic errors, wrong conditions, off-by-one, missed cases, contradicted invariants",
    "security": "injection, unsafe deserialization, secrets, path traversal, missing authz",
    "perf": "complexity blowups, N+1 query/IO patterns, unbounded caches/queues/retries, "
            "work inside hot loops — statically checkable, no execution needed",
    "caller-impact": "callers/imports of every changed symbol — are they all updated or broken?",
    "contract": "exact filenames, CLI flags, install/build commands, public API signatures, and docs "
                "match reality (the small-detail failure class: wrong flag, stale README, renamed file)",
}


def adversarial_verify(claim, lenses=("correctness",), threshold=None, evidence=""):
    """Spawn one read-only `skeptic` per lens, each trying to REFUTE the claim. The
    claim survives only if a majority vote it real. Independence is the point — a
    finder grading its own work is theater. Paste the claim + its evidence INTO the
    prompt (don't make the skeptic hunt for it).

    Returns {'survives','status','votes','failed','unverifiable'}. Gating is
    fail-closed (a dead or blocked skeptic never counts as a confirmation), but
    `status` keeps the failure modes honest: 'refuted' only when live actual
    refutations were decisive; 'unverified' when dead skeptics OR sandbox-blocked
    (UNVERIFIABLE) votes could have flipped the outcome — an unverifiable claim is
    kept-but-flagged for the root to confirm at runtime, never reported as refuted."""
    if not lenses:
        raise ValueError("adversarial_verify requires at least one lens")
    if threshold is None:
        threshold = len(lenses) // 2 + 1
    elif not 1 <= threshold <= len(lenses):
        # unreachable thresholds corrupt the status math (0 refutations reading as
        # 'refuted'; negative values auto-confirming refuted claims)
        raise ValueError(f"threshold must be in [1, {len(lenses)}], got {threshold}")
    n = len(lenses)
    votes = wf.parallel([
        (lambda j=j, lens=lens: wf.agent(
            f"You are independent skeptic {j + 1} of {n}.\n"
            f"Adversarially verify this claim through the {lens} lens "
            f"({LENSES.get(lens, lens)}). Try to REFUTE it; default to real=false if the "
            f"evidence does not clearly hold. If your check is BLOCKED — confirming would "
            f"require running code you cannot run — return real=false AND unverifiable=true "
            f"with the blocked step in `why` (UNVERIFIABLE is not a pass, but it is not a "
            f"refutation either); otherwise unverifiable=false."
            f"\n\nClaim: {claim}\n\nEvidence:\n{evidence or '(none supplied — reason from the repo)'}",
            schema=_VERDICT, role="skeptic", label=f"skeptic:{lens}"))
        for j, lens in enumerate(lenses)
    ])
    real = [v for v in votes if v and v.get("real")]
    failed = sum(1 for v in votes if v is None)
    blocked = sum(1 for v in votes if v and not v.get("real") and v.get("unverifiable"))
    survives = len(real) >= threshold
    status = "confirmed" if survives else (
        "unverified" if len(real) + failed + blocked >= threshold else "refuted")
    if failed or (blocked and not survives):
        wf.log(f"adversarial_verify: {failed} dead skeptic(s), {blocked} UNVERIFIABLE "
               f"vote(s) — status={status}")
    return {"survives": survives, "status": status, "votes": votes, "failed": failed,
            "unverifiable": blocked}


def judge_panel(task, approaches, schema=None, n_judges=1, synthesize=True):
    """N independent attempts from different angles, scored by a judge panel, then a
    synthesis pass that starts from the winner and grafts the runners-up's judge-noted
    strengths. Beats one-attempt-iterated when the solution space is wide.

    Returns {'winner','why','scores','synthesis','attempts'} — 'synthesis' is None
    when skipped (synthesize=False, <2 attempts, or the synthesizer failed)."""
    attempts = wf.parallel([
        (lambda a=a: {"approach": a, "answer": wf.agent(f"{task}\n\nApproach: {a}.",
                                                        schema=schema, label=f"attempt:{a}")})
        for a in approaches
    ])
    attempts = [a for a in attempts if a]
    if not attempts:
        wf.log("judge_panel: all attempts failed — skipping judge")
        return {"winner": None, "why": "all attempts failed", "scores": [],
                "synthesis": None, "attempts": []}
    listing = "\n\n".join(f"[{i}] ({a['approach']}): {a['answer']}" for i, a in enumerate(attempts))
    judge_schema = {"type": "object", "properties": {
        "scores": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "score": {"type": "number"},
            "strengths": {"type": "string"}}}},
        "best_index": {"type": "integer"}, "why": {"type": "string"}}}
    judges = wf.parallel([
        (lambda: wf.agent(
            "Score these attempts 0-10 for correctness and completeness. Return per-attempt "
            "scores with each attempt's concrete strengths, plus the index of the best.\n\n"
            + listing, schema=judge_schema, role="judge", label="judge"))
        for _ in range(max(1, n_judges))
    ])
    judges = [j for j in judges if j]
    if judges:
        totals = {}
        for j in judges:
            for s in (j.get("scores") or []):
                if isinstance(s, dict) and isinstance(s.get("index"), int):
                    totals[s["index"]] = totals.get(s["index"], 0) + (s.get("score") or 0)
        if totals:
            idx = max(totals, key=totals.get)
        else:
            bests = [j.get("best_index") for j in judges if isinstance(j.get("best_index"), int)]
            idx = max(set(bests), key=bests.count) if bests else 0
        if not (0 <= idx < len(attempts)):
            wf.log(f"judge returned invalid best_index={idx!r}; falling back to attempts[0]")
            idx = 0
        why = judges[0].get("why")
    else:
        wf.log("judge_panel: all judges failed — falling back to attempts[0]")
        idx, why = 0, "all judges failed"
    winner = attempts[idx]
    synthesis = None
    if synthesize and len(attempts) >= 2:
        strengths = {}
        for j in judges:
            for s in (j.get("scores") or []):
                if isinstance(s, dict) and isinstance(s.get("index"), int) and s.get("strengths"):
                    strengths.setdefault(s["index"], []).append(str(s["strengths"]))
        runners = "\n\n".join(
            f"[runner-up {i}] ({a['approach']})"
            + (f" — judge-noted strengths: {'; '.join(strengths[i])}" if i in strengths else "")
            + f":\n{a['answer']}"
            for i, a in enumerate(attempts) if i != idx)
        try:
            synthesis = wf.agent(
                f"{task}\n\nProduce the final answer starting from the winner below, grafting "
                f"concretely better ideas from the runners-up. Do not average them — keep the "
                f"winner's structure and improve it.\n\n[winner] ({winner['approach']}):\n"
                f"{winner['answer']}\n\n{runners}",
                schema=schema, role="synthesizer", label="synthesize")
        except Exception as e:
            wf.log(f"judge_panel: synthesis failed, returning winner as-is: {e}")
    elif synthesize:
        wf.log("judge_panel: <2 attempts — skipping synthesis (no runners-up to graft)")
    return {"winner": winner, "why": why, "scores": judges, "synthesis": synthesis,
            "attempts": attempts}


def loop_until_dry(finder, key, max_dry=2, max_rounds=10):
    """Run finder() in rounds until `max_dry` consecutive rounds surface nothing new.
    finder() returns a list of items; `key(item)` dedupes across rounds. For unknown-size
    discovery (bugs, edge cases) where a fixed count misses the tail. Logs a CAPPED
    warning if max_rounds is hit before convergence — a cap is a bound, not coverage."""
    seen, found, dry, rnd, fresh = set(), [], 0, 0, []
    while dry < max_dry and rnd < max_rounds:
        rnd += 1
        batch = finder(rnd) or []
        fresh = [x for x in batch if key(x) not in seen]
        for x in fresh:
            seen.add(key(x))
        found += fresh
        dry = dry + 1 if not fresh else 0
        wf.log(f"round {rnd}: +{len(fresh)} new ({len(found)} total), dry={dry}")
    if dry < max_dry:
        wf.log(f"loop_until_dry CAPPED at max_rounds={max_rounds} before {max_dry} consecutive "
               f"dry rounds (last round +{len(fresh)}) — coverage is bounded, not proven exhausted")
    return found


def multi_modal_sweep(task, modalities, key, schema=None):
    """One BLIND finder per search modality (by-name, by-content, by-caller,
    by-history, …), then dedup across modalities by `key` — one search angle won't
    find everything, and the agents stay blind to each other. Each finder should
    return a list, or {'findings': [...]} when `schema` shapes it that way.
    Returns {'items','per_modality','failed_modalities'} — failed modalities are
    named, never silently folded into 'covered'."""
    modalities = list(modalities)
    results = wf.parallel([
        (lambda m=m: wf.agent(
            f"{task}\n\nSearch using ONLY this strategy: {m}. Other strategies are covered "
            f"by other agents — do not attempt them. Return raw findings as a JSON array; "
            f"return an empty array if this angle surfaces nothing.",
            schema=schema, role=f"{m} finder", label=f"sweep:{m}"))
        for m in modalities
    ])
    seen, items, per, failed = set(), [], {}, []
    for m, r in zip(modalities, results):
        if r is None:
            failed.append(str(m))
            continue
        if isinstance(r, str):
            # schema=None returns text — parse the JSON the prompt asked for; an
            # unparseable reply is one opaque finding, never a char-by-char iterable
            try:
                r = json.loads(wf._extract_json(r, expect="array"))
            except (json.JSONDecodeError, ValueError):
                r = [r] if r.strip() else []
        batch = (r.get("findings") or []) if isinstance(r, dict) else (
            r if isinstance(r, list) else ([r] if r else []))
        fresh = [x for x in batch if key(x) not in seen]
        for x in fresh:
            seen.add(key(x))
        items.extend(fresh)
        per[str(m)] = len(fresh)
    wf.log(f"multi_modal_sweep: {len(items)} unique across {len(modalities)} modalities"
           + (f" ({len(failed)} FAILED: {', '.join(failed)})" if failed else " (all ran)"))
    return {"items": items, "per_modality": per, "failed_modalities": failed}


def completeness_critic(original_request, work_summary):
    """Map each explicit requirement to a 5-state status — a retrieval fix for the
    'lost in the middle' problem, not a self-grade. States must not be collapsed:
      verified  — proven by executed evidence
      detected  — a check exists but was not run
      inferred  — believed from reading, not proven
      needs-confirmation — conflicting/uncertain
      unresolved — not addressed
    Anything not `verified` is not done. `gaps` is next-round WORK, not just a
    verdict — feed each gap back as a finder/worker input (the next round of
    loop_until_dry or a new wf.parallel batch) before re-running the critic."""
    return wf.agent(
        "Re-read the original request and the work done. For EACH explicit requirement, give "
        "one status: verified | detected | inferred | needs-confirmation | unresolved. Do not "
        "collapse statuses (a parse check that ran is 'detected', not 'verified'). List every "
        "requirement that is not `verified`. Then, SEPARATELY from the requirement table, list "
        "what is MISSING that no explicit requirement names: a search angle or modality never "
        "run, a claim asserted but never verified, a file or source relied on but never read, "
        "an implied constraint unaddressed. Phrase each as a concrete follow-up task.\n\n"
        f"REQUEST:\n{original_request}\n\nWORK:\n{work_summary}",
        schema={"type": "object", "properties": {
            "requirements": {"type": "array", "items": {"type": "object", "properties": {
                "requirement": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["verified", "detected", "inferred", "needs-confirmation", "unresolved"]},
                "evidence": {"type": "string"}}}},
            "gaps": {"type": "array", "items": {"type": "object", "properties": {
                "task": {"type": "string"}, "why": {"type": "string"}}}},
            "all_verified": {"type": "boolean"}}},
        role="completeness critic", label="completeness-critic")


# --- Deterministic anti-theater guards (no model call; safe to run anywhere) -------

_SHALLOW = ("py_compile", "--noemit", "--no-emit", "tsc --noemit", "compileall",
            "ast.parse", "syntax", "lint", "ruff", "flake8", "import ")

_RUNNERS = ("pytest", "unittest", "go test", "cargo test",
            "jest", "vitest", "npm test", "npm run test", "mocha", "rspec")


def verification_shallow(commands):
    """Given the command(s) that 'verified' a change, return a warning unless a
    recognized test-runner invocation appears in the commands. Fail-closed on the
    unknown side: empty or unrecognized commands warn — an unproven check must never
    read as a pass. Runner names match at command boundaries ('.venv/bin/pytest -q'
    counts; 'tests/pytest_fixtures' does not), but this is a text heuristic: it
    cannot tell an executed runner from one merely named in another command
    ('git log --grep pytest' passes), nor whether the run succeeded — it gates
    theater, it does not prove execution. `commands` is a string or list of strings."""
    cmds = [commands] if isinstance(commands, str) else list(commands or [])
    blob = " \n ".join(cmds).lower()
    if not blob.strip():
        return "no verification ran — behavior is unproven"
    ran_tests = any(re.search(r"(?:^|[\s;&|/(])" + re.escape(t) + r"(?![\w-])", blob)
                    for t in _RUNNERS)
    if ran_tests:
        return ""
    if any(s in blob for s in _SHALLOW):
        return ("verification was shallow — only parse/compile/lint ran; this proves files "
                "parse, not that behavior is correct. Run the test suite before claiming success.")
    return ("no recognized test runner ran — the verification commands do not prove "
            "behavior; run the actual test suite before claiming success.")


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
            # a record that IS a finding (review_then_verify saves confirmed findings
            # as bare dicts) — a confirmed high-severity finding must block the gate
            if str(r.get("severity", "")).lower() in ("critical", "high") \
                    and ("claim" in r or "title" in r):
                blocking.append({"file": os.path.basename(path), "severity": r.get("severity"),
                                 "claim": str(r.get("claim") or r.get("title"))[:160]})
            # completeness_critic output: anything not all-verified blocks the gate
            if r.get("all_verified") is False:
                unmet = [q.get("requirement", "?") for q in (r.get("requirements") or [])
                         if isinstance(q, dict) and q.get("status") != "verified"]
                blocking.append({"file": os.path.basename(path), "severity": "high",
                                 "claim": f"not all requirements verified: {', '.join(unmet)[:160]}"})
            # completeness_critic gaps are next-round work — undone work blocks the gate
            # ("all_verified" in r scopes this to critic output, not unrelated JSON)
            if "all_verified" in r and r.get("gaps"):
                gaps = r["gaps"] if isinstance(r["gaps"], list) else [r["gaps"]]
                blocking.append({"file": os.path.basename(path), "severity": "high",
                                 "claim": ("completeness gaps (next-round work not done): "
                                           + "; ".join((str(g.get("task", g)) if isinstance(g, dict)
                                                        else str(g))[:80]
                                                       for g in gaps if g))[:160]})
            for f in (r.get("findings") or []):
                if isinstance(f, dict) and str(f.get("severity", "")).lower() in ("critical", "high"):
                    blocking.append({"file": os.path.basename(path), "severity": f.get("severity"),
                                     "claim": str(f.get("claim", ""))[:160]})
    return {"blocking": blocking, "files": n, "gate": "fail" if blocking else "pass"}


_DIM_ALIAS = {"performance": "perf"}


def review_then_verify(dimensions, find_schema, target="the code in this directory",
                       run_dir=None, key=None):
    """Canonical ultracode review: one reviewer per dimension, each finding then
    adversarially verified with STATIC lenses (pipeline = no barrier, each verifies
    as soon as its review lands). The finding's own dimension is used as a lens when
    it maps to LENSES (e.g. a perf finding gets the perf lens).

    Confirmed findings are deduped AFTER verification by `key` (default: normalized
    file/line/claim) — duplicates still each cost a skeptic panel; deduping earlier
    would need a barrier between review and verify, a trade left to the caller.

    A reviewer agent that dies drops its whole dimension: that is logged, recorded in
    the ledger's Coverage section, and (with run_dir) persisted as a failing coverage
    result so reingest_findings' gate fails closed — 0 findings with a dropped
    dimension must never read as a clean review.

    If `run_dir` is given (from wf.start_run), every confirmed finding is persisted via
    wf.save_result and a ledger is written via wf.write_ledger — so the audit trail is
    actually produced, not just available as an API."""
    def review(dim, _o, _i):
        return wf.agent(f"Review {target} for {dim}. Return your findings.",
                        schema=find_schema, role=f"{dim} reviewer", label=f"review:{dim}")

    def verify(rev, dim, _i):
        raw = (rev or {}).get("findings") or []
        raw = raw if isinstance(raw, list) else [raw]
        # a non-dict finding (string-shaped find_schema) must still be verified —
        # letting it TypeError would drop the whole dimension as a chain failure
        items = [f if isinstance(f, dict) else {"claim": str(f)} for f in raw if f]
        dl = _DIM_ALIAS.get(str(dim).lower(), str(dim).lower())
        lenses = tuple(dict.fromkeys(([dl] if dl in LENSES else [])
                                     + ["correctness", "security", "contract"]))[:3]
        return wf.parallel([
            (lambda f=f: {**f, "dim": dim, "verdict": adversarial_verify(
                json.dumps(f), lenses=lenses)})
            for f in items])

    out = wf.pipeline(dimensions, review, verify)
    # None = the reviewer/verify chain raised (dimension NOT reviewed); [] = reviewed, clean.
    dropped = [str(d) for d, sub in zip(dimensions, out) if sub is None]
    if dropped:
        wf.log(f"coverage: {len(dropped)} dimension(s) NOT reviewed: {', '.join(dropped)}")
    flat = [f for sub in out if sub for f in sub if f]
    confirmed = [f for f in flat if f["verdict"]["survives"]]
    refuted = [f for f in flat if f["verdict"]["status"] == "refuted"]
    unverified = [f for f in flat if f["verdict"]["status"] == "unverified"]
    # post-verification dedup: same issue found via two dimensions collapses to one.
    # Coalesce with `or`, not dict.get defaults — claim: null must fall through to
    # the whole-content fallback, or distinct null-claim findings would merge.
    def _default_key(f):
        text = (f.get("claim") or f.get("title")
                or json.dumps({k: v for k, v in f.items() if k not in ("dim", "verdict")},
                              sort_keys=True))
        return (str(f.get("file") or "").strip().lower(), f.get("line"),
                " ".join(str(text).lower().split()))
    key = key or _default_key
    uniq, seen = [], {}
    for f in confirmed:
        k = key(f)
        if k in seen:
            prev = seen[k]
            prev["dims"] = sorted({str(d) for d in (prev.get("dims") or [prev.get("dim")])}
                                  | {str(f.get("dim"))})
        else:
            seen[k] = f
            uniq.append(f)
    if len(uniq) < len(confirmed):
        wf.log(f"deduped {len(confirmed) - len(uniq)} duplicate confirmed finding(s)")
    confirmed = uniq
    if run_dir:
        for i, f in enumerate(confirmed):
            wf.save_result(run_dir, f"finding_{i}", f)
        # coverage persists as a pass/fail result so reingest_findings gates on it
        wf.save_result(run_dir, "coverage", {
            "status": "fail" if dropped else "pass",
            "summary": (f"dimensions not reviewed (reviewer agent failed): {', '.join(dropped)}"
                        if dropped else "all dimensions reviewed"),
            "covered": [str(d) for d in dimensions if str(d) not in dropped],
            "dropped": dropped})
        gate = (f"{len(confirmed)} confirmed / {len(refuted)} refuted / "
                f"{len(unverified)} unverified (skeptic failures)")
        gate += (f"; INCOMPLETE — {len(dropped)} dimension(s) unreviewed" if dropped
                 else "; all dimensions reviewed")
        if wf.budget_exhausted():
            gate = ("INCOMPLETE — token budget exhausted mid-verification; results are "
                    "partial, NOT a clean pass. " + gate)
        findings_txt = "\n".join(f"- [{f.get('dim')}] {str(f)[:200]}" for f in confirmed)
        if not findings_txt:
            findings_txt = "none survived verification"
            if unverified:
                findings_txt += (f" ({len(unverified)} finding(s) could not be verified "
                                 f"due to skeptic failures)")
            if dropped:
                findings_txt += f" (and {len(dropped)} dimension(s) went unreviewed)"
        wf.write_ledger(run_dir, {
            "Scope": f"{target}; dimensions: {', '.join(map(str, dimensions))}",
            "Coverage": f"reviewed {len(dimensions) - len(dropped)}/{len(dimensions)} dimensions"
                        + (f"; NOT reviewed (reviewer failed): {', '.join(dropped)}" if dropped else ""),
            "Findings": findings_txt,
            "Adversarial gate": gate,
        })
    return confirmed
