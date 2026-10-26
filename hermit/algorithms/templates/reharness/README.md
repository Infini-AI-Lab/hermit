# ReHarness template

The harness state of [ReHarness](https://arxiv.org/abs/2610.12668) (Xiao et al., 2026) as an adapter: a bank of
reflections written and revised by the `reharness` Evolver, and a reader that retrieves the top-k reflections for
each instance and hands them to the decision agent.

How an acting pipeline uses it: `Session.context(query=<the instance's input>, task=<the task row>)` returns
`ADAPTER.md` followed by the `[RANKED REFLECTIONS]` block for that instance (the manifest's `context_tool`,
`reflections`, is called by the harness; the agent never sees it as a tool). Record `session.last_context` in the
run's output (`RunOutput.extra["context_tool"]`) so the Evolver knows which reflections the agent read. The built-in
`react` agent and the examples' agents do both.

Retrieval compares the instance's input with each reflection's source input. With `index.json` `retrieval:
embedding` the reader embeds the input through an OpenAI-compatible `/embeddings` endpoint named by the environment
variable `REHARNESS_EMBED_URL` (`REHARNESS_EMBED_API_KEY_ENV` names the key's variable when the reader must send
one); inside a run the adapter's tools run in a sandbox that reaches 127.0.0.1 endpoints only, so point the
variable at a loopback relay in front of the provider (the examples' `launch.sh` starts one). With `retrieval: bm25`
no endpoint is needed.

`hermit deploy` and `hermit convert` render `ADAPTER.md` alone: a host workspace or plugin has no per-task
context, so a deployed copy of this adapter carries the instructions but not the retrieval.
