# OfficeQA through Harbor

This recipe runs OfficeQA Full (246 questions about U.S. Treasury Bulletins) as the
[OfficeQA paper](https://arxiv.org/abs/2603.08655) evaluates agents: each question in its own Harbor environment
(`environment.harbor`) with the whole corpus, the paper's agent prompt, and the paper's scorer on the final message's
`<FINAL_ANSWER>`. Harbor starts the task's container, Hermit's acting agent (Codex, or Claude Code) works in it with the
adapter deployed, and the task's verifier, uploaded only after the agent stopped, scores its final message. The code
and configs are in `examples/officeqa_harbor/`
([README](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa_harbor)). The [OfficeQA](officeqa.md)
recipe runs the same questions in the SkillOpt paper's setting (its prompt with each question's source pages, its exact
match); the two give numbers of their own.

## The tasks

| | |
|---|---|
| Tasks | [harbor-datasets](https://github.com/laude-institute/harbor-datasets) `datasets/officeqa` at `37db108` (2026-03-17): their questions, answers, corpus image and metadata, the dataset as the paper (2026-03-09) had it. The revisions of `databricks/officeqa` on Hugging Face since (2026-06 and 2026-07) reword 8 questions and change 8 answers; these tasks keep the paper's |
| Corpus | 697 parsed bulletins at `/app/corpus/`, in the corpus image pinned by digest |
| Instruction | the paper's agent prompt (Appendix E.5), the corpus at `/app/corpus/`, without its web-search sentence, then `Question:` and the question, together the session's prompt (the paper does not say how its agents receive the question) |
| Answer | the first `<FINAL_ANSWER>...</FINAL_ANSWER>` of the agent's final message (the whole message without one), as the paper's scorer takes it |
| Score | the OfficeQA repository's `reward.py` as the paper scored with it (`databricks/officeqa` at `2a2c3b0`, 2026-03-02, Apache-2.0; the copy Harbor's tasks carry): the expected numbers found in the answer within an allowable relative error. The reward is the paper's headline threshold, 0.0%; 0.1%, 1% and 5% are recorded beside it. The repository's later revisions add a direct-answer guard and take the last tag; these numbers use none of them |
| Split | 50 train, 24 validation and 172 test questions (`utils/splits/`, [SkillOpt's split](https://github.com/microsoft/SkillOpt/tree/79124b37e9a6371e13b753f8bcd7adb1e493ade1/data/officeqa_id_split)); 93 of the 172 are labelled `hard`, OfficeQA Pro's |
| Model | GPT-5.6-Luna through Codex on a ChatGPT login, or Claude Haiku 5.5 through Claude Code on a Claude subscription, at medium effort (no API key) |

## Prepare the tasks

`prepare.py` prepares the set itself rather than through `python -m hermit.harbor.dataset prepare`: OfficeQA is not in
Harbor's registry at that commit, so no `dataset.ref` names it. The tasks it writes must match `utils/dataset.lock`
(`--relock` after a deliberate change). It writes the new tasks, index and lock to `utils/.staging/` first and replaces
the previous ones only after the check against the lock and the image's build passed, so on any failure the last
successful setup stays as it was.

It changes Harbor's tasks in four ways, and removes from each its answer's source (`source_files` and `source_docs` in
the metadata and `environment/resources/manifest.json`, which the paper's full-corpus agents are not given) and
`solution/` (Harbor's oracle, which writes the `/app/answer.txt` this verifier does not read):

- **The instruction.** The paper's agent prompt replaces Harbor's, which asked for `/app/answer.txt` and announced a 1%
  tolerance.
- **The verifier.** `utils/verifier/`, with the tasks' own `reward.py`, the paper's (checked against its git blob),
  instead of Harbor's verifier, which reads `/app/answer.txt` at 1%. The feedback a learning method reads is `passed`,
  or `reward 0` with the verifier's line naming the agent's own final answer; never the expected one. The final message
  is the agent log's `last_message.txt`; where the log has none (Harbor's timeout ended the phase before Hermit
  delivered it), the verifier reads Codex's event stream only, so a Claude Code session cut off so scores 0. A verifier
  that fails writes no reward: the run records it as `agent_error` (`verifier_failed`) and scores it 0, as Harbor
  grades it. A record's `judge.extra.rewards` also holds the verdicts at 0.1%, 1% and 5%, which say how close a wrong
  answer came; a method that reads them reads that much more than the verdict.
- **The image.** The corpus image with Codex installed (`utils/Dockerfile`), `officeqa-corpus-codex:0.164.0`, so that
  an environment installs nothing. Harbor installs Claude Code in each container.
- **The network.** The tasks declare `allow_internet = true`. Here the agent reaches only the hosts of an allowlist
  (Harbor's `network_mode = "allowlist"`, enforced by a sidecar per environment on the TLS name): the ChatGPT login's,
  `chatgpt.com` and `auth.openai.com`, which every task names, and the hosts of the run's own model, which Hermit adds
  (for Claude Code, Anthropic's). The verifier reaches nothing.

## Isolation

The verifier's tests are uploaded after the agent ran, so the answer is not in the container while the agent works.
The verifier then runs in that same container (Harbor's shared mode), with its `python3` in isolated mode (`-I -S`: no
site or user site, no `PYTHON*` variables); an agent that replaced the interpreter itself could still change what
scores it. The scorer itself, `tests/`, arrives only then.

Checked with a scripted agent in a prepared task's environment: other hosts, direct IP addresses, plain HTTP and DNS
over TCP returned no data, and UDP is refused; in the verifier's phase not even the allowlisted host answers. Names
still resolve inside the container. The paper's agents had web search; these have none.

## Resources and timeouts

Each task asks for 1 CPU and 8 GB of memory, which Harbor sets as the container's limits; the configs override
neither. The agent has 3600 s (`utils/benchmark.yaml`'s `agent_timeout_sec`, over the tasks' 1800 s; Hermit's session
ends 60 s before it, so 3540 s of work), the verifier the tasks' 300 s. The reference machine (7.5 GB, WSL2) runs 4
environments at once: a session reads parts of the corpus with `rg` and `sed` and stays well under its limit, and 4 at
once left at least 4 GB free there; 16 at once exhausted it. [Run the Harbor
benchmarks](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/README.md#harbor-benchmarks) says what a machine
needs.

## Results

The no-adapter baseline on the 172 test questions, once each, 2026-11-25, with the agent, environment, judge and test
split of `baseline/codex.yaml`. All 172 were the agent's result (no infrastructure error; one session ended at Codex's
output-token limit and scored 0), so `mean_score` is also `python -m hermit.harbor.report`'s pass@1. Claude Code's has
not been run.

| Acting agent | All 172 test questions, 0.0% (the paper's headline, ± SE) | 0.1% | 1% | 5% | The 93 OfficeQA Pro questions of the same run, 0.0% (± SE) |
|---|---|---|---|---|---|
| Codex, GPT-5.6-Luna | 58.1% ± 3.8 | 60.5% | 69.2% | 74.4% | 43.0% ± 5.1 |
| Claude Code, Claude Haiku 5.5 | not run | | | | |

With Codex, a median of 272k tokens per question, 94 minutes for the 172 at 4 at once. The agent tried 40 times to
fetch from other hosts; none returned web data.

The smoke of `baseline/codex.yaml` (its 4 validation questions, Codex), 2026-11-27: 2 of 4 right at 0.0% (of the
other two, one within 0.1%, one within 5%), every rollout the agent's result.

## Differences from the paper

| | The paper | Here |
|---|---|---|
| Data, scorer | OfficeQA and `reward.py` as of 2026-03 | The same (later revisions not taken) |
| Prompt | Appendix E.5 | The same, without the web-search sentence; the corpus path changed |
| Web search | Yes | No |
| Reported on | OfficeQA Pro (133) | All 172 test questions, and the 93 OfficeQA Pro questions within them |
| Agents | Claude Agent SDK, Codex SDK, Gemini CLI | Hermit's Codex or Claude Code agent |
