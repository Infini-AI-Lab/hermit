# MWM core evolution: integration results

**Historical / superseded:** This run used the simplified MWM-inspired prototype. It is not evidence for the corrected MWM implementation. See [the fidelity correction](mwm-fidelity-correction.md). Raw records remain unchanged.

Date: 2026-09-18. This is a software integration check using fabricated evidence, not a learning benchmark or a reproduction of the original MetaWorld experiments.

## Actual learner run

The MWM method ran through the locally configured Claude Code model, `claude-fable-5-1`. The learner used only the framework's file tools. Its candidate passed static declaration and Python checks. The source core, evidence snapshot, and framework fingerprints stayed unchanged.

The learner changed nine core files: ADAPTER.md, adapter.json, world_model.py, the briefing, journal, two worked examples, one Markdown memory, and one structured policy-history file. It retained the earlier 14-day / 5% policy and recorded the later 7-day / 2% observation with scope and time information.

It also added `return_check`, a Python function and tool declaration. No MCP registration code, wrapper implementation, or host connector was edited. The session reported USD 1.1210555 in API-cost accounting; this is not a claim of incremental subscription billing.

Local records: `.demo/evolution/mwm-av8vugdl/control/`. The candidate remains at `.demo/evolution/mwm-av8vugdl/adapter/`; it was not activated automatically and the initial MWM template remains empty.

## Actual runtime and host calls

After reviewing the generated code, the unchanged framework wrapper loaded the candidate. A direct MCP client and two real acting sessions then called the new tool:

| Synthetic case | Expected result | Direct MCP | Codex | Claude Code |
|---|---|---|---|---|
| Tick 1, day 10, item price 200.00 | Refund 190.00 | Passed | Passed | Passed |
| Tick 2, day 10, item price 200.00 | Not eligible; refund 0.00 | Passed | Passed | Passed |
| Tick 2, day 5, item price 200.00 | Refund 196.00 | Passed | Passed | Passed |

Both hosts also called wm list, read the generated memory, and followed wm trace to original q2.json. Results were checked against server audit records rather than inferred from final answers.

- Codex records: `.demo/mwm-evolved/codex/runs/codex-pwmcd7tb/`.
- Claude records: `.demo/mwm-evolved/claude/runs/claude-ipfaqb_u/`.
- Selected proof: `control/host-verification.json` and `control/mcp-verification.json` inside the evolution session.

Native MWM role configurations were generated, but these acting checks targeted core tool calls and did not invoke the optional researcher role. Earlier minimal-shop tests separately exercised native delegation.

## Limits and unresolved findings

The learner introduced an executable despite incomplete evidence about date inclusivity and monetary rounding. It disclosed assumptions in memory and tool outputs, but disclosure is not verification. The tested cases avoid the exact day boundary and ambiguous rounding. This is an example of why valid code and successful delivery are distinct from a fully justified learned capability.

No live exploration, hidden-policy discovery, generalization evaluation, performance gain, automatic release, or complete sandbox security claim follows from this check. The original metaworld repository and benchmark data were not modified.
