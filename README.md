# Codex Ultracode

Bring **Claude-Code-style multi-agent orchestration** ("ultracode") to the **OpenAI Codex CLI**: an opt-in skill that fans work out across parallel sub-agents, verifies findings adversarially, and synthesizes — plus a deterministic external orchestrator for when you need dozens of agents.

> **The core insight.** Claude Code's "dozens of agents" don't come from the model *deciding* to spawn them — they come from a deterministic harness that picks the concurrency. Codex's native `spawn_agent` is the opposite: model-driven and trained to spawn conservatively (2–6). This kit closes that gap from two directions: a **skill** that makes the in-session path rigorous, and an **external harness** that puts the concurrency number in *your* code.

## What's in here

| Path | What it is |
|------|------------|
| `skills/ultracode/SKILL.md` | The orchestration skill — **explicit-only activation on the `$ultracode` token**. Phase 0 routing pre-flight → scout → fan out → adversarially verify → synthesize, with role selection, wave/slot-reclaim, a budget brake, and partial-failure handling. |
| `skills/ultracode/agents/openai.yaml` | Skill display metadata. |
| `agents/skeptic.toml` | A read-only "skeptic" agent role: tries to **refute** findings rather than confirm them (verdicts: `CONFIRMED` / `REFUTED` / `UNVERIFIABLE`). |
| `orchestrator/codex_workflow.py` | A Python port of Claude Code's Workflow tool — `agent()` / `parallel()` / `pipeline()` over `codex exec` subprocesses, with schema validation, worktree isolation, token budget, configurable concurrency, and a durable **run ledger** (`start_run`/`save_result`/`write_ledger`). |
| `orchestrator/codex_patterns.py` | The ultracode *methodology*: `adversarial_verify` (static lenses incl. **contract**), `judge_panel`, `loop_until_dry`, `completeness_critic` (5-state), plus deterministic guards `verification_shallow` and `reingest_findings`. |
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

# 8 reviewers in parallel, each finding adversarially verified
results = wf.pipeline(
    ["correctness", "security", "perf"],
    lambda dim, *_: wf.agent(f"Review this directory for {dim}.", schema=FINDINGS),
    lambda rev, dim, _: wf.parallel([
        (lambda f=f: cp.adversarial_verify(str(f), lenses=("correctness","security")))
        for f in (rev or {}).get("findings", [])]),
)
```

`CODEX_WF_CONCURRENCY`, `CODEX_WF_MODEL`, `CODEX_WF_EFFORT`, `CODEX_WF_BUDGET`, `CODEX_WF_CWD` tune the harness.

## Three layers, by scale

| Need | Tool | Concurrency |
|------|------|-------------|
| In-session, model coordinates, a handful of agents | `spawn_agent` (the skill) | capped by `agents.max_threads` |
| In-session homogeneous batch | `spawn_agents_on_csv` | `min(your value, 64, max_threads)` |
| Dozens of agents, deterministic | `codex_workflow.py` (external) | **your N** — bounded by API rate limits / RAM |

## Environment assumptions (read before relying on it)

This kit was built and verified on **Codex CLI 0.125.0 / macOS / gpt-5.5**, in a setup where a managed policy forces approvals to **`OnRequest`**. Two consequences are baked into the skill:

- **Sub-agents can edit files but cannot run shell commands** (tests/builds are auto-rejected in `exec` mode). So verification — running the suite — is the **root's** job, after collecting edits. If *your* Codex setup allows sub-agent command execution, the "Hard environment constraint" section of the skill is stricter than you need; relax it.
- **`agents.max_threads` default is 6** and is the tree-wide concurrent cap. The skill assumes you've raised it (install step 3). `spawn_agents_on_csv` is clamped to it.

## Verification status

Every "it works" claim here was checked by running it, not by inspection — including bugs that only appeared under load. Notably: the external harness deadlocked at 8 concurrent agents until child output was redirected to files instead of pipes (`subprocess` pipe-buffer deadlock); schema output needed `additionalProperties:false` injected; worktree isolation leaked branches on retry until fixed. See `docs/workflow-in-codex.md` and `docs/ultracode-test-plan.md`.

## License

MIT — see [LICENSE](LICENSE).
