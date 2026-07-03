"""Builtin saved workflow: the canonical ultracode review — one reviewer per
dimension, every finding adversarially verified by independent skeptics before it
survives (codex_patterns.review_then_verify, coverage-honest ledger included).

args: {"dimensions": [str] (required), "target": str = "the code in this directory",
       "run_dir": str|None (reuse a caller's run dir instead of minting one)}
Returns {"confirmed", "dimensions", "run_dir"}.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_patterns as cp  # noqa: E402
import codex_workflow as wf  # noqa: E402

META = {"name": "review",
        "description": "one reviewer per dimension; findings survive only adversarial "
                       "skeptic verification",
        "phases": ["review", "verify"]}

_FIND_SCHEMA = {"type": "object", "properties": {"findings": {
    "type": "array", "items": {"type": "object", "properties": {
        "file": {"type": "string"}, "line": {"type": "integer"},
        "claim": {"type": "string"}, "severity": {"type": "string"}}}}}}


def run(args):
    args = args or {}
    dims = [str(d) for d in (args.get("dimensions") or []) if str(d).strip()]
    if not dims:
        raise ValueError("args.dimensions is required (non-empty list)")
    target = args.get("target") or "the code in this directory"
    run_dir = args.get("run_dir") or wf.start_run(f"review {target} on: {', '.join(dims)}",
                                                  mode="review")
    confirmed = cp.review_then_verify(dims, _FIND_SCHEMA, target=target, run_dir=run_dir)
    return {"confirmed": confirmed, "dimensions": dims, "run_dir": run_dir}
