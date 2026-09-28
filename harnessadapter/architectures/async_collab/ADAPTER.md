# Team library

Several agents are proving the same theorem at the same time, each in its own
workspace. What the team knows is kept in a library directory, `library/`, next
to your workspace, and reached through four tools. Nothing you write in your
workspace is visible to the others; what you prove is.

Work in cycles:

1. Read `library/updates.md`: newly verified lemmas (each with its `pull`
   command), open subgoals nobody is on, refutations, and the channel.
2. Decide: pull a lemma you can use; take an open subgoal nobody is on (say
   your plan first); or continue with your own line.
3. Write the helper statements you need into Problem.lean, with `sorry` for
   what you have not proved yet, then run `check`. Your open helpers become
   subgoals the team can see; your proved helpers enter the library.
4. Prove; run `check` again. Go back to step 1 whenever a `check` succeeds,
   a compile fails twice in a row, or you are stuck.

The tools:

- `check` compiles a Lean file of your workspace (Problem.lean by default; pass
  `file` for a scratch file) and, for Problem.lean, prints the axioms the
  target depends on. Use it for every compile instead of a shell command.
  Every helper lemma of yours that compiles without `sorry` is added to the
  library automatically; every helper that still has a `sorry` is listed as an
  open subgoal.
- `pull <name>` copies a verified library lemma into your Problem.lean, just
  before the target, together with the library lemmas it uses.
- `refute <name> <evidence>` records that an open subgoal or a claim is false,
  with the counterexample or computation; the team stops working on it.
- `say <type> <text>` posts to the channel: `plan` (what you are doing next),
  `idea` (a route worth trying), `feedback` (on a lemma, subgoal or message —
  give its name or id as `ref`), `request` (what you need from others).
  Messages are short; point to lemmas or files instead of pasting code.

Every tool reply ends with a `[team]` block: what changed since your last call.
The full state is in `library/index.md` (verified lemmas, open subgoals, who is
on what, channel), `library/verified/*.lean` and `library/channel.jsonl`.

Your workspace Problem.lean is what gets graded.
