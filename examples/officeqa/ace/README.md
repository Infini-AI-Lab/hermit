# OfficeQA with ACE

[ACE](https://arxiv.org/abs/2510.04618) (Zhang et al., ICLR 2026; [code](https://github.com/ace-agent/ace)) on
OfficeQA in the SkillOpt paper's setting, offline: the [`ace`](../../../docs/methods/ace.md) Evolver grows a playbook
on the 50 training questions, keeps the playbook with the best score on the 24 validation questions, and the fixed
final adapter is evaluated on the 172 test questions. The adapter is SkillOpt's initial skill followed by ACE's
playbook ([`ace`](../../../hermit/algorithms/templates/ace/) template); the acting agent, its Codex harness and the
models (GLM 5.3 Flash acting and learning, medium effort) are those of the all-Flash GEPA and SkillOpt runs, so the
three methods compare on one row of settings.

```bash
python examples/officeqa/ace/make_seed.py                         # SkillOpt's initial skill + an empty playbook
export OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0
hermit check --config examples/officeqa/ace/smoke_codex.yaml
hermit train --config examples/officeqa/ace/smoke_codex.yaml      # 4 questions one by one, 2 validation, then eval 4
hermit train --config examples/officeqa/ace/run_codex_flash.yaml  # 50 training questions, 2 epochs
hermit eval  --config examples/officeqa/ace/run_codex_flash.yaml --adapter final
hermit eval  --config examples/officeqa/ace/run_codex_flash.yaml --adapter seed_adapter   # the starting adapter
```

## On your own Codex login (ChatGPT account)

`smoke_codex_login.yaml` and `run_codex_login.yaml` run everything on the Codex login of the machine (`codex login`
with a ChatGPT account; no API key): each acting session's container gets a copy of the login
(`agent.params.login: chatgpt`), and ACE's Reflector and Curator are `codex exec` sessions under it
(`evolver.params.learner: codex`). The model is the login's default unless `agent.params.model` and
`evolver.params.learner_model` name one.

```bash
codex login                                              # once, on this machine
# the Codex sessions' proxy must reach ChatGPT instead of Fireworks (or start a second proxy and set OFFICEQA_CODEX_PROXY)
docker rm -f oq-egress
docker run -d --name oq-egress --restart unless-stopped --read-only --network bridge "$OFFICEQA_CODEX_IMAGE" \
  python3 -u /opt/egress_proxy.py 3128 chatgpt.com:443 auth.openai.com:443
docker network connect oq-sandbox oq-egress
hermit check --config examples/officeqa/ace/smoke_codex_login.yaml
hermit train --config examples/officeqa/ace/smoke_codex_login.yaml
hermit train --config examples/officeqa/ace/run_codex_login.yaml
hermit eval  --config examples/officeqa/ace/run_codex_login.yaml --adapter final
hermit eval  --config examples/officeqa/ace/run_codex_login.yaml --adapter runs/login-codex-ace/workspace/state/last_adapter
hermit eval  --config examples/officeqa/ace/run_codex_login.yaml --adapter seed_adapter
hermit eval  --config examples/officeqa/ace/run_codex_login.yaml --adapter none
```

The image's Codex must know the model your login uses (build it with your Codex version if 0.162.0 does not). The
recipe's numbers are for GLM 5.3 Flash: on another model, the comparison is against `seed_adapter` and `none`
evaluated with this same config. The login's copy for the run is under `codex_dir/login/`; a session that
refreshes its token writes it back there, never to `~/.codex`. A plan's usage limits apply to every session: a
session refused for its limit is retried, then recorded as an error, so keep `agent.concurrency` modest.

## What to read after the smoke

The smoke is not a measurement; it shows whether each of ACE's levers works in a real session. In
`runs/smoke-codex-ace/`:

- `workspace/state/ace.json`, per sample: `cited` (the bullet ids the agent named; empty everywhere means the agent
  does not use the playbook's ids and the counters cannot move), `tags` / `rounds` (the Reflector's tags and the
  retry scores), `curator` (`applied` or why it was skipped) and `added`;
- the steps' `output/calls/`: every Reflector and Curator prompt and reply;
- `checkout/ADAPTER.md`: the skill unchanged at the top, the bullets under their sections, counters above zero;
- the training rollouts' Codex traces: that the agent read the skill and named bullets in its reasoning.

## The evaluation protocol

The split is the SkillOpt paper's (50 / 24 / 172), kept so that ACE joins the other methods' table. Two of its
sizes shape what a run can show:

- 24 validation questions: one question is 4.2 points, the standard error of a validation score about 10 points,
  so choosing between two epochs' playbooks on it is mostly noise. Both the best-validation playbook (`final`) and
  the last one (`workspace/state/last_adapter`, saved by finalize) are evaluated.
- 172 test questions: an accuracy's standard error is about 3.7 points, and the 95% interval of a paired difference
  about ±7 (from the Continual Harness stream's wins and losses). Each adapter is evaluated with three passes
  (`eval.repeats: 3`), which reduces the sessions' own variance but not the sampling of the questions; a gain of a
  few points needs repeated trainings (another `trainer.run_name` and `data.shuffle_seed`) to be told apart.
- 50 training questions are few for a playbook method (the paper's finance tasks train on 500 to 1,000): a small
  gain says as much about the data as about the method.

Report each adapter's paired difference to the starting adapter with its 95% bootstrap interval over questions:
a positive difference is a sign of an effect; a lower bound above zero, a significant one.

## Settings and differences

ACE's per-sample loop is `data.step_size: 1`: one rollout, its reflection (and up to three retry sessions for a
wrong answer), the Curator, then the next question. Two epochs; validation at each epoch's end. The Reflector sees
the judge's feedback, which names the expected answer, so this is ACE with labels, the paper's offline setting.
The full table of differences from the authors' code is on the [method page](../../../docs/methods/ace.md).
