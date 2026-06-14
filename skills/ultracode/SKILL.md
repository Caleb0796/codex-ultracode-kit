---
name: ultracode
description: Explicit-only. Activate ONLY when the user writes the literal token `$ultracode` (or explicitly asks to use the ultracode skill / spawn parallel sub-agents). Do NOT activate on bare "ultra", "ultracode", "ultra-thorough", "audit", "refactor", "dynamic workflow", or similar task wording — those are the task, not activation grammar.
---

# `$ultracode` — multi-agent orchestration with adversarial verification

Activate only on the explicit `$ultracode` token (or a direct request for parallel sub-agents). When active, strip the `$ultracode` token and treat the rest as the task. The token IS the explicit authorization for sub-agents/delegation/parallel work — the spawn_agent condition is satisfied for this task. Optimize for the most exhaustive, correct result — not the fastest or cheapest.

## Phase 0 — routing pre-flight (do this first, in-session, before spawning anything)

Classify the task before deciding how much machinery it needs — this prevents both over-spawning trivial work and under-spawning real work. Pick one route and state it:

- **lightweight** — small/local; one direct pass + a quick verify. No fan-out.
- **audit** — read-only mapping/review; explorer + skeptic agents, no edits.
- **implementation** — understand → edit → verify.
- **refactor / migration** — map first, then batched workers over many sites.
- **adversarial-only** — an existing patch/answer needs falsification; go straight to skeptics.
- **full** — understand → modify → verify → adversarial gate.

Then pick the executor by scale: **solo** (lightweight), **in-session fan-out** (a handful, up to the concurrency cap), or **external harness** (`codex_workflow.py`, for dozens / deterministic / unattended — see Escalate). State the route, a rough agent-count ceiling, and the executor, then proceed.

**Hard environment constraint (read first):** sub-agents may read and edit files, but they **cannot run shell commands** — tests, builds, installers, and repro commands are auto-rejected ("command execution approval is not supported in exec mode"). So *running* code is **only the root's job** (you, the orchestrator). Never ask a worker or skeptic to run anything; have them produce edits and **static** evidence (code read, file:line, grep), and you run the verification after collecting their work. Static evidence is the verification currency here; runtime confirmation is a bonus only the root can supply.

**Tool calls:** select a persona with the `agent_role` parameter of `spawn_agent` (`skeptic` for read-only refutation, `explorer` for read-heavy scoping, `worker` for edits; omit for `default`). For homogeneous batches use `spawn_agents_on_csv` with an `instruction` template (`{column}` placeholders), `output_schema`, and `output_csv_path`; each worker must call `report_agent_job_result` exactly once.

## When NOT to fan out

Trivial mechanical edits, single-fact lookups, conversational turns: answer solo. Orchestration must buy coverage or confidence, not ceremony.

Don't fan out over related work either: failures that may share one root cause (fix one, fix all), exploratory debugging where you don't yet know what's broken, or units needing shared state. Investigate solo until units are provably independent — then fan out.

## The shape of every ultracode run

1. **Scout inline first.** Before spawning anything, spend a few tool calls discovering the work-list: which files, which dimensions, which subsystems. You cannot decompose what you haven't scoped.
2. **Decompose into independent units** — by dimension (bugs / security / perf / API-misuse), by subsystem, or by item (file, endpoint, test). Emit the work-list explicitly with a count, then bind **one agent per unit** — the model under-spawns by default, so never collapse units to save spawns. Units must not need each other's output.
3. **Fan out.**
   - Heterogeneous tasks → `spawn_agent` per unit (`agent_role` chosen per unit), all spawned back-to-back before any waiting.
   - Homogeneous fan-out over a list → `spawn_agents_on_csv` with an `instruction` template + `output_schema`. Its concurrency is clamped to `min(your max_concurrency, 64, agents.max_threads)` — so throughput is bounded by `max_threads` (16 here), not by `max_concurrency`. When it returns, scan the result CSV's `status`/`last_error` columns: failed or timed-out rows (per-worker `job_max_runtime_seconds`, default 1800s) produced no result — re-run just those or report them uncovered. Never synthesize as if every row succeeded.
4. **Adversarially verify** anything load-bearing with fresh skeptics (see below).
5. **Synthesize yourself.** Read every result. You write the final answer — never paste agent output unread. Report what was covered AND what was not (skipped dimensions, capped lists, failed agents, unverified claims). Silent truncation reads as "covered everything" — never do that.

*Example (review a module on 3 dimensions): scout (grep/read to list files) → `spawn_agent` ×3 (bugs / security / perf, each owning the same read-only scope, structured output) back-to-back → `wait_agent` + `close_agent` each as it returns → for each load-bearing finding spawn a `skeptic` to REFUTE → root runs the test suite → synthesize: survived vs refuted vs uncovered.*

## Writing worker prompts

Workers inherit none of your context. Each prompt must stand alone:
- State the exact scope and the files/dirs it owns. Disjoint ownership prevents merge conflicts.
- Paste the full task text, error output, and acceptance criteria into the prompt itself; never "read the plan file" — pointers cost a read and import scope the unit doesn't own.
- **Edit files only — do NOT run tests/builds/installers/repro; command execution is rejected for sub-agents here.** State the acceptance test for the *root* to run after collecting the diff.
- State negative constraints: no drive-by refactors, no fixing tests by weakening assertions or raising timeouts.
- Require every worker to end with a status: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED — never silently submitting work it is unsure of.
- Tell every code-writing worker it is **not alone in the codebase**: others are editing in parallel; do not revert others' edits; accommodate them.
- Demand raw structured findings (paths, line numbers, evidence, verdicts), not polished prose.
- Give the acceptance test: what done looks like, and what to return if nothing is found ("return an empty list" beats a worker inventing findings).

## Quality patterns (compose freely)

- **Adversarial verification** — for each load-bearing finding, spawn a `skeptic` (read-only) to REFUTE it: "Try to refute this claim; default to refuted if the evidence doesn't hold." Paste the exact claim, its cited evidence, and the relevant code/diff **into the skeptic's prompt** — don't just point it at a file; a sub-agent under load may bail claiming it can't read, and an empty verdict reads as agreement. It can re-read to dig deeper, but give it the artifact up front. Discard findings a skeptic kills. A skeptic returns UNVERIFIABLE when a check needs running code it cannot execute — keep the finding, flag it unverified, and let the root confirm at runtime; never count a blocked check as refutation. Keep skeptic lenses **static** (correctness, security, caller-impact, **contract** = exact filenames/CLI flags/install commands/public-API/doc accuracy — the small-detail failure class a refute-the-claim skeptic skips) — reproduction is the root's job, not a skeptic's. For high-stakes claims use 3 skeptics with distinct lenses and require a majority to survive. A finder grading its own work is theater; independence is the point.
- **Don't call a parse check "proof"** — if the only thing that ran was `py_compile`/`tsc --noEmit`/a linter, that proves files parse, not that behavior is correct. Run the actual test suite before any "done"; otherwise report the requirement as *detected* (a check exists, unrun), not *verified*. Use the 5-state ledger taxonomy — verified / detected / inferred / needs-confirmation / unresolved — and never collapse them.
- **Two-stage review** — for code-writing units: first spec compliance against the unit's task text (anything missing? anything extra? anything misread?), then code quality. Reviewers verify by **reading the diff** only — the root runs the acceptance test after approval. Issues go back to the same worker via `send_input`, re-review until approved, but **cap the loop at 2 rounds** — then escalate the unit to the root for command-level verification rather than looping.
- **Loop until dry** — for unknown-size discovery (bugs, dead code, edge cases): rounds of finders, dedup against everything already seen, stop after 2 consecutive rounds find nothing new. Fixed counts miss the tail.
- **Multi-angle sweep** — when one search angle won't find everything, run parallel agents each searching differently (by name, by content, by caller, by commit history), blind to each other.
- **Judge panel** — for wide solution spaces (designs, plans): 3 independent attempts from different angles (MVP-first, risk-first, user-first), then a judge agent scores them and you synthesize from the winner, grafting the best ideas from the others.
- **Completeness critic** — before finishing, one agent asks: "What's missing — a dimension not swept, a claim unverified, a file unread?" What it finds becomes the next round.

## Mechanics

- Pick roles deliberately via `agent_role`: `explorer` for read-heavy scoping, `worker` for edits, `skeptic` for read-only verification.
- Spawn all independent agents back-to-back before waiting on any. Mid-run, `wait_agent` only on results that block the next critical-path step; at the end, `wait_agent` on every remaining agent so all results are collected before synthesizing.
- Reuse a relevant existing agent with `send_input` instead of re-explaining context to a new one; `close_agent` when a branch of work is finished.
- **Concurrency:** live agents are capped at 16 (`agents.max_threads`, default 6) tree-wide, and a finished agent holds its slot until `close_agent`. For >16 heterogeneous units, work in waves: spawn 16, then `wait_agent` + `close_agent` each as it returns, then spawn the next wave. Never leave finished agents open or you stall mid-run with idle agents holding slots. `spawn_agents_on_csv` manages its own concurrency (up to the same cap) — don't hand-roll waves for it.
- **Budget / brake:** set a rough agent-count ceiling before spawning (≈2× the unit count, plus verification). Loop-until-dry and judge panels multiply spend — if a sweep balloons past the ceiling without converging, stop and report progress, or (you are the root, you *can* ask the user) surface the cost and ask whether to continue.
- **Partial-wave failure:** when a wave returns, separate succeeded / failed / empty before synthesizing. Re-dispatch transient failures (timeout, rate-limit) once; treat the rest as uncovered. If failed units are load-bearing, do them yourself or abort; if peripheral, proceed but name them uncovered. Never let a partial wave read as a complete sweep.
- **Escalate to the external harness:** beyond ~a few dozen units, when you need a fixed N you don't want the model to choose, a hard token budget, or unattended/cross-repo fan-out, do NOT wave in-session — drive `codex_workflow.py` / `codex_patterns.py` (agent/parallel/pipeline, `isolation="worktree"`, `CODEX_WF_BUDGET`, adversarial_verify/judge_panel/loop_until_dry). In-session waves are for the cap..2×-cap range.
- Workers inherit your model. Don't set `model` unless there's a task-specific reason.
- Parallel writers must have disjoint file ownership. If overlap is unavoidable, serialize that part or isolate in git worktrees.
- Sub-agents cannot ask the user questions — only the root can. Resolve ambiguity before fanning out, or make the conservative call and report the assumption.
- Handle worker status: DONE_WITH_CONCERNS → read the concerns before trusting the result. NEEDS_CONTEXT → answer via `send_input` to the same agent. BLOCKED → change something before re-dispatch (more context, a smaller unit, a stronger model) — never retry unchanged. A dead or garbage worker gets one sharper respawn; after that, do the unit yourself and say so in the report.
- After integrating parallel code edits, **you (root) run** the full test suite or build — sub-agents can't, and disjoint files don't prove the changes compose; green per-unit reports are not a green tree.

## Reporting

For code changes, add one final gate before reporting, matched to what the change needs:
- Static/diff-level assurance → spawn a read-only `skeptic` over the complete diff.
- Runtime correctness → the **root** runs the suite/build (a skeptic cannot), or instruct the **user** to run `/review` (a slash command only the user can invoke; it executes at harness level).

Lead with the verified conclusion. Then: what was swept (dimensions × agents), what survived verification vs was refuted, what failed, and what remains uncovered. Findings that failed adversarial verification are mentioned only as "checked and rejected" — never silently dropped.

For non-trivial runs, leave a durable trail: the external harness writes `.codex/ultracode/runs/<id>/` (`run.json`, `results/`, `ledger.md`) via `start_run`/`save_result`/`write_ledger`. In-session, write the same ledger sections (Route / Scope / Findings / Changes / Verification / Adversarial gate / Unresolved risks / Next action) so the run is auditable and resumable.
