"""Builtin saved workflow: n angle-diverse workers + synthesis (the multi-angle-sweep
shape). Invoke via wf.workflow("fanout", args) or the MCP front door's
ultracode_workflow tool.

args: {"task": str (required), "n": int = 4, "effort": str|None,
       "run_dir": str|None (reuse a caller's run dir instead of minting one)}
Returns {"result", "workers_ok", "workers_failed", "worker_errors", "run_dir"}.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_workflow as wf  # noqa: E402

META = {"name": "fanout",
        "description": "n angle-diverse workers + synthesis, coverage-honest",
        "phases": ["fan-out", "synthesize"]}


def run(args):
    args = args or {}
    task = (args.get("task") or "").strip()
    if not task:
        raise ValueError("args.task is required")
    n = max(1, int(args.get("n") or 4))
    effort = args.get("effort")
    run_dir = args.get("run_dir") or wf.start_run(task, mode="fanout")
    errors = []

    def worker(k):
        try:
            return wf.agent(
                f"{task}\n\nYou are worker {k + 1} of {n}. Take the {k + 1}-th distinct "
                f"angle on this task — do not duplicate the obvious first approach when "
                f"k > 1. Return raw findings/data, not polished prose.",
                effort=effort, label=f"worker-{k + 1}", phase="fan-out")
        except Exception as e:
            errors.append({"index": k + 1, "error": f"{type(e).__name__}: {e}"})
            raise

    wf.phase("fan-out")
    outs = wf.parallel([(lambda k=k: worker(k)) for k in range(n)])
    ok = [o for o in outs if o is not None]
    wf.save_result(run_dir, "workers", {
        "requested": n, "succeeded": len(ok), "failed": n - len(ok),
        "workers": ([{"index": e["index"], "ok": False, "error": e["error"]} for e in errors]
                    + [{"index": i + 1, "ok": True} for i, o in enumerate(outs) if o is not None])})
    if not ok:
        errs = "; ".join(e["error"] for e in errors)
        wf.write_ledger(run_dir, {"Scope": task, "Coverage": f"0/{n} workers succeeded",
                                  "Findings": "none — all workers failed",
                                  "Unresolved risks": errs[:1500]})
        raise RuntimeError(f"all {n} workers failed — first errors: {errs[:600]}")
    wf.phase("synthesize")
    synthesis = wf.agent(
        f"Synthesize the {len(ok)} worker outputs below into one final answer to the "
        f"task. Report what was covered AND what was not ({n - len(ok)} of {n} "
        f"workers failed).\n\nTASK:\n{task}\n\n"
        + "\n\n".join(f"[worker {i + 1}]\n{o}" for i, o in enumerate(ok)),
        label="synthesis", phase="synthesize")
    wf.write_ledger(run_dir, {
        "Scope": task,
        "Coverage": f"{len(ok)}/{n} workers succeeded"
                    + (f"; {n - len(ok)} FAILED (see results/workers.json)" if len(ok) < n else ""),
        "Findings": synthesis[:4000],
        "Verification": f"tokens used: {wf.tokens_used()}",
    })
    return {"result": synthesis, "workers_ok": len(ok), "workers_failed": n - len(ok),
            "worker_errors": errors, "run_dir": run_dir}
