# Codex Ultracode

Bring **Claude-Code-style multi-agent orchestration** ("ultracode") to the **OpenAI Codex CLI**: an opt-in skill that fans work out across parallel sub-agents, verifies findings adversarially, and synthesizes — plus a deterministic external orchestrator for when you need dozens of agents.

> **The core insight.** Claude Code's "dozens of agents" don't come from the model *deciding* to spawn them — they come from a deterministic harness that picks the concurrency. Codex's native `spawn_agent` is the opposite: model-driven and trained to spawn conservatively (2–6). This kit closes that gap from two directions: a **skill** that makes the in-session path rigorous, and an **external harness** that puts the concurrency number in *your* code.

## What's in here

| Path | What it is |
|------|------------|
| `skills/ultracode/SKILL.md` | The orchestration skill core — **explicit-only activation on the `$ultracode` token**. Phase 0 routing pre-flight (with scale-to-ask depth calibration) → scout → fan out → adversarially verify → synthesize. Kept under the 8 KB Codex loader cap. |
| `skills/ultracode/reference.md` | The skill's long-form companion (read by the root on activation): worker-prompt rules, quality patterns (incl. barrier discipline), and concurrency/budget/effort mechanics. |
| `skills/ultracode/agents/openai.yaml` | Skill display metadata. |
| `agents/skeptic.toml` | A read-only "skeptic" agent role: tries to **refute** findings rather than confirm them (verdicts: `CONFIRMED` / `REFUTED` / `UNVERIFIABLE`). |
| `orchestrator/codex_workflow.py` | A Python port of Claude Code's Workflow tool — `agent()` / `parallel()` / `pipeline()` over `codex exec` subprocesses, with schema validation, worktree isolation (+ `apply_diff`), a **budget object** (`total`/`spent()`/`remaining()` for loop-until-budget), a **resume journal** (`CODEX_WF_RESUME`), runaway backstops (1000-agent lifetime cap, 4096-item cap), `phase()`/`label=` progress tags, and a durable **run ledger** (`start_run`/`save_result`/`write_ledger`). |
| `orchestrator/codex_patterns.py` | The ultracode *methodology*: `adversarial_verify` (status-honest: refuted vs unverified), `judge_panel` (multi-judge + synthesis/grafting), `loop_until_dry` (logs caps), `multi_modal_sweep`, `completeness_critic` (5-state + gaps→next-round work), plus deterministic guards `verification_shallow` (fail-closed) and `reingest_findings`. |
| `orchestrator/mcp_frontdoor.py` | **In-session MCP front door** (zero-dependency stdio server): `ultracode_run` / `ultracode_review` / `ultracode_workflow` (saved workflows by name) return a `run_id` **immediately** and run in the background on the kit engine — poll `workflow_status(run_id)` or subscribe to the run's MCP resource for push updates; per-worker error text always captured. Parses "+500k" budget directives. Registration snippet in the module docstring. |
| `orchestrator/workflows/` | Builtin **saved workflows** (`fanout`, `review`) for `wf.workflow(name, args)` / the `ultracode_workflow` tool; project workflows live in `.codex/ultracode/workflows/<name>.py` (a `run(args)` + optional `META`). |
| `scripts/install.py`, `scripts/check_package.py` | Cross-platform install/uninstall core and a behavioral package validator. |
| `install.sh`, `install.ps1` | Thin wrappers: validate, then install. |
| `docs/COMPARISON.md` | Honest head-to-head vs. `f1974939505/codex-ultracode-mode` — what each got right, what was adopted/rejected. |
| `docs/workflow-in-codex.md` | How the external harness mirrors the Workflow tool, the four `codex exec` gotchas it handles, and a verified Codex-vs-Claude parity table. |
| `docs/ultracode-test-plan.md` | A 30+ test plan covering trigger gating, orchestration, and the AGENTS.md rules. |
| `docs/ultracode-codex-setup.md` | Setup guide and optional config upgrades. |
| `docs/codex-ultracode-zh.html` | A detailed Chinese-language reference + gap analysis. |
| `examples/AGENTS.md` | An example global `AGENTS.md` showing how to wire the `$ultracode` trigger into your behavioral file. |

## Install

```bash
bash install.sh                 # validates the package, then installs the skill + skeptic role
#   --dry-run        show what would happen, write nothing
#   --uninstall      remove the skill + role
#   --codex-home DIR target a non-default $CODEX_HOME
# Windows: .\install.ps1
```

Then (manual, by design — it's your config) raise the in-session agent cap in `~/.codex/config.toml`:

```toml
[agents]
max_threads = 16        # default 6; no hard upper bound on the V1 multi-agent path
```

For the external harness: `pip install jsonschema` enables strict schema validation. Verify the skill loaded: `codex exec "List the skills available to you, names only."` → `ultracode` should appear.

## Use

In any Codex session, invoke with the explicit token (bare "ultra"/"ultracode" or task wording does **not** trigger it — the cost-control guarantee):

```
$ultracode review the changes on this branch — find bugs, verify each finding with skeptics, report what survived
```

For genuinely large, deterministic fan-out, drive the external harness instead:

```python
import codex_workflow as wf
import codex_patterns as cp

# reviewers must return a top-level 'findings' array — verify() reads rev["findings"]
FINDINGS_SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "items": {
    "type": "object", "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                                     "claim": {"type": "string"}}}}}}

# one reviewer per dimension, each finding adversarially verified by independent skeptics;
# pass run_dir to leave a durable audit trail (run.json + results/ + ledger.md on disk).
run = wf.start_run("review module on 3 dimensions")
confirmed = cp.review_then_verify(["correctness", "security", "perf"], FINDINGS_SCHEMA, run_dir=run)
# confirmed = findings that survived 3-lens static refutation (the finding's own
# dimension is used as a lens when it maps to one, e.g. perf). The ledger at
# run/ledger.md records Scope / Coverage / Findings / Adversarial gate — and reports
# dropped dimensions, skeptic failures, and budget exhaustion instead of a false clean pass.
```

`CODEX_WF_CONCURRENCY`, `CODEX_WF_MODEL`, `CODEX_WF_EFFORT`, `CODEX_WF_TIMEOUT`, `CODEX_WF_BUDGET` (soft token ceiling — see `wf.budget.remaining()` for loop-until-budget), `CODEX_WF_MAX_AGENTS` (lifetime runaway cap, default 1000), `CODEX_WF_RESUME` (resume journal dir; read-only agents replay, refuses across a changed HEAD), `CODEX_WF_CACHE` (legacy read-only memoizer: `1` uses the default location next to `CODEX_WF_RUNS`, any other value is the cache dir; identical calls collapse — prefer `CODEX_WF_RESUME`), `CODEX_WF_RUNS`, `CODEX_WF_CWD` tune the harness.

Parallel writers run via `agent(..., isolation="worktree")`: each gets a fresh git worktree and returns its diff. Land the diffs with `wf.apply_diff(diff)`, **then** call `wf.cleanup_worktrees()` — failure-path and no-change cleanup is automatic, success-path cleanup is yours.

## Four layers, by scale

| Need | Tool | Concurrency |
|------|------|-------------|
| In-session, model coordinates, a handful of agents | `spawn_agent` (the skill) | capped by `agents.max_threads` |
| In-session homogeneous batch | `spawn_agents_on_csv` | `min(your value, 64, max_threads)` |
| In-session, deterministic N, background + progress | `mcp_frontdoor.py` (MCP tools) | `CODEX_WF_CONCURRENCY`, N ≤ `ULTRACODE_MAX_FANOUT` |
| Dozens of agents, unattended/cross-repo | `codex_workflow.py` (external) | **your N** — bounded by API rate limits / RAM |

## Environment assumptions (read before relying on it)

This kit was built and verified on **Codex CLI 0.125.0 / macOS / gpt-5.5**, in a setup where a managed policy forces approvals to **`OnRequest`**. Two consequences are baked into the skill:

- **Sub-agents can edit files but cannot run shell commands** (tests/builds are auto-rejected in `exec` mode). So verification — running the suite — is the **root's** job, after collecting edits. If *your* Codex setup allows sub-agent command execution, the "Hard environment constraint" section of the skill is stricter than you need; relax it.
- **`agents.max_threads` default is 6** and is the tree-wide concurrent cap. The skill assumes you've raised it (the manual `config.toml` step under Install above). `spawn_agents_on_csv` is clamped to it.

## Verification status

Every "it works" claim here was checked by running it, not by inspection — including bugs that only appeared under load. Notably: the external harness deadlocked at 8 concurrent agents until child output was redirected to files instead of pipes (`subprocess` pipe-buffer deadlock); schema output needed `additionalProperties:false` injected; worktree isolation leaked branches on retry until fixed. See `docs/workflow-in-codex.md` and `docs/ultracode-test-plan.md`.

## License

MIT — see [LICENSE](LICENSE).
