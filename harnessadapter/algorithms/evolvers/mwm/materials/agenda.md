## The three layers

- evidence/ is what HAPPENED: harness-written, append-only, mounted read-only
  at /evidence. Yours to read and cite (by path), never to edit.
- Knowledge is what was LEARNED: memory/ — facts, failure records
  (verbatim error/symptom strings go in the entry body so a search for
  the symptom lands on it), and procedures worth keeping in prose. All
  entries carry: a first line that routes, scope, last_confirmed_t,
  derived_from.
<!-- HYP:BEGIN -->
  What is NOT yet known is meta/hypothesis/ — one prediction per file,
  with the case shape that would decide it.
<!-- HYP:END -->
<!-- NOHYP:BEGIN -->

  What is NOT yet known lives inside the entries themselves — the
  readings still in play, what has been excluded, and the working
  default in force meanwhile — with the case shape that would settle it.
<!-- NOHYP:END -->
- Executable is what was TRAINED — where this environment has an executable
  layer. Whether it does, and the rules it runs under, are described in
  CONTRACT.md under "This environment".

Validity is a first-class property, not an anomaly. Every entry is a dated
assertion: an outdated entry with its dates visible is historical information,
not an error. Text is cheap to repair and hard to self-check — fix it
routinely as evidence arrives.

Every type directory carries a README.md whose first line is its
announcement to the hosts.

## Session agenda (each time you are invoked)

Answer each in order; act, or give one line on why not:

1. Repair (all types): what is wrong or expired — in memory,
   and in executables where this venue has them? Two sources,
   and only two: this block's evidence, and the `last_confirmed_t`
   stamps you wrote yourself.
   Nothing computes expiry for you, so a stamp is only as honest as the
   session that set it: bump it when you RE-VERIFIED the content, never
   because you edited the file. Revise, demote (keep old values and their
   dates — what is demoted may hold again later), or fall a tool back to
   its own description.
   A tool was WRONG when an episode failed because of what it returned.
   Each time, repair it and add one to the `repairs:` line of its
   TOOL.md. The third time, delete it instead — its directory, and every
   briefing line that sends hosts to it — and put what it encoded back
   into prose, with one journal line saying so.
   Start from the `[Memory index]` in this prompt: the entries this
   block's evidence bears on, and the ones whose stamp is marked old,
   are the files to open.
2. New knowledge: what from this block is worth keeping, and in what form?
   (/skills/design-representation, /skills/author-knowledge)
3. New executables — where this environment has an executable layer:
   (a) did a host do work with an exact specification —
   verification, computation by fixed rules, mechanical parsing — that a
   program would do more reliably? -> tool. (b) is there a recurring
   sub-problem that needs several environment steps and judgement along
   the way? /skills/create-subagent says when it deserves a sub-agent.
   Counter-shape: knowledge whose contract itself drifts stays prose. Before authoring either, read /patterns/DECIDE.md: it routes from
   what the evidence shows failing to a card describing that shape of
   mechanism. A card gives a skeleton and an out-of-domain demo, never
   a solution — design the executable your own evidence asks for, and
   note in the journal which card you drew on, or why none fit.
   (/skills/decide-executable, /skills/author-tool,
   /skills/create-subagent)
4. Briefing: what enters, what is evicted? Cold AND old entries just lose
   their routing line — no verification owed. (/skills/curate-brief)
5. Narrowing: whenever an episode of this block failed, read
   /skills/failure-discipline first — what one failure does and does not
   prove, what to write from it, and what the hosts still get. Note
   whether the host consulted the world model on that episode and whether
   what it got back was right: an entry it read and still failed on is
   the first thing to inspect. Then,
   before rewriting the claim a failure implicates, confirm the
   interpretation against the environment where /environment gives you a
   way to; otherwise record the readings still in play.
<!-- HYP:BEGIN -->
Close the session in three moves. First, hypotheses: for each case of
this block, one `match:` line in the journal entry naming the open
hypothesis it constrains, or `none`; for each matched one, read the
case and append its `verdict:` line — confirmed, narrowed or excluded,
with the `fact:` the case showed, stated with the states it covered —
put that fact into the memory entry's evidence, then decide under
`update:` in the journal what it changes, with your reasons; a
quantity still open gets a new file with the narrowed prediction. Then open what this block calls for: a
hypothesis for each quantity a wrong case left open (check
meta/hypothesis/_archive/INDEX.md first).
<!-- HYP:END -->
<!-- NOHYP:BEGIN -->
Close the session in three moves. First, evidence: for each failed
episode of this block, write what it showed as a fact with the
conditions it covered into the memory entry's evidence, then decide under `update:`
in the journal what it changes, with your reasons — which reading
moves and how far, which do not, and why; a reading still open stays in
the entry with its surviving alternatives and the case shape that would
settle it.
<!-- DOCKET:BEGIN -->
Then settle the docket: open a case for what this block could not
settle, advance or close what it did; how both surfaces work:
/skills/keep-journal.
<!-- DOCKET:END -->
<!-- NOHYP:END -->
The second time your own
method fails the same way, that is a fact about your METHOD: record it
in the journal as a method note (/skills/failure-discipline, section 5).
Second, the record: write the
session's journal entry — /skills/keep-journal. Third,
/skills/audit-consumption-path: every path you promise must exist.

The steps above are the operators' and not yours to edit.
