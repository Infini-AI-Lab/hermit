# S2 review — requests to other streams

None of these block S2. The S2 implementer's own requests (`S2-pack-requests.md`, items 1–7) still stand; the items below are new and come from the review.

1. **S4 ledger: the pack gets less than the model saw after a Codex truncation.** (`harness_core/ledger.py`, `Ledger._output` → `observable_text`)
   - `observable_text` deletes every physical line that contains a `…N chars truncated…` marker before `pack.observations` runs. `officeqa.py read` prints its result as one physical line (a dict or JSON), so one marker removes the whole result.
   - `packs/officeqa/observe.py` already handles markers itself. It drops only the cut fragments, including the logical lines inside a one-line dict, and a test pins that behaviour.
   - Measured on all 2,779 tool outputs of the v8 none arm (final requests; scratch script `ledgerpath.py`):

     | input to `pack.observations` | outputs with at least one located observation | located observations |
     |---|---|---|
     | `view.body` (markers kept) | 943 | 52,647 |
     | `observable_text(view.body)` (today) | 825 | 42,147 |

   - Request: pass `view.body` with the markers kept to packs that handle markers. This could be a pack attribute such as `handles_truncation = True`. Keep `observable_text` for the core raw-line path.
   - Note: the S2 report's v8 coverage figures were measured without `observable_text`, so they overstate what the ledger records by about 20%.

2. **S4 ledger: `pack.observations` gets the command with its whitespace collapsed.** (`Command.cmd = normalize_command(...)`)
   - Newlines become spaces, so a script such as `show_table a 1⏎show_table b 2` or `cd /corpus⏎sed -n '10,20p' f.txt` reaches the pack as one simple command.
   - The pack now works around the tool case by splitting the arguments at a later tool name (`observe.invocations`). The `cd …⏎sed …` case still yields raw lines only.
   - Request: pass the raw command text as well. Either add an optional `raw_command` argument to `observations`, or keep the raw text on the `Command` event.

3. **S4/S3: pack observations are computed while the ledger lock is held.**
   - `Ledger.ingest_request` holds `self._lock` while `pack.observations` re-runs pack tools, so `/v1/submit` and the relay thread wait.
   - After this review the worst case is bounded: at most 6 tool re-runs per command, and each `printings` re-run takes at most about 2.9 s cold (it was about 10 s, and 32 s for three in one command). That is still seconds.
   - Request: compute the observations outside the lock, since the function is pure, then append the events under the lock. Or run it on a worker and splice the events in `call` order.

4. **Test configuration owner (repeat of S2 request 2):** register the `slow` marker. The S2 fast and slow sets still print `PytestUnknownMarkWarning`.
