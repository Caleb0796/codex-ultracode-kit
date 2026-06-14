# Comparison: codex-ultracode-kit vs. f1974939505/codex-ultracode-mode

An honest, evidence-based head-to-head (reviewed June 2026, against that repo's then-current `HEAD`). Both bring Claude-Code-style "ultracode" to Codex. They make opposite bets, and each got real things right.

## The core difference

- **codex-ultracode-mode is an artifact-and-prompt scaffolding layer.** Its scripts (`uc_route`, `uc_bootstrap`, `uc_merge_results`, `uc_adversarial_verify`, …) generate `routing.md`, `work_items.csv`, `spawn_agents_prompt.md`, and a run ledger, then hand orchestration to the in-session model (which is expected to call Codex's native `spawn_agents_on_csv`). Confirmed by reading every script: **no `codex exec`, no `Popen`, no threads.** The fan-out is delegated to the same model that tends to under-spawn.
- **codex-ultracode-kit is a real external orchestrator.** `codex_workflow.py` runs `agent()` = one `codex exec` subprocess; `parallel()` = a thread pool of N concurrent processes; `pipeline()` = per-item chains — verified running 8 concurrent agents. The concurrency number lives in your code, not the model's discretion.

Neither is strictly "better" — they optimize different things. This kit took the **engine** seriously and was thin on the **front door**; theirs took the front door/packaging seriously and never built an engine.

## Head-to-head

| Dimension | codex-ultracode-mode | codex-ultracode-kit (this repo) |
|---|---|---|
| Fan-out engine | In-session model + `spawn_agents_on_csv` (delegated) | Real concurrent `codex exec` processes (owned) |
| Activation | **`$ultracode` sigil, explicit-only** ✅ | now adopted `$ultracode` (was a bare keyword) |
| Routing pre-flight | **model-owned route + capability flags** ✅ | now adopted (Phase 0) |
| Schema output | honor-system prompt JSON | enforced `jsonschema` + `additionalProperties:false` ✅ |
| Parallel writers | trusts the model | git-worktree isolation ✅ |
| Token budget | none | `CODEX_WF_BUDGET` ceiling ✅ |
| Pipe-deadlock under load | latent (all `capture_output=True`, safe only serially) | hit it, fixed it (redirect to files) ✅ |
| Run ledger / audit trail | **durable `runs/<id>/` + idempotent ledger** ✅ | now adopted (`start_run`/`write_ledger`) |
| Adversarial verify | `uc_adversarial_verify.py` is a **regex linter + checklist** — no skeptics, no votes | real independent skeptics, majority vote ✅ |
| Cloud-OnRequest command constraint | ignored — `verifier`/`edge_tester` told to "run safe checks" (auto-reject) | skill makes verification the root's job ✅ |
| Hooks | shipped, but **don't fire under `codex exec`** (acknowledged) | none by design (guardrails in the harness we parent) |
| Install / uninstall / validator | **cross-platform + package validator** ✅ | now adopted (`install.sh`/`install.ps1`/validator) |
| Agent roles | 10 TOMLs (collapse to ~3 capabilities) | 1 skeptic + built-ins (+ prompt-parameterized) |

## What theirs got right (adopted here)

1. **`$ultracode` explicit-only activation.** A bare `ultra`/`ultracode` keyword mis-fires on "ultra-thorough refactor"; a sigil token doesn't. Adopted, with their negative-trigger description.
2. **Model-owned routing pre-flight.** Classify the task into a named route + decide solo / in-session / external-harness *before* spawning. Adopted as Phase 0.
3. **Durable run ledger.** `runs/<id>/` with `run.json`, `results/`, idempotent `ledger.md`. Adopted as `start_run`/`save_result`/`write_ledger`.
4. **Cross-platform install + a behavioral validator.** Adopted: `install.sh`/`install.ps1` over a Python core, plus `check_package.py` that py_compiles and exercises the deterministic guards.
5. **A few sharp verification ideas:** the "verification-shallow" anti-theater check (a parse/compile/lint pass is not behavioral proof), a 5-state evidence taxonomy for the ledger, and a **contract lens** (exact filenames / CLI flags / install commands / API / docs). All adopted into `codex_patterns.py`.

## What theirs got wrong (rejected, with fixes)

1. **No real engine.** Kept this repo's process-level orchestrator as the execution truth; their artifacts are a good *plan* format, not an *executor*.
2. **Ignores the cloud-`OnRequest` reality.** Their `verifier`/`edge_tester` sub-agents are told to run commands that auto-reject in `exec` mode. Here, sub-agents edit/read only; running tests is the root's job.
3. **`uc_adversarial_verify.py` has no adversary** — it's a regex linter that emits a to-do CSV. The name oversells. This repo keeps the adversary *in code*: independent read-only skeptics that try to refute, majority vote, `UNVERIFIABLE ≠ refuted`.
4. **Their re-ingest only content-checks files whose names match `adversarial|claim|edge`** — a `worker_03.json` reporting `status: fail` slips the gate. Fixed: `reingest_findings()` checks **every** result file, and existence alone never passes.
5. **`subprocess.run(capture_output=True)` everywhere** — safe only because it's serial; it would deadlock the moment it parallelized. This repo redirects child output to files.
6. **Hooks as safety theater** — they don't fire under `codex exec` and their destructive-command guard has false-negatives (`rm -rf $HOME/...` isn't caught because `shlex` doesn't expand `$HOME`). No hooks shipped here.
7. **10 near-duplicate role TOMLs** that collapse to ~3 capabilities. Kept 1 skeptic + built-ins; distinguish behavior by prompt parameters, not by minting agents.

## Bottom line

This repo now has both halves: the **environment-correct execution engine** it always had, plus the **front door, routing, ledger, and packaging** the other repo did better — with both projects' bugs (pipe deadlock, filename-only re-ingest, un-runnable sub-agent verification, hook theater, schema 400s) fixed and documented. Every claim above was verified by reading the other repo's source and by running this repo's code (`scripts/check_package.py`).
