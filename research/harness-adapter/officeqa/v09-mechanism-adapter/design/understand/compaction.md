# Context size and compaction: GLM-5.3-flash under Codex in v8

Scope: every actor API call in the three v8 roots `officeqa-pro133-{none,prompt,cards}-v8-work/runs/*/NNN/`
(399 runs, 22,086 requests, 22,143 wire attempts). Correctness is the 0/1 value from
`officeqa-divergence-analysis/index.json` only. No reference answers or graders were opened.
Scripts are in `understand/compaction_scripts/` (`scan.py` → `rows.json`, then `runs.py`, `post.py`, `loops.py`, `thresh.py`).

Terms used below:
- **Token**: the unit the model counts text in. For English, one token is roughly 4 characters.
- **Context window**: the most tokens one request may contain, counting the history and the new input.
- **Compaction**: when the history gets too long, Codex asks the model to write a short "handoff summary". It then
  throws the old history away and continues with only that summary. This is lossy, like replacing a lab notebook with
  a one-page memo.
- **Prompt cache**: the provider reuses work for an unchanged request prefix. `cached_tokens` counts those tokens.
- **Ledger**: the v9 L2 record of verified cells and candidate answers.

## TL;DR

1. **The config was not in effect.** Codex compacts at **244,800 tokens**, which is 90% of 272,000, not at the configured 800,000.
   In all 399 runs, Codex logged `Model metadata for 'accounts/fireworks/models/glm-5p3-flash' not found. Defaulting to fallback metadata`.
   The trigger data separate exactly at ~244.8K (see §4). `model_context_window=1048576` and
   `model_auto_compact_token_limit=800000` (`officeqa-pro133-v8-plumbing/acting.py:293-295`) are recorded in
   invocation.json but are inert.
2. **The provider did not cut anything near 245K.** v8 has zero HTTP errors and zero context-length messages. Older
   runs (v6f) succeeded with 326K–416K-token inputs. The 2026-09-24 diagnosis ("provider cuts streams near 245K;
   compaction never fires first", `MetaHarnessAdapter-next/docs/analysis/2026-09-24-mwm-agentic-coding-diagnosis.md:101-105`)
   is contradicted by the data. The ~245K ceiling is Codex's own compaction threshold.
3. **72 of 399 runs compacted (119 compactions), and they were nearly all wrong: 12/72 correct vs 169/327 for runs that did not compact.**
   Compaction marks a run that was stuck in a loop. 111/119 pre-compaction windows contain one command repeated
   ≥10 times, byte for byte. Only 1/327 non-compacted runs has that. 12 of the 13 v8 timeouts were compacted runs, and
   all 12 returned empty answers.
4. **What survives compaction:** the system instructions, the developer message, AGENTS.md (byte-identical in 118/118),
   the task prompt, and one model-written summary. Everything else is dropped: every tool call, every tool output
   and all reasoning. Summaries sometimes decide things, or carry wrong values, that the next segment then trusts.
5. **v9:** add a loop guard at 3 identical commands. It fires in 71/72 compacted runs at a median of 72.6K input
   tokens, and in only 5/327 non-compacted runs. Keep the effective limit at 244.8K, but set it explicitly.
   Re-inject the ledger by rewriting the compaction *response* in the broker. Codex then carries the ledger itself
   until the next compaction.

## 1. Per-request input size

Source: per-attempt `usage.input_tokens` in `api/NNN/transport.json`. This is what the provider counted, including
the reasoning items that the broker leaves out of the saved `request.json`. The broker removes them in `broker.py:41-47`,
so request.json byte sizes understate the real token counts.

| | n | p10 | p25 | p50 | p75 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|---|
| succeeded attempts, all arms | 22,069 | 14.3K | 35.5K | 85.4K | 159.7K | 211.1K | 242.6K | **247,847** |
| none | 7,036 | | | 72.8K | | 207.7K | | 246,654 |
| prompt | 6,348 | | | 99.9K | | 213.6K | | 246,813 |
| cards | 8,685 | | | 79.3K | | 211.9K | | 247,847 |

Histogram: <25K 4,021 · 25–50K 3,238 · 50–100K 5,034 · 100–150K 3,611 · 150–200K 3,234 · 200–230K 1,893 ·
230–240K 614 · 240–245K 388 · 245–250K 36 · **≥250K 0**.

- **Largest successful request:** 247,847 tokens. This was cards Q121 request 185, which was itself a compaction request.
  The largest tool-bearing request was 247,067 tokens (cards Q49 request 106).
- **Note on `transport.json` totals:** the request-level `usage` adds up all attempts. Five compaction requests show
  484K–491K only because they were sent twice (for example none Q53 request 152: 243,516 + 243,516). Use per-attempt usage.
- **Fixed prefix, meaning the first request's input:** none 7,329–7,548. cards 7,585–11,301 (AGENTS.md up to 17 KB).
  **prompt 7,662–84,438** (AGENTS.md grew to 231,899 bytes; the median for Q100–133 was 74K tokens). By the end the
  prompt arm spent about a third of the effective window on AGENTS.md before doing any work.
- **Growth per step:** median +1,463 tokens, p90 +4,175, p99 +5,770. Codex caps each tool output at about 10.2K chars:
  in the last requests of the none and cards arms, 986 of 5,611 outputs carry `…N chars truncated…`, and the maximum
  length was 10,222 chars.
- **Peak context of correct runs:** p50 50K, p90 112K, p95 243K. For correct runs that never compacted, the max was
  **158,215** and p95 was 104K.

### Provider errors

- **No context-length errors anywhere in v8.**
  - 0 `attempts/*/error.json` files. The broker writes one for every HTTP error (`broker.py:355`).
  - No hits for `context_length|maximum context|context window|too long|too many tokens|exceeds` in any
    transport.json, stderr.log, result.json or events.jsonl.
- **Non-success attempts (74 in total):**

| What happened | Count | Input size | Detail |
|---|---|---|---|
| `incomplete_without_output` at 65,536 output tokens | 49 | 12K–244K | Runaways (output that kept going until it hit the cap), re-sent by the broker (`broker.py:116-145`). 2 of the 49 were compaction requests. |
| `invalid_tool_arguments` | 4 | | Re-sent |
| `completed_without_output` | 3 | 242–245K | All three were compaction requests |
| Connection resets / `NoTerminalEvent` | 6 | 22K–216K | No clustering near the limit |
| `TimeoutError: Actor stopped` | 12 | | Run hit the time limit. 1 of these was a compaction request still in flight (prompt Q71). |

- **Compaction requests needed a re-send far more often:** 5/119 (4.2%), vs 51/21,967 (0.23%) for tool-bearing
  requests. The broker's re-send is what keeps a compaction from ending with no summary (see the comment at
  `broker.py:125-127`).

## 2. Recognising compaction, and when it happened

| Signal | Rule | Verified on v8 |
|---|---|---|
| Compaction request | `tools` missing or empty, **and** the last input item is a user message starting `You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.` | 119/119 tool-less requests. Every other request has exactly 5 tools (21,967/21,967). |
| Compaction input | The whole current history plus the prompt above. Same `instructions`. For example none Q66 request 65: 134 items. | |
| Compaction response | One assistant message, usually headed `**Handoff Summary**`. Output tokens: median 481, min 184. Summary 497–4,448 chars, median 1,493. | 118 completed |
| Next request | Exactly 4 input items: developer, user (AGENTS.md + `<environment_context>`), user (task prompt), user (`Another language model started to solve this problem and produced a summary … Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:` + summary). Tools come back (5). | 118/118 |
| events.jsonl | After each compaction, an `item.completed` item of type `error` with `Heads up: Long threads and multiple compactions can cause the model to be less accurate…`. `codex exec --json` has no dedicated compaction event. | Warnings match compactions in 398/399 runs. The exception is prompt Q71, where the 8th compaction was still in flight at timeout. |
| summary.json | `native_codex_items.error − 2` gives the compaction count. The two fixed errors are the unstable-feature warning and the fallback-metadata warning. The v8 summary.json does not record compactions directly. | |

- **Cost of a compaction:** 106 of the 118 completed compaction requests had `cached_tokens = 0` (median 0), so a
  compaction usually costs a full, uncached ~245K-token prompt. For the first request after a compaction, 96/118 had
  0 cached tokens.
- **Input size at compaction:** 241,474–247,847 tokens per attempt.
- **When it happened:** the first compaction came at request #40–527 (median 104.5), 255–2,809 s into the run (median 683 s).
- **Size after compaction:** median 9,826 tokens. none ~7.9K, cards ~10K, prompt 45K (max 78K, because of the large AGENTS.md).

### How many runs compacted, and did they get the answer right

| arm | runs compacted | compactions | correct if compacted | correct if not |
|---|---|---|---|---|
| none | 21 | 35 | 3/21 | 59/112 |
| prompt | 22 | 38 | 4/22 | 60/111 |
| cards | 29 | 46 | 5/29 | 50/104 |
| **all** | **72** | **119** | **12/72 (17%)** | **169/327 (52%)** |

- **Compactions per run:** 1× 51 runs, 2× 7, 3× 9, 4× 3, 7× 1, 8× 1. Runs with ≥2 compactions: 2/21 correct.
  Runs with ≥3: **0/14 correct**.
- **Status of compacted runs:** 60 completed (12 correct), 12 timed out (all empty). Non-compacted runs: 326 completed, 1 timed out.
- **Five questions compacted in all three arms:** Q10, Q28, Q32, Q35, Q100. 0/15 correct.
- **Controls for difficulty and length.** The gap shrinks but does not go away:
  - Within the 39 questions where some arms compacted and others did not: compacted arms 12/57, non-compacted arms 22/60.
  - By Opus result: on questions Opus got wrong, compacted runs 0/35 vs non-compacted 23/103. On questions Opus got
    right, 12/37 vs 146/224.
  - By request count 40–80, the only bucket that has both kinds: 3/10 vs 4/10.
  - Correctness also falls with context size even without compaction. Among non-compacted runs, peak <100K: 154/288;
    100–150K: 11/29; 150–200K: 2/6.
- **Conclusion:** compaction is mostly a *symptom* of a derailed run, not its cause. These data cannot pull out the
  harm it adds on top.
- **Where the harm does show up:** 20 of the 54 per-question analyses mention compaction. Examples from
  `officeqa-divergence-analysis/per-question/`:
  - The handoff summary itself chose the vintage, and GLM then trusted it (081 none).
  - The compacted summary held wrong values (105 prompt).
  - The run committed "from the summary" without re-checking (044 none, 095 none).
  - The run restarted from README after every compaction (071 prompt: 7 compactions; 071 cards: 4).

### What causes compaction: loops

| Measure | Result |
|---|---|
| Pre-compaction windows (commands since the previous compaction) | median 101 commands, median distinct/total ratio **0.21** |
| Windows with one command repeated identically ≥10 times | 111/119 |
| Same, ≥3 times | 118/119 |
| Non-compacted runs with a command repeated ≥10 times | 1/327 |
| Non-compacted runs with a command repeated ≥3 times | 5/327 |
| Compacted runs where a 3×-identical-command guard would fire | **71/72**, at median input 72.6K tokens (p90 120.7K), median command #27 of 151, far before the 244.8K compaction |
| Non-compacted runs where that guard would fire | 5/327 (1 of them correct) |
| Post-compaction commands that exactly repeat a pre-compaction command | 16/7,010 (0.2%) |

The last row means the re-work after a compaction is *paraphrased*. An exact-match loop guard will not see it; the
ledger has to carry what has already been done.

**Tokens:** compacted runs used **87.9%** of all v8 actor input tokens (1.95B of 2.22B).

## 3. What survives compaction

From comparing, for all 118 completed compactions, the request right after the compaction with request 001 of the same run:

- **`instructions` (the Codex system prompt):** unchanged.
- **Developer message (skills and permissions):** identical in 118/118.
- **AGENTS.md: yes, it is re-sent, byte-identical in 118/118.**
  - The `<environment_context>` part of the same message changed only in 4 runs that crossed midnight
    (`current_date` 2026-09-26 → 09-27). So Codex rebuilds the initial context at compaction.
  - Whether it re-reads AGENTS.md *from disk* was not tested: AGENTS.md never changed during a run, because it is
    hash-checked at `acting.py:268`.
  - Static rules and notes in AGENTS.md survive compaction. The prompt arm's 124–232 KB AGENTS.md was re-sent in full.
- **Original task prompt:** identical in 118/118.
- **Summary:** exactly one per request, always the latest.
  - Earlier summaries are folded in: the previous summary was part of the next compaction's input in 46 compaction
    requests. None survives on its own.
- **Dropped:** every `function_call` and `function_call_output`, and all reasoning.
  - Codex's resume prefix still says "You also have access to the state of the tools that were used". That is false
    for shell output.
- **What the summaries contain:**
  - The structure the prompt asks for: Task / Progress / Next steps / Constraints.
  - 100/118 name a `treasury_bulletin_YYYY_MM.txt` file.
  - Only 22/118 give file:line-style references.
  - 93/118 contain numbers. 83/118 use candidate or answer wording.
  - Many repeat environment lessons, for example "heredocs fail … `rg` is unavailable" (none Q66 request 66, cards Q98 request 59).
  - Values are copied by the model with no provenance check.

## 4. Codex config vs the limits we actually observed

| Item | Value | Where |
|---|---|---|
| Base profile | `model_context_window` 196,608 / `model_auto_compact_token_limit` 160,000 | `officeqa-pro133-v8-plumbing/acting.py:284` |
| FULL profile override (used in v8) | 1,048,576 / 800,000, plus `agents.enabled` | `acting.py:293-295`; `FULL = PROFILE.startswith('full_')` at `experiment.py:10`; the none root's `.task-paths.json` has `profile: full_cards_unbounded` |
| Recorded in each run | settings and argv carry 1048576 / 800000 | `runs/*/NNN/records/invocation.json` |
| Codex warning | `Model metadata for … glm-5p3-flash not found. Defaulting to fallback metadata` | events.jsonl `item_1`, 399/399 runs |
| Bundled metadata in the binary | `"context_window": 272000` (8×), `372000` (1×), `"auto_compact_token_limit": null` (9×) | `strings frameworks-v8/codex-0.155.0-alpha.9.2/codex` |
| Observed trigger | Measure = last response's input + output + (bytes of new tool outputs)/4. All 119 triggers ≥ **244,864**; all 1,132 non-triggers with input+output ≥ 225K are ≤ **244,858**. Input + output alone: non-trigger max 244,707; nothing at or above 244,800 went uncompacted. | `thresh.py` |
| Effective limit | **244,800 = 0.9 × 272,000.** 800,000 is ruled out: no request exceeded 247,847. | |
| Provider limit | Not reached in v8. Fireworks' declared `context_length` is 1,048,576 according to the comment at `MetaHarnessAdapter-next/tasks/OfficeQA/host/acting.py:38` (not verifiable offline). | |
| Largest input ever accepted | 415,655 (`officeqa-codex50-full-mwm-v6f-work/runs/adapted/038/api/173`), 412,898 (`…prompt-only-v6f…/adapted/005/api/038`), 326,743. All HTTP 200, `completed`. | |

**How v6f got past the limit.** Codex re-sent incomplete 65,536-token runaways, and their reasoning stayed in the
history. For example, v6f prompt Q5 jumped from 149,725 to 412,898 tokens in one step, *after* which Codex compacted.
The v8 broker's re-send of runaways (`broker.py:116-145`) stops this, which is why the v8 maximum is 247,847.

**The "provider cuts at 245K" claim.**
- v5 (`officeqa-codex50-full-mwm-v5-work`), which the 2026-09-24 diagnosis cites, shows the same Codex behavior:
  16 tool-less compaction requests at 241–245.5K, and a maximum input of 245,502.
- Its 20 `No terminal Responses event` failures do not cluster near the limit:
  - 17 of 20 had inputs ≤25K (mostly request 001, during a provider outage window).
  - One came at 81K and one at 199,670.
  - One was a compaction request (previous request 244,679).
- The 2026-09-26 baseline-variance audit ("auto-compaction fires ~245K (fallback model metadata), not 800K") agrees
  with the data. The 2026-09-24 wording does not.

**Other config keys in the 0.155 binary.** The strings include `model_catalog_json`, `compact_prompt`,
`experimental_compact_prompt_file`, `tool_output_token_limit`, `auto_compact_fallback_prompt`,
`auto_compact_fallback_buffer_tokens` and `context_window_reminder`. I could not check their meaning or schema
offline (no network, no source), so treat them as candidates to test, not facts.

## 5. Recommendations for v9

### Settings (identical for all arms; part of L0)

1. **Stop recording a limit that is not in effect.** Two options:
   - **Preferred:** give Codex real metadata for `glm-5p3-flash`, probably through `model_catalog_json`.
     - Use `context_window` 272,000, which keeps the effective auto-compact at 244,800 as in v8, but now declared.
     - Confirm with one smoke question: (a) the fallback-metadata warning disappears; (b) a deliberately low limit,
       e.g. 60K, compacts at 60K ± 1K; (c) request 001's `tools` and `instructions` are byte-identical to v8, or every
       difference is listed as an L0 change. Fallback metadata may also set the ~10 KB tool-output truncation.
   - **If the key does not work:** delete the inert overrides at `acting.py:293-295` and write
     `effective_auto_compact_limit_observed: 244800` into invocation.json.
2. **Do not raise the limit.**
   - No correct, non-compacted v8 run went past 158K.
   - The windows that filled up were 79% repeated commands.
   - A larger window would let loops run longer, with larger and slower requests, under the same 3,600 s budget
     (`experiment.py:22`).
3. **Do not lower it much either.** 12 compacted runs still ended correct. With the loop guard below in place,
   compaction should become a rare backstop.
4. **Budget the fixed prefix at ≤8K tokens** for AGENTS.md plus L5 notes combined.
   - AGENTS.md is re-sent in full after every compaction.
   - The v8 prompt arm reached 84K tokens before its first command, and restarted each compaction at 45–78K.
   - L5 notes should be retrieved by trigger, never shown as a whole deck.
5. **Tool output:** keep Codex's ~10 KB cap as a safety net, but have the L1 tools (`show_table`, `printings`,
   `find_tables`) print ≤8,000-byte pages that state "rows a–b of N; next: `--page k`". Then `…chars truncated…`
   (17.6% of sampled v8 outputs) never cuts a table silently. Do not raise `tool_output_token_limit`: bigger outputs
   fill the window faster, since each step already adds a median of 1.5K tokens (p90 4.2K).
6. **L3 loop guard:** trigger on the same exact `cmd` run 3 times. Inject a nudge plus the ledger, and at the same
   time point out that the question is still open. It would have fired in 71/72 compacted runs at a median of 72.6K
   tokens, with 5/327 false alarms.
7. **L3 at timeout:** submit the ledger's best candidate. 12/13 v8 timeouts were compacted runs that returned `""`.
8. **Keep the broker's defect re-send.** Without it, 5/119 compactions would have produced no summary.
9. **Records:** add `compactions`, `compaction_request_numbers`, `ledger_injections` (bytes and hash) and
   `effective_auto_compact_limit` to `summary.json` / `api/summary.json`, using the detection rules in §2.

### Where the ledger re-injection should hook in

All the hooks below are in the broker. Codex owns the history, and the broker already buffers each whole response
before forwarding it: `final, wire, … = value` at `broker.py:401`, and `self.wfile.write(wire)` at `broker.py:413`.
The comment at `broker.py:336` notes that "the whole wire is buffered".

- **H1 (primary): rewrite the compaction response.**
  - When the request is a compaction (the §2 rule, tested at `broker.py:262` on `outgoing`), take the
    `workspace/.ledger/ledger.jsonl` written by the L2 `ledger` CLI.
  - Render it to at most 2K tokens (~8 KB): every candidate with its calc id; every cell a candidate cites (file:line,
    value, unit, as-of date, printing); the last 10 cells; and "full: `ledger show`".
  - Append it to the summary's `output_text` under a header such as
    `LEDGER (tool-recorded, verbatim; authoritative where the summary disagrees)`.
  - Re-serialize the SSE (server-sent events, the streaming format of the response) before line 413:
    `output_text.delta`/`done`, `output_item.done` and `response.completed`.
  - Why this hook:
    - Codex stores the ledger inside the summary message (input item 3) and re-sends it on every later request with
      no per-request work.
    - The prompt-cache prefix stays stable.
    - It is visible in every saved `request.json` for audit.
    - The next compaction sees it in its own input.
- **H0 (together with H1): show the ledger to the summarizer.** Append the rendered ledger to the compaction prompt
  message in `outgoing` at `broker.py:262`, with the line "do not restate or re-decide these values; the ledger will
  be attached verbatim". This targets the failures in 081 none (summary chose the vintage) and 105 prompt (wrong
  values in the summary).
- **H2 (fallback if H1's SSE rewrite proves brittle):** on each post-compaction request (input[3] starts with
  `Another language model started`), append the rendered ledger as a *trailing* user message in `outgoing`, before
  the save at `broker.py:280`. This must repeat on every request, because Codex's history never holds it. Putting it
  last keeps the cache prefix.
- **One renderer for everything:** loop-guard nudges, time-budget nudges and the commit gate's feedback should use
  the same `render_ledger()` function, so the model always sees the same authoritative block.
- **Do not put the ledger in AGENTS.md.** AGENTS.md survives compaction, but it is hash-frozen before the run
  (`acting.py:268`), and v8 did not show that Codex re-reads it from disk.
- **Zero-cost offline tests before any paid run:**
  - Run the detection rules over all v8 requests. Expected: 119/119 compaction requests and 118/118 post-compaction
    requests, with 0 false positives among 21,967 tool requests.
  - Run the SSE rewrite on synthetic streams built from the 118 saved compaction `response.json` files, and parse the
    result back with `read_wire` (`broker.py:152`).
  - Then run one paid smoke question with a forced low limit. Check that the request after compaction has the LEDGER
    block in input[3], and that `events.jsonl` shows exactly one `Heads up` item.
- **Precedent in this codebase:** the learner loop already does "summary + deterministic state".
  `cards_learning.py:343-354` re-injects `Proposals already accepted for verification: …` after its own checkpoint.
  H1 is the same idea placed where the Codex actor can use it.
