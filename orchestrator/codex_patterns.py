#!/usr/bin/env python3
"""codex_patterns.py — the ultracode *methodology* on top of codex_workflow.

The Workflow tool is just an engine; "ultracode" is the set of quality patterns
you run on it (adversarial verification, judge panels, loop-until-dry, completeness
critic). These are the same patterns Claude Code's ultracode uses, ported to Codex.
"""
import json

import codex_workflow as wf

_VERDICT = {
    "type": "object",
    "properties": {"real": {"type": "boolean"}, "why": {"type": "string"}},
}


def adversarial_verify(claim, lenses=("correctness",), threshold=None):
    """Spawn one skeptic per lens, each trying to REFUTE the claim. The claim
    survives only if a majority vote it real. Independence is the point — a finder
    grading its own work is theater. Returns {'survives','votes'}."""
    threshold = threshold or (len(lenses) // 2 + 1)
    votes = wf.parallel([
        (lambda lens=lens: wf.agent(
            f"Adversarially verify this claim through the {lens} lens. Try to REFUTE it; "
            f"default to real=false if the evidence does not clearly hold.\n\nClaim: {claim}",
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
    return {"winner": attempts[judged["best_index"]] if attempts else None,
            "why": judged.get("why"), "attempts": attempts}


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
    """A final pass that maps each explicit requirement to whether it was satisfied —
    a retrieval fix for the 'lost in the middle' problem, not a self-grade. Returns the gaps."""
    return wf.agent(
        "Re-read this original request and the work done. List each explicit requirement and "
        "whether the work satisfies it. Anything unsatisfied is NOT DONE.\n\n"
        f"REQUEST:\n{original_request}\n\nWORK:\n{work_summary}",
        schema={"type": "object", "properties": {"gaps": {"type": "array", "items": {"type": "string"}},
                                                  "complete": {"type": "boolean"}}}, role="completeness critic")


def review_then_verify(dimensions, find_schema, target="the code in this directory"):
    """Canonical ultracode review: one reviewer per dimension, each finding then
    adversarially verified (pipeline = no barrier, each verifies as soon as its review lands)."""
    def review(dim, _o, _i):
        return wf.agent(f"Review {target} for {dim}. Return your findings.",
                        schema=find_schema, role=f"{dim} reviewer")

    def verify(rev, dim, _i):
        items = (rev or {}).get("findings", [])
        return wf.parallel([
            (lambda f=f: {**f, "dim": dim, "verdict": adversarial_verify(
                json.dumps(f), lenses=("correctness", "security", "reproduce"))})
            for f in items])

    out = wf.pipeline(dimensions, review, verify)
    return [f for sub in out if sub for f in sub if f and f["verdict"]["survives"]]
