#!/usr/bin/env python3
"""mcp_frontdoor.py — in-session MCP front door for the ultracode kit.

Claude Code's Workflow tool returns a runId IMMEDIATELY, runs in the background,
and exposes live progress; the model never sits blind inside a blocking tool
call. This server gives Codex the same contract on top of codex_workflow.py /
codex_patterns.py:

  ultracode_run(task, n?, effort?)          -> {run_id, status: "running"} instantly;
                                               n angle-diverse workers + synthesis run
                                               in a background thread.
  ultracode_review(dimensions, target?)     -> same async contract; one reviewer per
                                               dimension, findings adversarially
                                               verified (review_then_verify).
  ultracode_workflow(name, args?)           -> run a SAVED workflow by name (builtins:
                                               fanout, review; project workflows in
                                               .codex/ultracode/workflows/) — same
                                               async contract, shared caps/budget.
  workflow_status(run_id)                   -> live progress: state, workers done/
                                               failed (WITH per-worker error text),
                                               tokens spent, result when finished,
                                               ledger on disk for the audit trail.

Push updates (MCP resources): every run is a resource (ultracode://runs/<run_id>).
resources/subscribe to one and the server emits notifications/resources/updated as
workers finish and when the run completes — streaming progress without polling, for
clients that support subscriptions (poll workflow_status otherwise). '+500k' in a
task applies the Workflow-tool budget directive to CODEX_WF_BUDGET (shared pool).

Design rules (Workflow-tool parity):
  - The fan-out tools NEVER block: a run is a daemon thread; the tool result is the
    run_id. A model that waited minutes blind and terminated the call — losing the
    run — cannot happen here; after a disconnect the ledger under
    .codex/ultracode/runs/<run_id>/ still has everything.
  - Per-worker failures are captured as text in workers.json and in status output —
    a dead worker is diagnosable, never a silent {failed: N, outputs: []}.
  - Zero dependencies: a minimal JSON-RPC 2.0 / MCP stdio loop (newline-delimited),
    stdlib only, same philosophy as the rest of the kit.

Register in ~/.codex/config.toml (adjust paths; CODEX_WF_CODEX points at a codex
binary that your endpoint security actually lets run — e.g. the Codex.app one):

  [mcp_servers.ultracode]
  command = "python3"
  args = ["/path/to/codex-ultracode-kit/orchestrator/mcp_frontdoor.py"]
  startup_timeout_sec = 60.0
  tool_timeout_sec = 60.0        # tools return instantly; no 30-minute blind waits

  [mcp_servers.ultracode.env]
  CODEX_WF_CODEX = "/Applications/Codex.app/Contents/Resources/codex"
  CODEX_WF_CWD = "/path/to/your/project"
  CODEX_WF_EFFORT = "medium"
  ULTRACODE_MAX_FANOUT = "6"
"""
import json
import os
import re
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_patterns as cp  # noqa: E402
import codex_workflow as wf  # noqa: E402

MAX_FANOUT = int(os.environ.get("ULTRACODE_MAX_FANOUT", "6"))
PROTOCOL_FALLBACK = "2024-11-05"

CAPABILITIES = {"tools": {}, "resources": {"subscribe": True, "listChanged": True}}

_runs_lock = threading.Lock()
_runs = {}  # run_id -> state dict (this process's live runs; disk is the cold path)
_subs = set()  # subscribed resource URIs (ultracode://runs/<run_id>)
_out_lock = threading.Lock()  # daemon threads emit notifications — writes must not interleave
# a ceiling the USER configured at server start is never overridden by directives
_USER_BUDGET = bool(int(os.environ.get("CODEX_WF_BUDGET", "0") or "0"))


def _uri(run_id):
    return f"ultracode://runs/{run_id}"


def _notify(method, params):
    """Server->client notification (no id). Push parity with the Workflow tool:
    subscribers get resources/updated as workers finish and when the run completes."""
    with _out_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method,
                                     "params": params}) + "\n")
        sys.stdout.flush()


def _push_update(run_id):
    if _uri(run_id) in _subs:
        _notify("notifications/resources/updated", {"uri": _uri(run_id)})


# --------------------------------------------------------------------------- runs

def _new_run(task, kind):
    run_dir = wf.start_run(task, mode=kind)
    run_id = os.path.basename(run_dir)
    state = {
        "run_id": run_id, "run_dir": run_dir, "kind": kind, "task": task,
        "state": "running", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": 0, "done": 0, "failed": 0, "workers": [],  # [{index, ok, error?}]
        "result": None, "error": None,
        "lock": threading.Lock(),  # workers mutate counters concurrently
    }
    with _runs_lock:
        _runs[run_id] = state
    _notify("notifications/resources/list_changed", {})
    return state


def _finish(state, result=None, error=None):
    try:  # a non-JSON-serializable workflow result must not poison every future status
        json.dumps(result)
    except (TypeError, ValueError):
        result = {"repr": repr(result)[:2000],
                  "note": "workflow returned a non-JSON-serializable value"}
    with state["lock"]:
        state["result"] = result
        state["error"] = error
        state["state"] = "failed" if error else "completed"
    # don't clobber the richer workers.json a saved workflow writes for its own run
    wj = os.path.join(state["run_dir"], "results", "workers.json")
    if state["kind"] != "workflow" or not os.path.exists(wj):
        wf.save_result(state["run_dir"], "workers", {
            "requested": state["total"], "succeeded": state["done"],
            "failed": state["failed"], "workers": state["workers"],
        })
    _push_update(state["run_id"])  # completion push for subscribers


def _run_fanout(state, n, effort):
    """n angle-diverse workers + one synthesis, per the multi-modal-sweep pattern.
    Runs on a daemon thread; every worker failure is captured as text."""
    task, run_dir = state["task"], state["run_dir"]
    state["total"] = n

    def worker(k):
        try:
            out = wf.agent(
                f"{task}\n\nYou are worker {k + 1} of {n}. Take the {k + 1}-th distinct "
                f"angle on this task — do not duplicate the obvious first approach when "
                f"k > 1. Return raw findings/data, not polished prose.",
                effort=effort, label=f"worker-{k + 1}", phase="fan-out")
            with state["lock"]:
                state["workers"].append({"index": k + 1, "ok": True})
                state["done"] += 1
            wf.save_result(run_dir, f"worker_{k + 1}", {"index": k + 1, "output": out})
            _push_update(state["run_id"])  # streaming progress for subscribers
            return out
        except Exception as e:  # captured, never silent
            with state["lock"]:
                state["workers"].append({"index": k + 1, "ok": False,
                                         "error": f"{type(e).__name__}: {e}"})
                state["failed"] += 1
            _push_update(state["run_id"])
            raise

    try:
        outs = wf.parallel([(lambda k=k: worker(k)) for k in range(n)])
        ok = [o for o in outs if o is not None]
        if not ok:
            errs = "; ".join(str(w.get("error")) for w in state["workers"] if not w.get("ok"))
            wf.write_ledger(run_dir, {"Scope": task, "Coverage": f"0/{n} workers succeeded",
                                      "Findings": "none — all workers failed",
                                      "Unresolved risks": errs[:1500]})
            _finish(state, error=f"all {n} workers failed — first errors: {errs[:600]}")
            return
        synthesis = wf.agent(
            f"Synthesize the {len(ok)} worker outputs below into one final answer to the "
            f"task. Report what was covered AND what was not ({state['failed']} of {n} "
            f"workers failed).\n\nTASK:\n{task}\n\n" +
            "\n\n".join(f"[worker {i + 1}]\n{o}" for i, o in enumerate(ok)),
            label="synthesis", phase="synthesize")
        wf.write_ledger(run_dir, {
            "Scope": task,
            "Coverage": f"{state['done']}/{n} workers succeeded"
                        + (f"; {state['failed']} FAILED (see results/workers.json)"
                           if state["failed"] else ""),
            "Findings": synthesis[:4000],
            "Verification": f"tokens used: {wf.tokens_used()}",
        })
        _finish(state, result=synthesis)
    except Exception as e:
        _finish(state, error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")


def _run_review(state, dimensions, target):
    try:
        find_schema = {"type": "object", "properties": {"findings": {
            "type": "array", "items": {"type": "object", "properties": {
                "file": {"type": "string"}, "line": {"type": "integer"},
                "claim": {"type": "string"}, "severity": {"type": "string"}}}}}}
        state["total"] = len(dimensions)
        confirmed = cp.review_then_verify(dimensions, find_schema, target=target,
                                          run_dir=state["run_dir"])
        state["done"] = state["total"]
        _finish(state, result={"confirmed": confirmed})
    except Exception as e:
        _finish(state, error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")


def _status(run_id):
    if not re.fullmatch(r"[\w.-]+", run_id or ""):
        return {"run_id": run_id, "found": False, "error": "invalid run_id"}
    with _runs_lock:
        state = _runs.get(run_id)
    if state is not None:
        with state["lock"]:
            out = {k: (list(state[k]) if k == "workers" else state[k])
                   for k in ("run_id", "kind", "state", "started", "total",
                             "done", "failed", "workers", "result", "error")}
        out["found"] = True
        out["tokens_used"] = wf.tokens_used()
        return out
    # cold path: a run dir from a previous server process — report what disk knows
    d = os.path.join(wf.RUNS_ROOT, run_id)
    if not os.path.isdir(os.path.realpath(d)) or \
            not os.path.realpath(d).startswith(os.path.realpath(wf.RUNS_ROOT) + os.sep):
        return {"run_id": run_id, "found": False, "error": "run not found"}

    def read(p):
        try:
            with open(p) as f:
                return f.read()
        except OSError:
            return None
    workers = read(os.path.join(d, "results", "workers.json"))
    return {"run_id": run_id, "found": True, "state": "unknown (not this server process)",
            "run": read(os.path.join(d, "run.json")),
            "workers": json.loads(workers) if workers else None,
            "ledger": read(os.path.join(d, "ledger.md"))}


# ---------------------------------------------------------------------- MCP tools

TOOLS = [
    {
        "name": "ultracode_run",
        "description": (
            "Deterministic multi-agent fan-out (ultracode): n angle-diverse codex workers "
            "+ synthesis. Returns {run_id} IMMEDIATELY and runs in the background — poll "
            "workflow_status(run_id) for progress and the final result. Workers cannot "
            "run shell commands; verification stays with you."),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "the full task, self-contained; a "
                     "standalone first/last '+500k' token grants that token budget"},
            "n": {"type": "integer", "description": f"worker count, 1..{MAX_FANOUT}"},
            "effort": {"type": "string", "description": "per-worker reasoning effort override"},
            "budget": {"type": "integer", "description": "token budget grant (wins over "
                       "any '+500k' in the task text)"},
        }, "required": ["task"]},
    },
    {
        "name": "ultracode_review",
        "description": (
            "Ultracode review: one reviewer per dimension, every finding adversarially "
            "verified by independent skeptics before it survives. Returns {run_id} "
            "IMMEDIATELY; poll workflow_status(run_id). Result lists confirmed findings "
            "with a coverage-honest ledger."),
        "inputSchema": {"type": "object", "properties": {
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "target": {"type": "string", "description": "what to review (path or description)"},
            "budget": {"type": "integer", "description": "token budget grant for this run"},
        }, "required": ["dimensions"]},
    },
    {
        "name": "ultracode_workflow",
        "description": (
            "Run a SAVED ultracode workflow by name (builtins: 'fanout', 'review'; project "
            "workflows live in .codex/ultracode/workflows/<name>.py defining run(args)). "
            "Returns {run_id} IMMEDIATELY and runs in the background — poll "
            "workflow_status(run_id). Shares the process's concurrency cap and token budget."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "workflow name or path"},
            "args": {"type": "object", "description": "passed to the workflow's run(args); "
                     f"an integer args.n is clamped to ULTRACODE_MAX_FANOUT ({MAX_FANOUT})"},
            "budget": {"type": "integer", "description": "token budget grant for this run"},
        }, "required": ["name"]},
    },
    {
        "name": "workflow_status",
        "description": ("Live status of an ultracode run: state (running/completed/failed), "
                        "workers done/failed with per-worker error text, tokens used, and "
                        "the final result once finished. Runs are also MCP resources "
                        "(ultracode://runs/<run_id>) — subscribe for push updates instead "
                        "of polling, if your client supports resource subscriptions."),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}}, "required": ["run_id"]},
    },
]


def _apply_budget_directive(text=None, explicit=None):
    """Grant a token budget (Workflow-tool budget directive parity): an explicit
    `budget` tool arg wins; else a standalone '+500k' first/last token of the task
    text. The grant is RELATIVE — ceiling = current meter + b — because this server
    is long-lived and the engine's meter accumulates across runs; an absolute value
    would let earlier runs' spend instantly exhaust a later grant. Later directives
    re-arm the ceiling. A ceiling the user configured at server start is never
    overridden."""
    b = int(explicit) if isinstance(explicit, (int, float)) and explicit > 0 \
        else wf.parse_budget_directive(text)
    if not b or _USER_BUDGET:
        return None
    os.environ["CODEX_WF_BUDGET"] = str(b + wf.tokens_used())
    wf.log(f"budget directive: +{b} tokens from now (ceiling {b + wf.tokens_used()}; "
           f"pool shared across runs)")
    return b


def _run_saved(state, name, wargs):
    try:
        out = wf.workflow(name, wargs)
        # the workflow's META loaded AFTER our start_run wrote run.json — attribute it
        # to THIS run and consume it so it can't leak into a later run
        if wf._meta_state:
            rj = os.path.join(state["run_dir"], "run.json")
            try:
                rec = json.load(open(rj))
                rec["meta"] = dict(wf._meta_state)
                with open(rj, "w") as f:
                    json.dump(rec, f, indent=2)
            except (OSError, json.JSONDecodeError):
                pass
            wf._meta_state.clear()
        with state["lock"]:
            # lift real worker stats when the workflow reports them (fanout shape)
            if isinstance(out, dict) and "workers_ok" in out:
                state["done"] = int(out.get("workers_ok") or 0)
                state["failed"] = int(out.get("workers_failed") or 0)
                state["total"] = state["done"] + state["failed"]
                state["workers"] = (
                    [{"index": e.get("index"), "ok": False, "error": e.get("error")}
                     for e in (out.get("worker_errors") or [])]
                    + [{"index": None, "ok": True}] * state["done"])
            else:
                state["done"] = state["total"] = max(state["total"], 1)
        _finish(state, result=out)
    except Exception as e:
        _finish(state, error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")


def _call_tool(name, args):
    if name == "ultracode_run":
        task = (args.get("task") or "").strip()
        if not task:
            return {"error": "task is required"}
        budget_applied = _apply_budget_directive(task, explicit=args.get("budget"))
        n = max(1, min(int(args.get("n") or min(4, MAX_FANOUT)), MAX_FANOUT))
        effort = args.get("effort")
        state = _new_run(task, "run")
        t = threading.Thread(target=_run_fanout, args=(state, n, effort), daemon=True)
        t.start()
        out = {"run_id": state["run_id"], "status": "running", "workers": n,
               "next": "poll workflow_status(run_id); the run also persists under "
                       f".codex/ultracode/runs/{state['run_id']}/"}
        if budget_applied:
            out["budget"] = budget_applied
        return out
    if name == "ultracode_review":
        dims = [str(d) for d in (args.get("dimensions") or []) if str(d).strip()]
        if not dims:
            return {"error": "dimensions is required (non-empty array)"}
        # explicit budget arg only — a review TARGET is a path/description, not a
        # message that carries directives; prose parsing there caused false ceilings
        _apply_budget_directive(explicit=args.get("budget"))
        state = _new_run(f"review {args.get('target') or 'the code'} on: {', '.join(dims)}",
                         "review")
        t = threading.Thread(target=_run_review,
                             args=(state, dims, args.get("target") or "the code in this directory"),
                             daemon=True)
        t.start()
        return {"run_id": state["run_id"], "status": "running", "dimensions": dims,
                "next": "poll workflow_status(run_id)"}
    if name == "ultracode_workflow":
        wname = (args.get("name") or "").strip()
        if not wname:
            return {"error": "name is required"}
        try:
            wf._resolve_workflow(wname)  # fail fast on unknown names, before minting a run
        except ValueError as e:
            return {"error": str(e)}
        wargs = dict(args.get("args") or {})
        # directive from the workflow's task STRING only (never json.dumps blobs),
        # plus the explicit budget arg
        _apply_budget_directive(wargs.get("task") if isinstance(wargs.get("task"), str)
                                else None, explicit=args.get("budget"))
        if isinstance(wargs.get("n"), (int, float)):
            wargs["n"] = max(1, min(int(wargs["n"]), MAX_FANOUT))  # MCP-layer worker cap
        state = _new_run(f"workflow {wname}", "workflow")
        wargs.setdefault("run_dir", state["run_dir"])  # one run dir, shared with the workflow
        t = threading.Thread(target=_run_saved, args=(state, wname, wargs), daemon=True)
        t.start()
        return {"run_id": state["run_id"], "status": "running", "workflow": wname,
                "next": "poll workflow_status(run_id)"}
    if name == "workflow_status":
        return _status(args.get("run_id") or "")
    return {"error": f"unknown tool {name!r}"}


# ------------------------------------------------------------------ JSON-RPC loop

def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    with _out_lock:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _run_ids():
    with _runs_lock:
        ids = list(_runs)
    if os.path.isdir(wf.RUNS_ROOT):
        ids += [d for d in os.listdir(wf.RUNS_ROOT)
                if d not in ids and os.path.isdir(os.path.join(wf.RUNS_ROOT, d))]
    return ids


def _handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        _reply(msg_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_FALLBACK,
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": "ultracode-kit", "version": "3.1"},
        })
    elif method == "notifications/initialized":
        pass  # notification — no response
    elif method == "ping":
        _reply(msg_id, {})
    elif method == "tools/list":
        _reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            res = _call_tool(params.get("name"), params.get("arguments") or {})
            is_err = isinstance(res, dict) and set(res) == {"error"}
            _reply(msg_id, {"content": [{"type": "text", "text": json.dumps(res)}],
                            "isError": bool(is_err)})
        except Exception as e:
            _reply(msg_id, {"content": [{"type": "text", "text": json.dumps(
                {"error": f"{type(e).__name__}: {e}"})}], "isError": True})
    elif method == "resources/list":
        _reply(msg_id, {"resources": [
            {"uri": _uri(r), "name": f"ultracode run {r}", "mimeType": "application/json"}
            for r in _run_ids()]})
    elif method == "resources/read":
        rid = str(params.get("uri") or "").rsplit("/", 1)[-1]
        _reply(msg_id, {"contents": [{"uri": _uri(rid), "mimeType": "application/json",
                                      "text": json.dumps(_status(rid))}]})
    elif method == "resources/subscribe":
        uri = str(params.get("uri") or "")
        rid = uri.rsplit("/", 1)[-1]
        # a malformed/unknown URI must error, not "subscribe" to something that will
        # never notify
        if not uri.startswith("ultracode://runs/") or not _status(rid).get("found"):
            _reply(msg_id, error={"code": -32002, "message": f"resource not found: {uri}"})
        else:
            _subs.add(uri)
            _reply(msg_id, {})
    elif method == "resources/unsubscribe":
        _subs.discard(str(params.get("uri") or ""))
        _reply(msg_id, {})
    elif msg_id is not None:  # unknown REQUEST -> method-not-found; ignore notifications
        _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    sys.stderr.write("[ultracode-kit-mcp] stdio server ready\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(None, error={"code": -32700, "message": "parse error",
                                "data": {"line": line[:200]}})
            continue
        if not isinstance(msg, dict):
            # valid JSON but not a request object (e.g. a '[]' batch) — reject it;
            # it must never crash a server with in-flight runs
            _reply(None, error={"code": -32600, "message": "invalid request: expected a "
                                "JSON-RPC object (batches unsupported)"})
            continue
        try:
            _handle(msg)
        except Exception as e:  # a handler bug must not kill the server
            sys.stderr.write(f"[ultracode-kit-mcp] handler error: {e}\n")
            if msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
