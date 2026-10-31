# The Flash row with Codex + GPT-5.6-Luna

The seven methods of the [Flash row](../README.md#results) (no skill, SkillOpt's initial skill, GEPA, SkillOpt,
Continual Harness, ReHarness, Harness²) with Codex and GPT-5.6-Luna (reasoning effort medium) both acting and
learning, on a machine's own Codex and its ChatGPT login (a Codex plan) instead of GLM 5.3 Flash on Fireworks. The
data, split, seed skill, judge, evolvers' parameters, reasoning effort, session limits (SkillOpt's 180 s × 24
sessions; 3,600 s for the streams), configured concurrency, ReHarness's embedding model (Qwen3-Embedding-8B on
Fireworks behind its launcher's wire meter: the plan serves no embeddings) and one test pass are the Flash row's. The
first six ran from `worktree-officeqa-luna` (base 2056e43), ReHarness from this branch, which merges main (0d3df08).
What differs:

| Setting | Flash row | Here |
|---|---|---|
| acting model | `fireworks/accounts/fireworks/models/glm-5p3-flash` through a Codex custom provider | `codexplan/gpt-5.6-luna`: Codex's built-in provider on a per-session copy of the ChatGPT login |
| Codex's tools and prompt for the model | GLM is not in Codex's model catalog: default function tools (`exec_command`), the 20,751-character fallback prompt | Codex fetches gpt-5.6-luna's catalog entry from chatgpt.com at each session start: code mode only (a JavaScript `exec` tool that calls `exec_command`), a 17,730-character base prompt, verbosity low, tool output cut at 10,000 tokens; it stayed the same throughout the runs |
| learner (`llm`: GEPA, SkillOpt, Continual Harness, ReHarness's reflection and revision agents) | a chat completion to the same GLM (system prompt as such, `max_tokens` applied, 600 s timeout) | `codexrelay/gpt-5.6-luna`: each call is one `codex exec` in a container (`../utils/agent/codex_chat.py`), with Codex's base prompt and the call's system text as developer instructions; `max_tokens` is recorded, not applied (no reply came near one); `llm.timeout` 3,600 s |
| Harness² editors | Claude Code (`claude -p`) on GLM through a wire meter | Codex sessions (`../harness2_stream/codex_proposer.py`) in hermit's sandbox, `sandbox.allow: [chatgpt.com]`, `evolver.params.backend: codex`, a tree of its own |
| Codex | 0.162.0 (npm package), image `officeqa-codex:0.162.0` | the machine's standalone 0.164.0, pinned, image `officeqa-codex:0.164.0` (`../utils/agent/docker/Dockerfile.standalone`) |
| egress | `oq-egress`: api.fireworks.ai:443 only | `oq-egress-chatgpt`: chatgpt.com:443 only |
| concurrency on the machine | each run's own | each SkillOpt-harness rollout and each training learner call also holds one of the machine's session slots (below) |
| run names, workspaces | `flash-codex-gepa`/`-skillopt{,-r2,-r3,-r4}`, `ch-stream-s0`, `rh-stream-s0`, `h2-codex-s0`; `~/.cache/hermit/...` | `luna-gepa`/`-skillopt{,-r2,-r3,-r4}`, `luna-ch-s0`, `luna-rh-s0`, `luna-h2-s0`, `luna-reference`; `~/albz/officeqa-luna/cache/...` |

The model change cannot be separated from the second, third and fourth rows: they come with running GPT-5.6-Luna in
Codex on the plan.

## Setup (once)

```bash
cd examples/officeqa/utils/agent/docker
cp -a ~/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl codex-standalone
docker build -f Dockerfile.standalone -t officeqa-codex:0.164.0 .
docker run -d --name oq-egress-chatgpt --restart unless-stopped --read-only --network bridge officeqa-codex:0.164.0 \
  python3 -u /opt/egress_proxy.py 3128 chatgpt.com:443
docker network connect oq-sandbox oq-egress-chatgpt
mkdir -p ~/albz/officeqa-luna/codex-pin
cp -a ~/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl ~/albz/officeqa-luna/codex-pin/0.164.0
```

The machine's Codex must be logged in with ChatGPT (`codex login`). Each session gets a copy of `~/.codex/auth.json`
in its fresh CODEX_HOME (one copy for all sessions of a SkillOpt-harness rollout), removed when it ends; a session
killed mid-run leaves its copy behind, so run `launch.sh sweep` after the runs (66 were left by the runs below). The
proxy does not let auth.openai.com through, so no session can refresh, and thereby rotate, the machine's login: the
login's access token must outlive the runs (`python ../utils/agent/codex_login.py` prints when it expires).

## Run

```bash
OFFICEQA_CODEX_NO_SLOTS=1 RELAY_PORT=3671 RELAY_CONCURRENCY=6 examples/officeqa/luna/launch.sh relay   # the streams' learner
RELAY_PORT=3672 examples/officeqa/luna/launch.sh relay                                                 # the trainings' learner, under the slots
python examples/officeqa/luna/slots_ctl.py --min 2 --max 16 &                                          # sets SLOTS as it goes
examples/officeqa/luna/launch.sh none          # no skill: skillopt/runs/luna-reference/eval/none
examples/officeqa/luna/launch.sh initial       # initial skill: skillopt/runs/luna-reference/eval/seed_adapter
RELAY_PORT=3672 examples/officeqa/luna/launch.sh gepa 1       # ... gepa 4: gepa/runs/luna-gepa{,-r2,-r3,-r4}, then eval/final
RELAY_PORT=3672 examples/officeqa/luna/launch.sh skillopt 1   # ... skillopt 4
examples/officeqa/luna/launch.sh ch            # continual_harness/runs/luna-ch-s0 (steps.jsonl: the stream)
examples/officeqa/luna/launch.sh h2            # harness2_stream/runs/luna-h2-s0 (no relay: its learner is the editors)
MW_KEY_FILE=... WM_PORT=3691 examples/officeqa/luna/launch.sh rh   # reharness/runs/luna-rh-s0 (embeddings: Fireworks)
examples/officeqa/luna/launch.sh sweep         # afterwards
```

A stream stopped mid-run continues after its last completed step when started again under the same run name.

## Plan usage

The plan's usage is shared with everything else on the account; `~/albz/officeqa-luna/plan.json` holds its latest
report and `plan.jsonl` every report. The runs set no cap of their own: sessions wait only while
`~/albz/officeqa-luna/PAUSE` exists, or after a session was refused for the plan's limit, until the plan's window
resets (a number in `~/albz/officeqa-luna/MAX_USED` sets a cap in percent, read while runs go on). A session refused
for the plan's limit is run again after the wait; it is never scored as a wrong answer. All runs together took the
weekly window from 28-29% to 38% (whole percents, account-wide, so an upper bound on their share); no session was
refused and the credits balance did not change.

## The login's rate limit

The login is rate-limited, and its slow state outlasted the load that tripped it. From the first evaluations
(01:18) until about 13:40 the first model reply of a session took a median 33-54 s; afterwards 1.4-2.1 s. The two
reference evaluations ran in the slow state: 4 and 3 of their sessions hit the 180 s cap and score 0 (at most -2.3
and -1.7 points, most likely about -0.6 each). With the eight trainings side by side and no limit (about sixty
sessions at once) the backend answered 429 Too Many Requests, first replies took a median 100-400 s, and 32-45% of
the training rollouts of three successive batches timed out (25-67% per run in the first); those batches were
discarded (`~/albz/officeqa-luna/aborted-429*`).

The trainings behind the results: from 03:17 every SkillOpt-harness rollout and every training learner call (relay
3672) held one of the machine's session slots (`~/albz/officeqa-luna/SLOTS`, read at every try; codex_login.slot); a
rollout takes its slot before its first session starts, so no session's 180 s is spent waiting for one. The streams'
sessions take none, and from 04:34 their learner calls (Continual Harness's and ReHarness's, relay 3671) neither.
`slots_ctl.py` set the number; three versions ran in turn: a ten-minute window that cut two slots at more than 6%
timed-out rollouts or more than 30% 429s and added one after ten quiet minutes (03:17-10:36); the same on timeouts
mainly, 429s only above half of the logged sessions (10:36-13:37); and the one in this folder, which counts the last
30 minutes' rollouts and cuts only for at least two timeouts (13:37-19:16). The slots kept the sixty-session overload
from returning, but did not lower the timeout rate while the account was slow (4.4% at 2 slots, 4.6% at 3-5); its
recovery did. The first wave of trainings (`luna-gepa`, `-r2`, `luna-skillopt`, `-r2`, launched 03:30) trained
11-15 h in the slow state and learned from 0.8-2.1% timed-out rollouts (14, 11, 12, 13), spread over the runs; the
second (`-r3`, `-r4`, from 14:18) from 0-3. The eight final evaluations ran after the recovery: no timeouts.

"Selected model is at capacity" (the backend's, now and then) ends a session; from cc051d9 (04:33) such a session is
run again later like one the provider could not serve. The first wave started before that, and in SkillOpt's
harness the rule could not see the message at all (SkillOpt keeps the first 4,000 characters of a failed session's
output; Codex prints the reason at the end) until the agent read Codex's own ERROR lines from the rollout's raw
output: `luna-gepa` lost 2 seed-validation rollouts to it, scored as wrong.
