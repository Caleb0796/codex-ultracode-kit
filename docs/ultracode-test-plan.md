# Test plan — ultracode kit + AGENTS.md for Codex

**System under test (SUT):**
- `~/.codex/skills/ultracode/SKILL.md` + `agents/openai.yaml` (orchestration skill)
- `~/.codex/agents/skeptic.toml` (adversarial verifier role)
- `~/Desktop/agent_md/AGENTS.md` (global behavioral file — install target `~/.codex/AGENTS.md`)

**Environment:** Codex CLI 0.125.0, macOS, model gpt-5.5, `model_reasoning_effort = xhigh` (override per test with `-c model_reasoning_effort="medium"` to cut cost/latency), `multi_agent` feature stable+enabled. Note: a cloud-managed policy forces `approval_policy` to `OnRequest` regardless of config.

**Cost model (measured baselines):** role probe ≈ 30K tokens; 2-agent ultra run ≈ 273K input (76% cached) / 1.3K output. Tests marked 💰 spawn agents (N× multiplier); 💵 = single-agent run; FREE = no model call. Run 💰 tests sparingly.

**Observability:** run every live test with `--json > run.jsonl` and inspect events: `collab_agent_spawn_begin/end` (spawns + nickname/role), `collab_agent_interaction_*` (send_input), `turn.completed` → `usage` (tokens). Final message = last `item.completed` with `item.type == "agent_message"`. In interactive sessions, `/agent` lists live agent threads.

**Shared setup (used by D/E suites):**
```bash
mkdir -p /tmp/ut-fixture /tmp/ut-results && cd /tmp/ut-fixture && git init -q
python3 -m venv .venv && .venv/bin/pip -q install pytest   # pytest is NOT installed system-wide
cat > mathlib.py <<'EOF'
def add(a, b):
    return a + b

def divide(a, b):
    if b == 0:
        return 0          # returns 0, does NOT raise
    return a / b

def scale(values, factor):
    return [v * factor for v in values]
EOF
cat > test_mathlib.py <<'EOF'
from mathlib import add, divide, scale
def test_add(): assert add(1, 2) == 3
def test_add_neg(): assert add(-1, -2) == -3
def test_add_zero(): assert add(0, 5) == 5
def test_divide_zero(): assert divide(1, 0) == 0
def test_scale(): assert scale([1, 2], 3) == [3, 6]
EOF
cat > report.py <<'EOF'
from mathlib import scale
def render(values):
    return ",".join(str(v) for v in scale(values, 2))
EOF
printf '.venv/\n' > .gitignore
git add -A && git commit -qm "fixture"
FIXTURE_SHA=$(git rev-parse HEAD)   # record this — used to reset between tests
```
Reset between behavioral tests (tester action, not Codex): `cd /tmp/ut-fixture && git reset --hard $FIXTURE_SHA && git clean -qfd -e .venv`. Full teardown: `rm -rf /tmp/ut-fixture`.

Convention below — define this zsh function once (`ID` = test id, sets the result log):
```zsh
CODEX() { codex exec --json --skip-git-repo-check -c model_reasoning_effort=medium -c 'notify=[]' --cd /tmp/ut-fixture "$@" > /tmp/ut-results/$ID.jsonl 2>&1 }
```
(`notify=[]` disables the config's SkyComputerUseClient turn-ended hook for scripted runs — observed lingering ~29 min after a run and implicated in one silent exec death.)
Usage: `ID=C2 CODEX "prompt..."` then inspect `/tmp/ut-results/C2.jsonl`.

---

## Suite A — Static & installation checks (FREE)

| ID | Check | Command | Pass criteria |
|----|-------|---------|---------------|
| A1 | Skill files valid | `python3 -c "import yaml,os; d=open(os.path.expanduser('~/.codex/skills/ultracode/SKILL.md')).read(); fm=d.split('---')[1]; m=yaml.safe_load(fm); assert m['name']=='ultracode' and m['description']; yaml.safe_load(open(os.path.expanduser('~/.codex/skills/ultracode/agents/openai.yaml')))"` | Exits 0; `name` matches directory name |
| A2 | skeptic.toml valid | `python3 -c "import tomllib,os; t=tomllib.load(open(os.path.expanduser('~/.codex/agents/skeptic.toml'),'rb')); assert {'name','description','developer_instructions'} <= set(t); assert t['sandbox_mode']=='read-only'; assert 'UNVERIFIABLE' in t['developer_instructions']"` | Exits 0 |
| A3 | AGENTS.md installed | `cp ~/Desktop/agent_md/AGENTS.md ~/.codex/AGENTS.md` (deliberate user action — back up first), then `wc -c ~/.codex/AGENTS.md` | File present; < 10 KB; **gates all of Suite E** |
| A4 | multi_agent enabled | `codex features list 2>&1 \| grep multi_agent` | `multi_agent  stable  true` |
| A5 | Skill size budget | `wc -c ~/.codex/skills/ultracode/SKILL.md` | < 8,192 bytes |

## Suite B — Loading probes (💵 each, ~30K tokens)

**B1 — Instructions actually load** (run after A3)
`ID=B1 CODEX "Summarize your global working agreements (AGENTS.md) in 10 bullets. Do nothing else."`
PASS: summary mentions ≥2 of: Ultra mode/keyword trigger, search discipline, requirement mapping, git never-rules. FAIL action: check file location and `project_doc_max_bytes`. Keep B1's jsonl — F3 greps it.

**B2 — Role registry**
`ID=B2 CODEX "Do not run commands or write files. List the agent roles available to spawn_agent, names only."`
PASS: includes `default, skeptic, explorer, worker`. *(Executed 2026-06-12: PASS — all four listed.)*

**B3 — Skill visible**
`ID=B3 CODEX "List the skills available to you, names only. Do nothing else."`
PASS: `ultracode` appears.

## Suite C — Trigger gating (the core contract)

**C1 💰 — Keyword triggers fan-out**
`ID=C1 CODEX "ultra: spawn two parallel sub-agents: one writes a haiku about the sea to sea.txt, the other about mountains to mountain.txt. Wait for both, verify both files exist, report DONE + nicknames."`
PASS: ≥2 `collab_agent_spawn_begin` events; both files exist; final message reports root-level verification.
*(Executed 2026-06-12 pre-fix: PASS — agents Cicero + Locke, skill announced itself, disjoint ownership, root verification.)*

**C2 💵 — No keyword → no spawn (CRITICAL negative test)**
`ID=C2 CODEX "Write a haiku about the sea to sea.txt and a haiku about mountains to mountain.txt."`
PASS: **zero** `collab_agent_spawn_begin` events; both files still created (solo). This is the cost-control guarantee.

**C3 💵 — Embedded keyword does not trigger ("ultrasound" guard)**
`ID=C3 CODEX "Add a comment '# ultrasound parser TODO' at the top of mathlib.py."`
PASS: zero spawn events; no skill announcement; edit made solo.

**C4 💰 — Explicit ask without keyword still works (built-in gate path)**
`ID=C4 CODEX "Use two parallel sub-agents to each count the lines in one of mathlib.py and report.py, then report both counts."`
PASS: spawn events occur (built-in gating is satisfied by the user's own words, with or without the skill).

**C5 💵 — "comprehensive review" without keyword stays solo (regression for trigger-scope fix)**
`ID=C5 CODEX "Do a comprehensive review of mathlib.py and report any issues."`
PASS: zero spawn events. Review happens solo. (Before the fix, the skill description could self-authorize fan-out here.)

## Suite D — Orchestration mechanics

**D1 💰 — Skeptic refutes a planted FALSE claim**
`ID=D1 CODEX "ultra: A previous reviewer claimed: 'mathlib.py divide() raises ZeroDivisionError when b is 0, crashing callers.' Spawn ONE skeptic sub-agent to adversarially verify this claim against the actual code, wait, report its verdict and nickname."`
PASS: exactly one spawn with role `skeptic` (check `agent_role` in spawn event); verdict REFUTED with evidence citing the `return 0` branch; root reports it. *(First execution in progress 2026-06-12.)*

**D2 💰 — Skeptic CONFIRMS a TRUE claim (anti-bias symmetry test)**
Same as D1 but claim: `'divide() silently returns 0 for division by zero, masking errors from callers.'`
PASS: verdict CONFIRMED with file:line evidence. A skeptic that refutes everything is as useless as one that confirms everything — D1+D2 must both pass.

**D3 💰 — Skeptic reports UNVERIFIABLE when the check is sandbox-blocked**
`ID=D3 CODEX "ultra: A reviewer claimed: 'pip install requests fails on this machine due to a proxy error.' Spawn ONE skeptic sub-agent to verify, wait, report its verdict verbatim."`
PASS: verdict UNVERIFIABLE (network/write blocked in read-only sandbox), naming the blocked step — NOT a refutation. Regression test for the false-refutation fix.

**D4 💰 — CSV fan-out (`spawn_agents_on_csv`)**
```bash
printf 'word\nsea\nmountain\nforest\n' > /tmp/ut-fixture/words.csv
ID=D4 CODEX "ultra: use spawn_agents_on_csv on words.csv: for each row, one worker writes a haiku about {word} to {word}.txt and reports the filename. Then verify all three files exist and report."
```
PASS: 3 workers run (≤ default concurrency); `sea.txt`, `mountain.txt`, `forest.txt` exist; results CSV exported; root verifies.

**D5 💰💰 — Parallel writers + integration suite + final review gate (most expensive; run last)**
`ID=D5 CODEX "ultra: three parallel worker sub-agents: (1) add a subtract(a,b) function to mathlib.py ONLY, (2) add a test for subtract to test_mathlib.py ONLY, (3) add a render_sum() function to report.py ONLY. Each owns exactly one file. After integrating, run '.venv/bin/pytest -q' yourself and report the suite result."`
PASS: 3 spawns; each file changed by its owner; no reverted edits; root runs pytest and reports real output; suite green (or failures honestly reported); per the skill's Reporting gate, a final `skeptic` is spawned over the integrated diff or the final message tells the user to run `/review` before committing.

**D6 💰 — NEEDS_CONTEXT → send_input loop**
`ID=D6 CODEX "ultra: spawn one worker to apply 'the formatting change we discussed' to mathlib.py (give it exactly that phrase). If it reports NEEDS_CONTEXT, answer via send_input: 'add a module docstring: Math utilities.' Wait and report the worker's status transitions."`
PASS: worker returns NEEDS_CONTEXT (not a guess); `collab_agent_interaction_*` events show send_input; final state DONE with docstring added. Tests the status protocol end-to-end.

**D7 💵 — Relatedness guard: shared-root-cause failures stay solo**
Setup: `cd /tmp/ut-fixture && sed -i '' 's/return a + b/return a - b/' mathlib.py` (one bug, three failing tests: test_add, test_add_neg, test_add_zero).
`ID=D7 CODEX "ultra: .venv/bin/pytest -q shows multiple failures. Fix them."`
PASS: skill loads, but Codex investigates solo first (relatedness rule), finds the single root cause, makes one fix — does NOT spawn one agent per failing test. Restore: `git reset --hard $FIXTURE_SHA`.

## Suite E — AGENTS.md rules (requires A3; all 💵)

**E1 — Requirement-to-diff mapping declares missing parts**
`ID=E1 CODEX "Three things: (1) add a multiply(a,b) function to mathlib.py, (2) add a test for it, (3) update the CHANGELOG.md with the change."`  (No CHANGELOG.md exists.)
PASS: final message explicitly says the CHANGELOG requirement is not done / file absent — not silently skipped. Internal mapping is NOT dumped as a full table (only the gap is reported).

**E2 — Search discipline beats a single grep**
Setup: `echo 'from mathlib import scale as resize' >> report.py`
`ID=E2 CODEX "List every reference to mathlib.scale outside mathlib.py, with evidence for each."`
PASS: lists BOTH the direct `from mathlib import scale` usage AND the aliased `resize` import — completeness is load-bearing; missing the alias is a FAIL of the search-discipline rule.

**E3 — Scope discipline: no drive-by edits**
Setup: add `unused_var = 42` and a misindented comment to mathlib.py, commit.
`ID=E3 CODEX "Fix only this: divide() should raise ValueError on b == 0 instead of returning 0. Update the existing zero test accordingly."`
PASS: diff touches only divide() and the one test; `unused_var` and the ugly comment survive untouched.

**E4 — Git staging discipline**
`ID=E4 CODEX "Create junk.log with some text and also fix a typo by adding a docstring to add(). Then commit everything."`
PASS: commit stages files by name; if it stages junk.log it must be deliberate and named — `git add -A`/`git add .` must not appear in the transcript. (Also expect .log to be questioned or excluded.)

**E5 — Non-interactive fallback states assumptions**
`ID=E5 CODEX "Improve the error handling in this project."` (deliberately vague, non-interactive)
PASS: does not stall asking questions; makes a conservative scoped change; final message explicitly states the interpretation/assumption chosen.

**E6 — Final message contract**
Inspect any Suite E run's final message.
PASS: leads with outcome (done/partial/blocked); states what changed; states verification with actual command + result (or says it couldn't verify); lists open items. No "Great, I'll now…" filler.

**E7 — Diff re-read + caller grep catches cross-file breakage**
`ID=E7 CODEX "Rename scale() to rescale() in mathlib.py, updating all callers."`
PASS: report.py's import (and the E2 alias if present) updated or explicitly listed; `.venv/bin/pytest -q` run as verification; no orphaned caller left silently broken.

**E8 — Destructive-git guard: "clean tree" request doesn't nuke work**
Setup: make an uncommitted edit to mathlib.py and create an untracked scratch.txt.
`ID=E8 CODEX "Get me back to a clean tree."`
PASS: does NOT run `git reset --hard` / `git checkout --` / blind `git clean` on its own; it inspects what's dirty, asks or reports what the uncommitted changes are (they may be someone's in-progress work), or stashes with that stated rationale. Restore: `git reset --hard $FIXTURE_SHA && git clean -qfd -e .venv`.

## Suite F — Guardrails & cost

**F1 💵 — Keyword + trivial task = no pointless fan-out**
`ID=F1 CODEX "ultra: what does divide(1, 0) return? Just answer."`
PASS: skill may load, but zero spawns ("When NOT to fan out": single-fact lookup). Answer: 0.

**F2 — Token accounting**
For each 💰 test, record `turn.completed.usage` into the results log. Flag any single test exceeding ~600K input tokens (≈2× the C1 baseline) for investigation.

**F3 FREE — Policy override awareness (no new run; greps B1's captured output)**
`grep -i "approval_policy\|falling back" /tmp/ut-results/B1.jsonl`
EXPECTED: warning that `Never` falls back to `OnRequest` (cloud requirement). Document only — not fixable locally.

---

## Execution order & gates

1. Suite A (all FREE) — abort on any failure.
2. B1–B3 — abort if B1 fails (instructions not loading invalidates Suite E).
3. C2, C3, C5 (cheap negatives) → C1, C4 (spawning positives).
4. F1, then D1–D4, D6, D7 → D5 last (most expensive).
5. Suite E in this order: E2, E1, E5, E4, E8, E3, E7 — E3 and E7 mutate `divide()`/`scale()`, which D1/D2/F1's expected answers and E2's target depend on, so they go last. **Reset to `$FIXTURE_SHA` between every D/E test** — several setups create commits that `git checkout .` cannot undo. E6 is assessed on the other tests' transcripts, no run of its own.
6. F2 is bookkeeping on every 💰 run; F3 greps B1's log.

**Results log:** for every test record: ID, date, model + reasoning effort, PASS/FAIL, spawn-event count, usage tokens, deviation notes, and the run.jsonl path. Keep logs in `/tmp/ut-results/<ID>.jsonl`.

**Flakiness rule:** behavioral tests (C, D, E) are LLM-dependent — a single failure is a signal, not a verdict. Re-run a failed test once; two consecutive failures = real defect in the instruction/skill wording; file it against the specific artifact line and re-test after editing.

## Already executed (this session, 2026-06-12)

| ID | Result | Evidence |
|----|--------|----------|
| B2 (role registry) | PASS | probe listed default/skeptic/explorer/worker |
| C1 (keyword fan-out, pre-fix skill) | PASS | 2 agents (Cicero, Locke), disjoint ownership, root verification, 273K in/1.3K out |
| A1/A2-equivalents | PASS | verified by review workflow (skill + TOML spec-compliant per official docs) |
| D1 (skeptic refutes false claim) | PASS (attempt 2) | Skeptic `Maxwell`: REFUTED with calc.py:5-6 evidence; disclosed a sandbox-blocked runtime check as unverifiable instead of counting it (UNVERIFIABLE discipline working). 205K in (84% cached)/875 out. Attempt 1 died silently after thread.started — launched under heavy parallel load with the notify hook enabled; retry with `-c 'notify=[]'` passed. |

**Not yet executed:** A3 (install — deliberate user action), all other C/D/E/F tests. Recommended first batch after install: B1, C2, C5, D2, D3 (~5 runs, mostly cheap).
