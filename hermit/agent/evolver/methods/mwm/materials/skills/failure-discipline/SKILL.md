# failure-discipline — what a failure does and does not prove

Method only; every rule below is domain-free. Your evidence decides the
content.

## 1. A failure only derives bounds — enumerate what it leaves open

A failed episode refutes exactly the ACTION SUBMITTED in exactly the
STATE OBSERVED, and nothing wider. If the environment grades pass/fail,
read "zero" below literally; if the outcome is a quantity, read it as
"worse than the reading predicted"; if outcomes carry randomness, one
episode bounds nothing — only counts over many do (§7). Before writing
anything from it:

1. List every reading the failure is consistent with — not only the one
   that fits the fewest words. Illustration: an episode tried reading X and
   did not get the outcome the reading predicted. That is consistent with "X is wrong here", with "X was
   right but another condition of this episode failed it" (a step it
   skipped, a requirement it did not meet), and with "the reading was
   right and the execution slipped". Several such outcomes still distinguish
   nothing if the other condition was present every time.
2. Write down the bound the failure actually established — which reading
   is excluded, in which state — and the readings that survive, as a
   candidate set in the entry. Writing "X is never right" from a handful of
   zeros is a collapse the evidence never forced.
3. Name the observation that would separate the survivors: what a future
   episode would have to look like, and what each outcome would mean.
   That is what turns an open question into one that evidence can close.
4. Choosing which survivor to serve the hosts is a separate judgement
   about SUPPORT — one has an episode behind it where it worked, one is
   what the environment's own documents say (where it has any), one is
   merely not yet excluded. Write that judgement down next to the fact, so the zero
   does not look as if it made the decision. Two collapses are easy: a few
   bad outcomes on one reading become "that reading is never right"; and when
   something must be served, an unsupported third reading is invented
   because the excluded one left a gap.

## 2. What an episode showed is first a fact, then a decision

Write what the episode showed as a fact about the environment, together
with the conditions under which it was observed, and put it in the
entry's evidence. Then, separately, decide what it changes: which reading
moves and how far, which readings it does not touch, and why. Keeping
the two apart is what lets a later session see whether the decision was
actually supported.

Two judgements live in that second step, and both deserve their evidence
stated.

**Width — how far does this episode's lesson generalise?** An episode
carries several features at once, and the record does not say which of
them decided the outcome. Narrowing a reading to one feature, and
extending it to feature values not yet seen, are both claims.
Illustration: a rule that holds for one kind of input fails on an input
that differs in two ways at once; reading the failure as "the rule breaks
on feature A" and as "the rule breaks on feature B" are different claims,
and splitting the rule along whichever feature was noticed first pays for
the same miss twice when the other feature was the cause. State the width
you chose and why.

**Time — do earlier episodes still count?** They remain evidence. That
the environment's behaviour changed is a claim of its own, strongest when
the same kind of episode was correct earlier and wrong later; cite both
with a `drift: <earlier address> -> <later address>` line on the entry.
The opposite collapse: explaining every excluded reading with "it
changed" discards the earliest evidence and rewrites the same rule
session after session.

## 3. A complex episode is several facts — take it apart before you write

One episode can rest on several pieces of knowledge at once, and a single
verdict does not say which of them was missing or wrong. Do not record
such an episode as one lesson. Work through it piece by piece: list every
fact the correct outcome depended on, mark each one as confirmed by this
episode, contradicted by it, or untouched, and write each down separately
in the entry it belongs to — one entry per fact, not one entry per
episode. Then look at the combination: which entries have to be read
together for an episode like this, and does the briefing route a host to
all of them, or only to the first one it would hit? A host that finds one
fact and stops is a routing failure, not a knowledge failure; fix it by
naming the entries that go together, not by adding another rule.

## 4. A deployed rule silences its own counter-evidence

Once a rule reaches the hosts — in the briefing or compiled into a tool
— they act on it, so the actions the rule forbids stop being tried, and
the evidence that could refute it stops being produced. Silence after
deployment is NOT confirmation. For any rule that forbids an action,
ask: when did the record last contain a genuine TRY of the forbidden
action? If the answer is "not since deployment", the rule is unverified
from that point on, whatever its hit rate looks like.

## 5. The second same-class failure of your own method is a method note

Once is a data point; twice or more is a pattern about YOUR OWN method
— not about the environment, and not about the hosts. When your method
has failed the same way two or more times, record it in the journal as
a method note — the pattern, when it applies, what to do instead — so
your next session reads it. The steps of the agenda are the operators'
and do not change; the note is yours.

What counts depends on what the environment returns:
- **Deterministic outcomes** (a fixed answer, a verdict): a claim that
  was wrong twice in the same way — too wide twice, rewritten from one
  outcome twice, scoped to the wrong feature twice — is a method failure.
- **Random outcomes** (a result that varies for the same action): one or
  two outcomes settle nothing by themselves — and they are not to be
  dismissed either. The method is to COUNT: over every episode the claim
  applied to, how did they go, against what the claim predicted? Two
  failures of method look alike here: rewriting a claim from two
  outcomes without that count, and leaving a claim untouched while the
  count says it is failing. Either one, two or more times, is the
  pattern to act on; the method note you write states how many
  episodes the count must cover before a claim moves, and what movement
  the count then requires.

## 6. Uncertainty is yours to manage — hosts still get a working default

An entry may honestly say that a reading is unresolved; the briefing may
not stop there. Where the evidence favours one reading, every rule a host
must act on ships a WORKING DEFAULT — that reading — labelled as working,
while the entry carries what is still open and the surviving alternatives.
Exporting "unresolved" to the hosts converts your uncertainty into their
coin flip: a briefing that leaves a choice open makes hosts guess between
the alternatives and lose a share of the guesses, on episodes where the
evidence already favoured one. You hold the evidence; they do not. Where
the evidence favours none, the briefing names the readings still in play
and what each would mean — information, not a choice pushed onto the
host (§1.4). Honesty about uncertainty lives in the entry; the serving
surface always answers "so what do I do TODAY?".

## 7. A lost episode has more than one possible cause

Before rewriting the entry a failure implicates, separate three shapes:
the entry was wrong; the entry was right and the environment has moved
since it was confirmed; the host had the right entry and mis-executed
it. Only the first two are facts about the environment, and only they
license an edit. One outcome is not a finding: count the episodes the
entry applied to and how they went. Where the environment returns no
verdict, re-derive the answer by an independent path; two independent
derivations that agree are the verification.
