# Ultracode for Codex — what's installed and how to use it

Codex CLI 0.125 (your build) ships native multi-agent support: `multi_agent` is stable and enabled, with tools `spawn_agent`, `send_input`, `wait_agent`, `close_agent`, `list_agents`, `resume_agent`, and the batch fan-out `spawn_agents_on_csv` (one worker per CSV row, `{column}` templates, `output_schema`, `max_concurrency` default 16). The catch — by design and confirmed in the built-in prompt — is that **Codex only spawns agents when the user explicitly asks**. Everything below builds on that hook.

## Installed and verified (2026-06-12)

**1. `~/.codex/skills/ultracode/SKILL.md` (+ `reference.md`)** — the orchestration playbook. Triggers on the literal `$ultracode` token in your message (the token *is* the explicit ask the gate requires). The SKILL.md core (under the 8 KB loader cap) encodes: routing pre-flight with scale-to-ask calibration → scout → decompose → fan out (`spawn_agent` / `spawn_agents_on_csv`) → adversarial verify → synthesize; the companion `reference.md` holds the worker-prompt rules, quality patterns (independent skeptics that refute findings, loop-until-dry discovery, multi-angle sweeps, judge panels, barrier discipline, completeness critic), disjoint file ownership for parallel writers, and honest coverage reporting.
*Verified live:* `codex exec '$ultracode spawn two parallel sub-agents…'` → the skill auto-triggered, two sub-agents (Cicero, Locke) ran in parallel with disjoint ownership, and the root did its own verification rather than trusting worker reports. (In shell commands, single-quote or escape the token — inside double quotes the shell expands `$ultracode` to an empty string before Codex ever sees it.)

**2. `~/.codex/agents/skeptic.toml`** — a custom adversarial-verifier role, `sandbox_mode = "read-only"`, instructed to refute rather than confirm.
*Verified live:* role probe returned `default, skeptic, explorer, worker` — the custom role loads. Built-ins: `explorer` (read-heavy), `worker` (read-write).

**3. `examples/AGENTS.md` (in the kit checkout, ready to install)** — adds an "Ultra mode" trigger section plus three always-on rigor rules that published evals show actually bind (search-until-dry sweeps, hunk-by-hunk diff re-read + grep-changed-symbols, requirement-to-diff completeness mapping). Install from the kit checkout with:
```bash
cp examples/AGENTS.md ~/.codex/AGENTS.md
```

## How to use

- `$ultracode <task>` in any Codex session (interactive or `codex exec`) → full orchestration: parallel sub-agents, adversarial verification, synthesis.
- Plain messages → normal solo Codex. No keyword, no token multiplier (sub-agent runs cost roughly N× tokens; the smoke test burned ~273K input tokens for a toy task, 76% cached).
- For homogeneous fan-out ("do X for each of these 200 files"), use `$ultracode` and mention the list — the skill steers Codex to `spawn_agents_on_csv`.

## Worth adding next (config snippets — your call, not auto-applied)

```toml
# ~/.codex/config.toml

# Independent judge for /review — different model reduces self-preference bias
review_model = "gpt-5.4"

# More parallel agent threads for ultra runs (default 6, depth 1)
[agents]
max_threads = 16
```

- **`/review` as the final gate** — runs a *fresh-context* reviewer agent over your working tree; accepts custom focus instructions. This is the real adversarial self-review; same-context "double-check your work" prose measurably is not (ICLR 2024: self-correction without new evidence flips more right answers than it fixes).
- **Cloud best-of-N** — `codex cloud exec --env <ENV_ID> --attempts 4 "<task>"` generates four independent candidate solutions to pick from. The only native best-of-N; local equivalent is one Codex per git worktree, then a fresh `/review` session judging the diffs.
- **ExecPlan / PLANS.md** (OpenAI Cookbook, "multi-hour problem solving") — for long autonomous runs: copy the cookbook's PLANS.md into the repo and add one AGENTS.md line: *"When writing complex features or significant refactors, use an ExecPlan (as described in .agent/PLANS.md) from design to implementation."*
- **Hooks** — `codex_hooks` is stable in your build: a turn-stop hook can run tests/linters mechanically and feed failures back, enforcing verification instead of trusting prose. **Interactive sessions only:** hooks do not fire under non-interactive `codex exec`, which is why this kit ships none (guardrails live in the external harness instead).
- **Outer-loop fan-out** — for cross-repo or unattended batch work, drive `codex exec` from a plain shell (one git worktree per task, `--json` / `-o` to collect results). Don't spawn `codex exec` from *inside* a sandboxed session — the child inherits the sandbox and loses network.

## Things to know

- A cloud-managed policy on your machine overrides `approval_policy = "never"` to `OnRequest` (seen in the smoke test logs: "set by cloud requirements"). Sub-agents inherit the session's sandbox/approval policy.
- `multi_agent_v2` and `enable_fanout` exist in your build but are under development and off — leave them.
- Your `model_reasoning_effort = "xhigh"` is already the max depth knob; "think harder" prose adds nothing on top of it.
- Sub-agents can't ask you questions — only the root can. Ultra runs resolve ambiguity up front or state assumptions.
