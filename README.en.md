# zcode-agent-routing

**English** | [中文](README.md)

Turn "which model should handle this?" from an on-the-spot judgment call into rules written into config.

A multi-agent routing setup for [ZCode](https://zcode.ai): the main session plans and decides,
cheap models do the work, two reviewers from **different model families** alternate automatically,
and a final review layer guarantees that *nothing in the chain shares an origin with the producer*.

Three hooks do three things: **swap the reviewer, block silent skips, heal the config.**

---

## The problems it solves

The hard part of multi-agent work is not "can I define a few roles" — it's **deciding who gets each job**.

**1. Deciding by feel.** When to spend on a strong model is usually gut feeling. With a full plate,
"is this change worth the expensive model" and "can I skip review this time" degrade into instinct.

**2. One task, one perspective.** When the reviewer is the same model that wrote the code,
it sees the **same blind spots**. It finds the code fine — because that is exactly the code it would have written.

**3. Skips go unnoticed.** Stage N says stage N+1 should review. Someone dispatches a different stage.
The chain still looks complete. This class of failure **never errors out** — the most insidious kind.

This project assigns those three problems to: a config file, a rotation hook, and a gate hook.

---

## Architecture: four layers

| Layer | Role | Job | How to pick the model |
|---|---|---|---|
| Command | main session | plan, split, decide, orchestrate | your primary model |
| Execution | `impl-worker` | write code to spec, run commands, bulk edits | **two fast models, swapped as needed** |
| | `vision-reader` | screenshots, page diffs, screen recordings | one with vision input |
| | `doc-writer` | Chinese copywriting, polish | one that's good at Chinese |
| First review | `first-reviewer-a` | independent first review | a strong model |
| | `first-reviewer-b` | independent first review | **a different family than A** |
| Final review | `reviewer` | heterogeneous final review | **different family from both producer and A** |

The bundled example config:

- First review A = **GLM-5.4** (long thinking pays off *here*; see below)
- First review B = **DeepSeek-V4.1-Flash**
- Final review = **Kimi K3.1-Pro** (the other side can be any external model; sides alternate per case)
- Execution = **GLM-5.3-Flash** ⟷ **MiniMax-M3.1-Flash**, swapped by need

---

## The counter-intuitive bit: first review is *not* heterogeneous

Lay out every layer against "is it the same origin as the main session?" and the design intent becomes visible:

| Layer | Relation to the main session |
|---|---|
| Execution | different origin |
| First review | **one of the two members is always the same family as the main session** (if the main session runs DeepSeek, member B is DeepSeek too) |
| Final review | **different from everything upstream** — no matter which model the main session switched to |

So:

- **The first-review layer buys "two independent perspectives", not heterogeneity.**
  It adds independence, adversarial stance, and freedom from the author's rationalization.
  It cannot see blind spots shared by same-family models.
- **The final-review layer buys heterogeneity.** Its candidates share no origin with anything upstream,
  so **the final layer stays heterogeneous regardless of which model the main session is on.**

**The final layer doesn't buy "stronger". It buys "guaranteed different origin".**

This also answers an obvious objection: if the main session temporarily switches to a strong model
that also serves as a reviewer, isn't it reviewing itself? No — heterogeneity is backstopped by the
final layer and does not depend on what the main session runs.

---

## The three hooks

| Hook | Event | What it solves |
|---|---|---|
| `hooks/review_role_rotation.py` | PreToolUse | Alternates the two first-review members **within one session**; also defaults review dispatches to background so the main session isn't blocked |
| `hooks/review_dispatch_guard.py` | PreToolUse + PostToolUse/Failure | A review dispatch must carry **entry evidence** (the previous report's status line, or a `first review` declaration). Missing = rejected |
| `hooks/heal_frontmatter.py` | SessionStart | Panel saves silently drop fields (`memory`, `thoughtLevel`); healed idempotently at session start |

All three follow one rule: **they never block the pipeline because of their own failure.**

- The rotation hook only rewrites dispatches; it never denies.
- The gate denies *only* on missing evidence; malformed payloads and internal errors fail open.
- The heal hook logs its own errors and exits 0.

### How rotation splits work

```
1st dispatch of first-reviewer-a → actually runs as first-reviewer-b
2nd dispatch of first-reviewer-b → actually runs as first-reviewer-a
```

Alternation is per session. The starting member for a new session alternates globally (so every
session doesn't start with the same member). Put `REVIEW-ROLE-FIXED` in the prompt to pin the
member for that one dispatch (don't put it in your task-book template).

### What the gate accepts as evidence

The dispatch `prompt` or `description` must contain one of:

- a line starting with **`〔下一跳`** (most common);
- a line starting with **`〔状态`** or **`〔本轮状态`** (real task books often put status before next-hop);
- a line starting with **`首审`** or **`状态：`** (first stage in a chain, declare it honestly).

Allowed prefixes: `>`, `-`, `#`, backticks, `**`, and a single **ASCII role label**
(e.g. `K3：〔下一跳…〕`, `glm：首审…`). Chinese prose does not count — so
"here is a checklist: 〔下一跳…〕" cannot sneak through.

**The rule is "starts with", not "contains".** This was calibrated against real task books:
with a "contains" rule, a format illustration such as `状态行格式是〔下一跳:X〕` satisfied the gate —
and the gate exists to catch template reuse, so being fooled by a template defeats its purpose.
The tightening was A/B-tested on **889 real task books**; it also had to accept `〔状态`-leading lines,
backtick-wrapped lines, `> ##` blockquote headings, `（逐字）`-style heading suffixes, and content
after an unclosed code fence. Final result: **zero regression** on real task books.

**The inline-prompt path is slightly looser: the marker only has to appear somewhere in the prompt**,
not at line start. That is deliberate — inline text is written at dispatch time, while task books are
copied around and accumulate stale templates, so the tightening targets task books. Do not treat the
gate as an intent checker; it only guarantees that *something* is there.

When the prompt only carries a task-book path, the gate reads that task book, finds the
"previous report status line" heading (blockquote / `#` / `**` / `（必填）` / `（逐字）` all tolerated),
and validates the declaration line that follows.

---

## Four hard rules

**① Model routing is config, not prompt.**
Switching models = editing the `model:` field in the role file. Writing "you are model X" in the
prompt has **zero** mechanical effect and misleads whoever reads the config later (including you).

**② Task books give paths, not content.**
Let the agent read files itself (it has a cheap channel). The task book carries paths, parameters,
and acceptance criteria only. Pasting content into a task book makes the expensive layer expensive too.

**③ Write artifacts to disk, return only a summary.**
Evidence, scripts, reports all go to files; the return is a summary plus paths. Otherwise the whole
intermediate process floods back into the main session and its context fills up fast.

**④ Review reports end with a status line.**
Format: `〔下一跳:<next action>(之后→<follow-up>;闭案条件=<close condition>)〕`.
The first hop is the next role + stage that must be dispatched. It doubles as the
"entry evidence" the gate looks for.

---

## Issue grading: real scenarios, not "looks strict"

| Level | Definition | Required |
|---|---|---|
| **P0** | Wrong behavior / data corruption / false green / security incident on a real path | trigger scenario, probability, impact, minimal fix |
| **P1** | Reachable in real usage with clear impact; or low probability but severe *and* the minimal fix is a small local change | same four items — **missing any one disqualifies it as P1** |
| **P2** | Style / optimization / extreme scenarios | must state "not worth complex design for this" |

Extreme scenarios (multiple independent failures, non-product paths, fixes requiring new machinery)
default to P2. If the impact is data corruption / a security incident / a wrong external delivery,
list it separately for a human decision instead of silently dropping it.

---

## Install

Full steps in **[install.md](install.md)**. Shortest path:

```bash
# 1. roles
mkdir -p ~/.zcode/agents
cp agents/first-reviewer-a.md agents/first-reviewer-b.md ~/.zcode/agents/

# 2. hooks
cp hooks/*.py ~/.zcode/agents/
chmod +x ~/.zcode/agents/*.py

# 3. merge the hooks section of config-hooks.snippet.json into ~/.zcode/cli/config.json
# 4. reopen a session, then dispatch the same review role twice and check the log
tail -5 ~/.zcode/agents/.review-rotation.log
```

Role definition changes take effect in a **new session** (the engine re-reads role config at the
start of every turn).

### Self-test

All three hooks have regression tests — zero dependencies, and it never touches your real config:

```bash
python3 test/smoke.py
```

It covers rotation alternation, the fixed-member marker, explicit foreground not being overridden,
zero impact on non-review roles, fail-open behavior, gate reject/allow, eleven evidence formats (eight real-world variants plus three
template traps that must stay rejected), registry writes, and heal idempotency.
**All 30 checks must pass.**

---

## Finding your own model IDs

The `model:` field holds **your machine's** model identifier, which differs per account. Two ways:

**Most reliable:** in the ZCode panel, pick a model for one role once and let it write the `model:`
line itself, then copy that value back into your hand-written file (the panel save drops fields —
use it only as a value reader).

**From the CLI:**

```bash
python3 - <<'EOF'
import json, os
p = os.path.expanduser('~/.zcode/v2/provider_config.json')
d = json.load(open(p))['config']
for r in d.get('providerConfigRules', {}).get('providerRules', []):
    pid = r.get('providerId')
    cfg = r.get('config') or {}
    for m in (cfg.get('modelOrder') or cfg.get('personalModelIds') or []):
        if pid:
            print(f'{pid}/{m}')
EOF
```

Field shapes vary across versions; if this yields nothing, use the panel route.

---

## Known limitations

- **This is infrastructure you maintain, not a one-time setup.** Every rule change means syncing the
  role files, the hook constants, and your process docs. If your workload is small, or your model pool
  has only one family, skip it — **don't build a kitchen for a recipe you can follow already.**
- **It does not work with a single model family.** "Final review is heterogeneous" requires at least
  two genuinely distinct families.
- **The gate is an existence check, not a semantic check.** A determined liar can get past it;
  what it stops is accidental drift and template reuse.
- **Long-thinking models review better but are slow.** Waiting on them in the foreground feels bad,
  which is why the rotation hook defaults review dispatches to background.
- **Panel saves drop fields.** That's why `heal_frontmatter.py` exists; hand-written markdown is the
  full-fidelity path.

---

## Layout

```
zcode-agent-routing/
├── README.md                    Chinese README（中文版）
├── README.en.md                 you are here
├── install.md                   step-by-step install with a verification per step
├── config-hooks.snippet.json    hooks fragment to merge into ~/.zcode/cli/config.json
├── agents/                      6 role definitions
├── hooks/                       3 hook scripts
└── test/smoke.py                zero-dependency regression tests
```

---

## License

MIT
