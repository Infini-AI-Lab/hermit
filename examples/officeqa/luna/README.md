# The Flash row with Codex + GPT-5.6-Luna

The six methods of the [Flash row](../../../docs/recipes/officeqa.md#the-flash-row) (no skill, SkillOpt's initial
skill, GEPA, SkillOpt, Continual Harness, Harness²) with Codex and GPT-5.6-Luna (reasoning effort medium) both
acting and learning, on a machine's own Codex and its ChatGPT login (a Codex plan) instead of GLM 5.3 Flash on
Fireworks. Every other setting is the Flash row's config; `launch.sh` changes only:

| Setting | Flash row | Here |
|---|---|---|
| acting model | `fireworks/accounts/fireworks/models/glm-5p3-flash` | `codexplan/gpt-5.6-luna` (Codex's built-in provider on the ChatGPT login) |
| learner (`llm`: GEPA, SkillOpt, Continual Harness) | the same GLM on Fireworks | `codexrelay/gpt-5.6-luna`: each call is one Codex session (`../utils/agent/codex_chat.py`) |
| Harness² editors | Claude Code (`claude -p`) on GLM through a wire meter | Codex sessions (`../harness2_stream/codex_proposer.py`) |
| Codex | 0.162.0 (npm package), image `officeqa-codex:0.162.0` | the machine's standalone 0.164.0, pinned, image `officeqa-codex:0.164.0` (`../utils/agent/docker/Dockerfile.standalone`) |
| egress | `oq-egress`: api.fireworks.ai:443 only | `oq-egress-chatgpt`: chatgpt.com:443 only |
| run names, workspaces | `flash-codex-*`, `~/.cache/hermit/...` | `luna-*`, `~/albz/officeqa-luna/cache/...` |

Reasoning effort (medium), the harnesses and their limits (SkillOpt's 180 s × 24 sessions; the 3,600 s Codex session
of Continual Harness and Harness²), the split, the seed skill, the evolvers' parameters, concurrency, one test pass
and the judge are the Flash row's.

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
in its fresh CODEX_HOME for that session only. The proxy does not let auth.openai.com through, so no session can
refresh, and thereby rotate, the machine's login: the login's access token must outlive the runs
(`python ../utils/agent/codex_login.py` prints when it expires).

## Run

```bash
examples/officeqa/luna/launch.sh none          # no skill: skillopt/runs/luna-reference/eval/none
examples/officeqa/luna/launch.sh initial       # initial skill: skillopt/runs/luna-reference/eval/seed_adapter
examples/officeqa/luna/launch.sh gepa 1        # ... gepa 4: gepa/runs/luna-gepa{,-r2,-r3,-r4}, then eval/final
examples/officeqa/luna/launch.sh skillopt 1    # ... skillopt 4
examples/officeqa/luna/launch.sh ch            # continual_harness/runs/luna-ch-s0 (steps.jsonl: the stream)
examples/officeqa/luna/launch.sh h2            # harness2_stream/runs/luna-h2-s0
```

The plan's usage is shared with everything else on the account. Sessions wait while the latest report of the plan's
usage is at or above 90% of its window (`~/albz/officeqa-luna/MAX_USED` changes the cap while runs go on;
`~/albz/officeqa-luna/plan.json` holds the report), or while `~/albz/officeqa-luna/PAUSE` exists. A session refused
for the plan's limit is run again after the wait; it is never scored as a wrong answer.
