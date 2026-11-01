# Choosing a card

One page. Start from what the evidence shows failing, not from what looks
interesting to build.

**This library covers EXECUTABLES only** — programs and delegates. It is
deliberately silent on how to write knowledge. A failure of fact, wording,
scope or timing is answered in your own prose, from your own evidence, and
no card here is the right shape for it. Routing such a failure into a tool
builds a mechanism the failure never asked for.

## Route by failure shape

| The evidence shows... | Look at |
|---|---|
| The actor computed or classified wrongly in its head, where a running quantity or a written rule set exists | `tools/` — raw-input-classifier, stateful-batch-planner |
| State set early in a long interaction stopped influencing later decisions | `tools/` — return-echo |
| Irreversible actions ran with prerequisites unmet | `tools/` — preflight-checklist |
| A single read or output flooded the working context | `tools/` — windowed-output |
| Judgment degraded late in long interactions | `subagents/` — verifier-delegate |
| Delegated work came back confidently wrong | `subagents/` — attached-delegation |
| The actor lacked a fact, held it in the wrong form, or acted at the wrong moment | **no card — write the knowledge yourself** |

Families mirror the executable half of your own repository: `tools/` cards
produce tested programs, `subagents/` cards produce SPECs and delegation
habits. Your knowledge layer has no counterpart here by design.

## Strength ladder

Mechanisms differ in how much they constrain the actor: a fact in text <
a tool the actor may call < a tool the briefing routes to for a named
situation. Nothing here can interpose itself before the actor's own
actions or refuse to let it proceed — the actor calls what it chooses.
Stronger is not better — it is more expensive to build and to be wrong
with. Match strength to how costly the failure is and how certain you
are of the rule.

## Discipline

- A card is a FORM. Its demo's domain content is deliberately foreign;
  yours comes from evidence.
- Nothing here is mandatory, and the catalog is not exhaustive — a form
  you invent is as admissible as any card, and worth proposing in your
  journal.
- Checking a failure against this page costs one minute; building the
  wrong mechanism costs blocks. Route first.
