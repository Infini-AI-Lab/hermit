# How the acting agent runs

One GLM 5.3 Flash agent per question, in a tool-calling loop with two tools: `search` (BM25 full-text search over the
697 bulletin files, optional filename glob) and `read` (lines of one file). At most 24 model turns and about 65k
output tokens per question. The adapter's context (ADAPTER.md) is appended to the agent's system prompt.
