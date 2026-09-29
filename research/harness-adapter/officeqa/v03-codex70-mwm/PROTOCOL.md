# OfficeQA Pro: GLM + native Codex versus GLM + native Codex + evolving MWM

This is the fresh v3 experiment. Older answers, visual adapters, Codex-learned
knowledge, and earlier OfficeQA scores are not model or learner inputs. The v1
attempt was stopped without grading after provider rate refusals and a learner
request-budget exhaustion. Preserve v1 records separately; no v1 predictions,
feedback or candidate knowledge enter this run. V2 was stopped without grading
after two ended questions per arm: a Responses output-format configuration field
was incorrectly classified as generated text, preventing rate-refusal retries.
V3 only corrects that transport-envelope classification; v2 budgets and all task
and learning instructions are unchanged. The same fixed 70 IDs are restarted
from generic W0 under the following uniformly changed infrastructure budgets.

## Fixed data and task order

- Original OfficeQA Pro, dataset revision `763a8366abf2a3605c381d53586d844dc60fa756`.
- 70 of 133 questions, sorted by SHA256(seed + NUL + uid), with seed
  `metaworld-officeqa-pro-codex70-online-v1`. Selection does not consult answers,
  difficulty, source mappings, or old results. Both arms have the same ordered IDs.
- Both arms can read all 697 officially downloaded parsed TXT documents and the
  same deterministic FTS5 index. No answer-side document selection.
- This is a parsed-text, offline subset experiment, not the official full PDF
  plus internet leaderboard setting. Questions requiring external information
  are retained in the denominator.

## Arms and inference

- Baseline: `accounts/fireworks/models/glm-5p3-flash` + actual native Codex CLI.
- Adapted: same model, actual native Codex CLI, tools, corpus and per-question
  budgets, plus native MetaHarnessAdapter MWM context and read-only memory tools.
- Start W0 from the repository's generic MWM architecture, not previously learned W.
- Both use Fireworks's native Responses API. A local relay records public request
  and response content, adds the secret in controller memory, and enforces budgets.
  It is not a custom agent loop. Codex selects/runs tools and manages its context.
- Fresh Codex conversation and filesystem workspace for every question. No saved
  earlier task context in baseline; adapted receives only its frozen adapter.
- Actor budgets: 24 logical API requests, 65,536 output tokens in aggregate,
  32,768 maximum output tokens per response, 1,800 seconds including queue and
  rate-limit waits; temperature 0; Codex reasoning high.
  Both arms have identical settings. Provider reasoning allocation is not claimed
  to be an exactly measured hard cap. No aesthetic or correctness-based retries.
- Each arm works in batches of five, at most one active actor per arm.
  Both arms can run concurrently. Every adapted batch uses one frozen W snapshot.
  Hardware and API contention may affect latency; quality/usage are reported
  separately and latency is not treated as a controlled speed comparison.
- The Responses transport and Chat learner share one scheduler: at most two
  in-flight requests, at least one second between dispatches. An explicit rate
  refusal before generation can retry the identical payload, at most five wire
  attempts. Shared backoff is 15/30/60/120 seconds, honoring longer Retry-After
  within the request deadline. Every attempt and wait is recorded. Partial
  generation, unknown network failures, model answers and grading outcomes never
  trigger retries. Missing usage on refused requests is labeled unknown, not zero.

## Isolation

- Enforced OS file policy: own scratch workspace writable; corpus, tool runtime,
  instructions and active adapter read-only; no access to original CSV, keys,
  source mappings, full question list, sibling tasks, other arm, historical reports,
  or private grader data. Symlink and parent traversal probes must fail.
- One externally enforced OS sandbox covers the whole Codex client and all its
  child shell/MCP processes. It permits only that task's local inference relay
  port; the relay can reach only the fixed Fireworks inference URL. All other
  local ports, external IPs and Internet destinations are denied. The learner's
  separate file-tool sandbox has no network.
  The relay rejects web search, remote/server-executed MCP/SSE tools, remote image
  or file inputs, model changes, redirects and cross-task provider response IDs.
- No user/global Codex settings, browser, plugins, connectors, external memories,
  subagents or GPT/Astra model fallback. Approval escalation is disabled for actors.
- macOS cannot apply a second Seatbelt sandbox inside an already sandboxed process.
  Consequently native Codex's internal sandbox is disabled only inside the external
  enforced sandbox. This is not an unsandboxed run. Actual OS denial probes must
  pass before launch. Controller records stay outside the actor's readable roots.
- Isolation is a runtime boundary, not a claim that model pretraining never saw
  public benchmark material. Each request/response/model and tool trace is retained.

## GLM-only learning and feedback

- `learn_every = 5`: W0 for Q1–5, learn W1, W1 for Q6–10, etc. W13 is used for
  Q66–70. W14 is saved after the final batch but cannot affect these 70 scores.
- One GLM learner per completed adapted batch. Evidence is only those five
  adapted public trajectories, actual tool observations and original submissions.
  No baseline trajectories, future questions, gold answers, gold source mappings,
  grader scores or grader explanations are exposed to the learner.
- GLM generates its own feedback and updates reusable Markdown knowledge using
  the native MWM evolution file interface. This is adapter knowledge learning;
  model weights do not change. Self-assessments may be wrong without gold labels.
- Learner budgets: 1,800 seconds including waits, 64 logical requests, 120 tools, 65,536 completion tokens,
  32,768 per response, requested numeric reasoning effort 4096 on Chat Completions.
- Only knowledge Markdown is mutable. No new executable tool code. Native
  validation and provenance checks determine mechanical admission. No human or
  Astra semantic review selects the better candidate or writes corrective advice.
  If a learning session fails admission, retain the prior W and record the failure.
- Astra writes experimental software and fixed task/interface/learning rules;
  those inputs are published. Astra supplies no task-specific critique or manual
  memory edits based on answers. This is not described as zero Astra involvement.

## Submission, scoring, and reporting

- Final response must be one direct answer, one nonempty line, at most 250
  characters. Retain tool working/citations in the trajectory. Do not manually
  extract or repair an answer from a failed run's intermediate reasoning.
- Every question is attempted once per arm. Failures remain in the fixed 70
  denominator; record exact transport/runtime status. Do not substitute easier IDs.
- Seal all 140 original predictions before running the official pinned reward.py
  outside actor/learner sandboxes. Primary tolerance 0; secondary numeric relative
  tolerance 0.01, explicitly separate. This is deterministic scoring, no LLM judge.
- Report paired accuracy, by-block outcomes, disagreements, infrastructure
  failures, observed token usage, learning cost separately, W versions, and raw
  GLM feedback/provenance. Learned arm gets extra learning compute; expose it.
- This is sequential online adaptation, not a frozen held-out-model assessment.

References: https://huggingface.co/datasets/databricks/officeqa ;
https://github.com/databricks/officeqa/tree/7b9a3c154ef9fb40215bb67934afc43e6799de16 ;
https://docs.fireworks.ai/ecosystem/fireconnect/codex ;
https://docs.fireworks.ai/guides/response-api ;
https://learn.chatgpt.com/docs/permissions .
