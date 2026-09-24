# Lemma board

Several agents are proving the same theorem at the same time, each in its own
workspace. The only shared channel is the lemma board, reached through the
`board_*` tools. It holds Lean lemmas, not discussion.

- `board_list` shows every lemma on the board with its status: open (nobody is
  proving it), claimed (someone is), submitted (a proof is being compiled),
  done (verified; anyone can use it), rejected (the proof did not compile; the
  message is on the board), blocked (the owner gave up and said why).
- `board_post` puts a lemma statement on the board, by default claimed by you.
  Its reply lists what was already on the board, and names any lemma whose
  conclusion looks like yours; if someone already has your lemma, release
  yours with `board_blocked` and use theirs.
- `board_claim` takes an open, blocked or rejected lemma; one owner at a time.
- `board_done` submits the full declaration with proof. It is compiled on its
  own, against the original file and the lemmas already done, and must not
  depend on any `sorry`. Verification runs in the background; check the
  board later for done or rejected.
- `board_blocked` releases a lemma you own, with one sentence on where it is
  stuck.
- `board_pull` copies a done lemma into your Problem.lean, just before the
  target theorem, so you can use it. A done lemma may use lemmas done before
  it; pull those too if the compiler asks for them.

Your workspace Problem.lean is what gets graded. Whether and how you use the
board is up to you. Each run of `check.sh` ends with one line summarizing what
changed on the board since your last check.
