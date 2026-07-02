---
name: ultracode
description: Explicit-only. Activate ONLY when the message contains the literal token `$ultracode`, OR the user explicitly asks to spawn parallel sub-agents / run multi-agent work. Do NOT activate on bare "ultracode" (without the `$`), "ultra", "ultra-thorough", "audit", "refactor", "dynamic workflow", or similar task wording — those are the task, not activation grammar.
---

# `$ultracode` — multi-agent orchestration with adversarial verification

Activate on the literal `$ultracode` token, or an explicit request to spawn parallel sub-agents. Match the token literally even if escaped (`\$ultracode`) or punctuation-adjacent. Bare `ultracode` without the `$` does NOT activate. When active, strip the token and treat the rest as the task. The token IS the authorization for sub-agents/delegation — the spawn_agent condition is satisfied for this task. Optimize for the most exhaustive, correct result — not the fastest or cheapest.

**Read `reference.md` in this skill directory before fanning out** — it holds the worker-prompt rules, the quality patterns (adversarial verification, judge panel, loop-until-dry, multi-angle sweep, completeness critic, barrier discipline), and the concurrency/budget mechanics. This file holds only the core loop.

## Phase 0 — routing pre-flight (do this first, in-session, before spawning anything)

Classify the task before deciding how much machinery it needs — this prevents both over-spawning trivial work and under-spawning real work. Pick one route and state it:

- **lightweight** — small/local; one direct pass + a quick verify. No fan-out.
- **audit** — read-only mapping/review; explorer + skeptic agents, no edits.
- **implementation** — understand → edit → verify.
- **refactor / migration** — map first, then batched workers over many sites.
- **adversarial-only** — an existing patch/answer needs falsification; go straight to skeptics.
- **full** — understand → modify → verify → adversarial gate.

Then pick the executor by scale: **solo** (lightweight), **in-session fan-out** (cap and wave mechanics are in reference.md — don't restate a number here), or the **external harness** (`codex_workflow.py`) for large/deterministic/unattended fan-out. Calibrate depth to the ask, not just the route: "find any bugs" / a quick check → a few finders and a single-skeptic verify; "thorough" / "comprehensive" / "audit everything" → a larger finder pool, 3-skeptic-majority gates, and a synthesis stage; lean thorough for research/review/audit. A user-stated ceiling (agents or tokens) in the message is the hard budget. State the route and executor, then proceed.

**Hard environment constraint (read first):** sub-agents may read and edit files, but they **cannot run shell commands** — tests, builds, installers, and repro commands are auto-rejected ("command execution approval is not supported in exec mode"). So *running* code is **only the root's job** (you, the orchestrator). Never ask a worker or skeptic to run anything; have them produce edits and **static** evidence (code read, file:line, grep), and you run the verification after collecting their work. Static evidence is the verification currency here; runtime confirmation is a bonus only the root can supply.

**Tool calls:** select a persona with the `agent_role` parameter of `spawn_agent` (`skeptic` for read-only refutation, `explorer` for read-heavy scoping, `worker` for edits; omit for `default`). For homogeneous batches use `spawn_agents_on_csv` with an `instruction` template (`{column}` placeholders), `output_schema`, and `output_csv_path`; each worker must call `report_agent_job_result` exactly once.

## When NOT to fan out

Trivial mechanical edits, single-fact lookups, conversational turns: answer solo. Orchestration must buy coverage or confidence, not ceremony.

Don't fan out over related work either: failures that may share one root cause (fix one, fix all), exploratory debugging where you don't yet know what's broken, or units needing shared state. Investigate solo until units are provably independent — then fan out.

## The shape of a full run

This is the `full` route. Other routes are subsets: `lightweight` = steps 1+5 only (no fan-out); `audit` = no edits; `adversarial-only` = step 4 only.

1. **Scout inline first.** Before spawning anything, spend a few tool calls discovering the work-list: which files, which dimensions, which subsystems. You cannot decompose what you haven't scoped.
2. **Decompose into independent units** — by dimension (bugs / security / perf / API-misuse), by subsystem, or by item (file, endpoint, test). Emit the work-list explicitly with a count, then bind **one agent per unit** — the model under-spawns by default, so never collapse units to save spawns. Units must not need each other's output.
3. **Fan out.**
   - Heterogeneous tasks → `spawn_agent` per unit (`agent_role` chosen per unit), all spawned back-to-back before any waiting.
   - Homogeneous fan-out over a list → `spawn_agents_on_csv` with an `instruction` template + `output_schema` (concurrency clamps to `agents.max_threads`). When it returns, scan the result CSV's `status`/`last_error` columns: failed or timed-out rows produced no result — re-run just those or report them uncovered. Never synthesize as if every row succeeded.
4. **Adversarially verify** anything load-bearing with fresh skeptics (reference.md has the full pattern).
5. **Synthesize yourself.** Read every result. You write the final answer — never paste agent output unread. Report what was covered AND what was not (skipped dimensions, capped lists, failed agents, unverified claims). Silent truncation reads as "covered everything" — never do that.

## Reporting

For code changes, add one final gate before reporting, matched to what the change needs:
- Static/diff-level assurance → spawn a read-only `skeptic` over the complete diff.
- Runtime correctness → the **root** runs the suite/build (a skeptic cannot). In an *interactive* session you may also instruct the user to run `/review` (a slash command only the user can invoke); under `codex exec`/CI there is no user, so the root must self-verify or report the gap.

Lead with the verified conclusion. Then: what was swept (dimensions × agents), what survived verification vs was refuted, what failed, and what remains uncovered. Findings that failed adversarial verification are mentioned only as "checked and rejected" — never silently dropped.

For non-trivial runs, leave a durable trail: the external harness writes `.codex/ultracode/runs/<id>/` (`run.json`, `results/`, `ledger.md`) via `start_run`/`save_result`/`write_ledger`. In-session, write the same ledger sections (Route / Scope / Coverage / Findings / Changes / Verification / Adversarial gate / Unresolved risks / Next action) so the run is auditable and resumable.
