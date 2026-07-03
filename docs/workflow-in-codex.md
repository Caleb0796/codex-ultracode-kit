# Simulating Claude Code's Workflow tool in Codex

## The core insight (verified against Codex's Rust source)

Claude Code's "dozens of agents" do **not** come from the model deciding to spawn them. They come from a deterministic script — `parallel()` / `pipeline()` loops where the *harness* picks the concurrency number. Codex's in-session `spawn_agent` is the opposite: **model-driven and trained to be conservative.** The official docs say "Codex only spawns a new agent when you explicitly ask," and the canonical examples spawn 2-6. No prompt wording reliably gets you to dozens, because under-spawning is intended behavior, not a bug.

So you don't simulate the Workflow tool with config or prompting. You simulate it with an **external orchestrator that holds the concurrency number in code** and uses `codex exec` as the agent primitive. That removes the model from the fan-out decision entirely.

## The mapping

| Workflow tool | Codex equivalent |
|---|---|
| `agent(prompt)` → text | `codex exec "prompt" -o out.txt` → read out.txt |
| `agent(prompt, {schema})` → validated object | `codex exec ... --output-schema s.json -o out.txt` → parse |
| `parallel(thunks)` (barrier, ~16 concurrent) | thread pool of N `codex exec` processes |
| `pipeline(items, s1, s2)` (no barrier) | per-item chains in the pool |
| concurrency cap min(16, cores-2) | your `N` (xargs -P N / pool max_workers) |
| worktree isolation for writers | `git worktree add` per task |

## The harness — `codex_workflow.py` (verified working)

A near-direct port of the Workflow API. `agent()` / `parallel()` / `pipeline()` with the same semantics, backed by `codex exec` subprocesses and a `ThreadPoolExecutor`.

```python
import codex_workflow as wf
res = wf.parallel([lambda: wf.agent("...", schema=S), lambda: wf.agent("...", schema=S)])
```

Knobs via env: `CODEX_WF_CONCURRENCY` (default min(16, cores−2)), `CODEX_WF_MODEL`, `CODEX_WF_EFFORT`, `CODEX_WF_CWD`, `CODEX_WF_TIMEOUT`, `CODEX_WF_BUDGET`, `CODEX_WF_MAX_AGENTS`, `CODEX_WF_RESUME`, `CODEX_WF_CACHE`, `CODEX_WF_RUNS`.

**Verified on this machine (Codex 0.125.0, 2026-06-13):**
- 2 parallel schema agents → `[{'answer': 42}, {'answer': 72}]` — PASS
- 8 agents at concurrency 8, each schema-validated → `[1,4,9,16,25,36,49,64]` in 22s — PASS
- 2 parallel **writer** agents in isolated worktrees → each edited only its own file, no collision, worktrees+branches cleaned up — PASS

This took a real bug fix to reach. The harness passed at 2 agents but hung at 8. Control tests proved Codex itself runs 8 concurrent `codex exec` fine (~22s), isolating the bug to the harness: `subprocess.run(capture_output=True)` pipes stdout/stderr, and `codex exec` emits enough banner/progress text that under concurrency the ~64KB OS pipe buffers fill and `subprocess.run` deadlocks. **Fix: redirect child output to a file, never a pipe** (the result comes from `-o` anyway). If you hand-roll your own orchestrator, do the same — this is the trap that makes "works at 2, hangs at 8."

### Five real bugs found and fixed during testing

These are `codex exec` gotchas the harness now handles; you'd hit all of them hand-rolling it:

1. **`-a never` is a GLOBAL flag.** `codex exec -a never` → `error: unexpected argument '-a' found`. Must be `codex -a never exec ...`. (Note: a cloud-managed policy on your account overrides it to `OnRequest` regardless — read-only sandbox runs fine anyway.)
2. **Strict schema.** OpenAI structured output requires `"additionalProperties": false` and all properties in `required` on *every* object, or you get `400 invalid_json_schema`. The harness auto-injects both — you write a normal JSON Schema.
3. **MCP slows/breaks exec.** Every `codex exec` boots your configured MCP servers — startup cost × N agents, and `--output-schema` can misbehave when any MCP server is active. The harness passes `-c mcp_servers={}` to run clean.
4. **`wait -n` needs bash ≥4.3; macOS ships 3.2.** That's why the harness uses a Python thread pool, not bash job control.
5. **Notify hooks silently kill concurrent execs.** If your config has a notify/turn-ended hook, it can linger long after a run (~29 min observed) and was implicated in a silent exec death under parallel load. The harness passes `-c notify=[]` to every child. Do the same if you hand-roll.

## When to use which of the three layers

| Need | Tool | Concurrency |
|---|---|---|
| In-session, model coordinates, 2-6 agents | ultracode skill (`spawn_agent`) | capped by `[agents] max_threads` (default **6**) |
| In-session homogeneous fan-out | `spawn_agents_on_csv` | min(max_concurrency≤64, **max_threads**) |
| **Dozens of agents, deterministic** | **`codex_workflow.py` (external)** | **your N** — bounded by CPU/RAM + API rate limits, not the model |

### Raise the in-session ceiling too (optional)

Your `~/.codex/config.toml` has no `[agents]` section, so you're on `max_threads = 6`. Critically, **`spawn_agents_on_csv` is clamped to `max_threads`** — even CSV fan-out tops out at 6 right now. To lift both:

```toml
[agents]
max_threads = 16        # default 6; no hard upper cap (honored verbatim). caps CONCURRENT live agents tree-wide
max_depth = 1           # default 1; raise only if you want agents that spawn agents (recursion ≠ more concurrency)
# job_max_runtime_seconds = 3600   # default 1800s per CSV worker
```
Note: completed agents keep their thread slot until `close_agent` (a known leak, #22779), so the skill must close finished agents to reclaim slots. And don't enable `multi_agent_v2` — it rejects `max_threads` and caps lower.

But even at `max_threads = 16`, in-session spawning stays model-conservative. For genuine "10s of agents," use the external harness — that's the real Workflow-tool equivalent.

## Bash alternative (no Python)

For embarrassingly-parallel batches, GNU `parallel` or `xargs -P N` driving `codex exec`:

```bash
# per-file review, 8 concurrent, read-only
ls src/**/*.ts | xargs -P 8 -I{} \
  codex -a never -s read-only exec --skip-git-repo-check -c 'mcp_servers={}' \
  "Review {} and write findings to {}.review"
```

For parallel *writers*, give each task its own git worktree (so they don't collide on the index), pre-install deps in the orchestrator, and assign a unique port per worktree. Route all dependency/lockfile/migration changes through a single coordinator task — those are globally ordered and will conflict.

## Parity with Claude ultracode — what matches, what's filled, what can't be

An adversarial audit compared this harness feature-by-feature against the real Workflow tool. Verdict: the **engine and methodology now match; three harness-level features cannot be replicated with `codex exec`.**

**Matched (core — the load-bearing 80%):** `agent` / `parallel` (barrier, allSettled→None) / `pipeline` (no barrier, `(prev,item,index)` stages, throw→None) / schema-validated structured output / concurrency cap (default min(16, cores−2)) / the runaway backstops (lifetime agent cap via `CODEX_WF_MAX_AGENTS`, default 1000; a 4096-item per-call cap that raises instead of truncating) / `phase()` + per-agent `label=` progress tags / the budget object (`wf.budget.total/spent()/remaining()` for loop-until-budget and fleet scaling; exhaustion raises `BudgetExceeded`, never retried, and batches report "BUDGET EXHAUSTED — N thunks did not run" distinctly from agent failures). Plus `codex_patterns.py` ports the ultracode *methodology*: `adversarial_verify` (N skeptics refute, majority survives; a dead skeptic is 'unverified', never a silent kill), `judge_panel` (multi-judge scores + synthesis grafting runners-up), `loop_until_dry` (logs when capped), `multi_modal_sweep`, `completeness_critic` (gaps become next-round work), `review_then_verify` (coverage-honest ledger). The patterns are what make it "ultracode" rather than a parallel `map`.

**Filled after the audits caught them as defects in this harness:**
- **Worktree isolation** for parallel writers — fresh worktree per agent, returns the diff; leak-proof on retry; a no-change worktree is removed immediately. (Verified: 2 writers, no collision.) The cleanup contract: failure-path and no-change cleanup is automatic; after **applying** diffs (use `wf.apply_diff(diff)` — empty-diff no-op, `--3way`, reports conflicted paths) call `wf.cleanup_worktrees()` yourself to remove the surviving worktrees *and* `cxwf/*` branches.
- **Real schema validation** — was a buggy greedy `{.*}` regex with no validation; now escape-correct balanced-brace extraction that skips prose braces and validates candidates by parsing, + `jsonschema.validate` with retry-on-mismatch (true Workflow parity). Falls back gracefully if `jsonschema` isn't installed.
- **Budget ceiling** — `CODEX_WF_BUDGET` token cap; `agent()` raises `BudgetExceeded` once exceeded (was a read-only meter).
- **Resume** — `CODEX_WF_RESUME=<run dir>` journals every **read-only** agent result by content+occurrence: N identical redundant calls (independent skeptic votes) stay N distinct entries, an edited prompt hash-misses and runs live, and resuming after the repo's HEAD moved is refused (`CODEX_WF_RESUME_FORCE=1` overrides, loudly). Unlike Claude Code's prefix journal it does not auto-invalidate calls *after* an edited one — resume from the same HEAD or start a fresh run dir. Writer/worktree agents always run live: replaying a writer's text without re-applying its edits would silently return an empty diff, so the journal (and the legacy `CODEX_WF_CACHE` memoizer) covers read-only agents only.

- **Background runs + progress (in-session)** — `orchestrator/mcp_frontdoor.py` registers as an MCP server: `ultracode_run`/`ultracode_review` return a `run_id` IMMEDIATELY (Workflow-tool contract), the fan-out runs on a daemon thread over this same engine, and `workflow_status(run_id)` reports state, workers done/failed **with per-worker error text**, tokens, and the final result. A terminated/dropped tool call no longer loses the run — the ledger under `.codex/ultracode/runs/<run_id>/` survives. (No live tree UI like `/workflows`; polling stands in for streaming.)

- **Saved/named workflows** — `wf.workflow(name, args)` runs a `.py` defining `run(args)` (+ optional `META = {name, description, phases}`), resolved from `.codex/ultracode/workflows/` then the kit's builtins (`fanout`, `review`); it executes in-process so it shares the concurrency semaphore, agent counter, and token budget by construction; one nesting level (a child calling `workflow()` raises). In-session: the front door's `ultracode_workflow` tool. Determinism guard (spec parity): under `CODEX_WF_RESUME`, workflow sources calling `time.time`/`datetime.now`/`random.*`/`uuid.uuid4` are refused — pass timestamps/seeds via args.
- **Prefix-invalidation resume** — default `CODEX_WF_RESUME_MODE=prefix`: while RESUMING (the journal already has entries), the first miss (an edited/new call) switches the REST of the run live, so nothing recorded after an edit ever replays (conservative under threads: wall-clock-after, never stale). `content` mode keeps pure content+occurrence replay. Limits vs the spec's journal: DELETING a call isn't detected (the remaining calls still hash-hit) — start a fresh run dir after structural edits.
- **Tool-call-layer schema retry** — when an agent's output fails schema validation, one cheap REPAIR call feeds the invalid output + validation error back instead of re-running the whole agent (`CODEX_WF_SCHEMA_REPAIR=0` disables); a failed repair falls into the normal retry loop.
- **`meta` contract** — `wf.meta(name=, description=, phases=)` (or a saved workflow's `META` dict) lands in `run.json`, and `write_ledger` auto-records the `phase()` trail as a Phases section.
- **Budget directives** — `wf.parse_budget_directive("+500k") -> 500000` (a STANDALONE first/last token only, so prose like "adds +2k lines" never fires; sub-1000 ignored). The front door applies it — or an explicit `budget` tool arg, which wins — as a RELATIVE grant (`ceiling = meter + b`, since its process is long-lived and the meter accumulates across runs); later directives re-arm, and a user-configured ceiling is never overridden.
- **Push progress (MCP resources)** — every run is a resource (`ultracode://runs/<id>`); `resources/subscribe` gets `notifications/resources/updated` per worker completion and on finish. Caveat: depends on the MCP client supporting subscriptions — `workflow_status` polling always works.
- **Session-wide toggle** — a `ultracode: always` line in the project's `AGENTS.md` treats every substantive task as activated; `$ultracode on`/`off` add/remove it (see examples/AGENTS.md).

**Cannot be replicated (Claude Code *harness* features, not exposed by `codex exec`):**
- **The `/workflows` live tree UI** — there is no UI surface inside a Codex session; resource subscriptions + `workflow_status` polling are the substitute.
- **MCP access for agents** — Workflow agents reach session MCP tools; this harness runs agents MCP-clean by default (`-c mcp_servers={}`) for speed/determinism. Audit caveat: that flag only *partially* suppresses configured servers (they still attempt to connect) — a deliberate divergence, not full parity.

**Environment constraint (neither tool can change):** your account's cloud policy forces approvals to `OnRequest`, so `-a never` is silently downgraded. Consequence — orchestrated agents can **edit files** (in-sandbox writes need no approval) but **cannot run shell commands** (tests/builds get auto-rejected: *"command execution approval is not supported in exec mode"*). So writer agents are instructed to edit only, and **verification (running tests) must happen in the orchestrator** after collecting diffs — which is the correct division of labor anyway. Note `git add -A` in a worktree also sweeps incidental untracked files (e.g. SCM logs); scope diffs to intended paths when applying.

## Realistic ceilings

- **Per-job CSV hard cap: 64** concurrent workers (`MAX_AGENT_JOB_CONCURRENCY`), and only if `max_threads ≥ 64`.
- **External harness:** concurrency is your `CODEX_WF_CONCURRENCY`, with two runaway backstops (Workflow-tool parity): a **lifetime cap of 1000 agents** per process (`CODEX_WF_MAX_AGENTS`; `<=0` disables) and a **4096-item cap** per `parallel()`/`pipeline()` call that raises an explicit error rather than truncating. Below those, the real limit is your **API rate limits** (TPM/RPM) and RAM (~8-10 GB per agent that runs a dev server/tests). Lightweight read-only agents scale to dozens; heavy build/test agents top out at 3-5 on a laptop.
- **Review capacity is the true bottleneck** at scale, not compute. Pin a finite N; never `xargs -P 0`.
