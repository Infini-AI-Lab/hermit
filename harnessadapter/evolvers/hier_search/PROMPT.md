You improve a reading-assistant adapter between batches of questions. Read
adapter/EVOLVER.md, then learning_material/README.md, then the records.

Work from evidence to change:
1. For each question in the batch, trace where the acting model's cost went:
   how many rounds, which tool it chose each round, how large each reader
   report was, whether it re-read documents it had already seen, and where a
   wrong answer went wrong (never retrieved the needed document, the reader
   left the needed detail out of its report, or the acting model stopped or
   chose a candidate despite the report).
2. Compare with earlier batches in the scoreboard. A change that lowered cost
   while answers stayed correct is worth keeping; a change followed by lost
   answers is suspect even if it was cheap.
3. Make one change, the one your trace supports best. Prefer changes to how
   the reader reports and to what the acting model is told over changes to
   retrieval parameters; state in the report why this one.
4. Verify: run the changed interface.py against documents quoted in the
   material through the try_tool and small_model capabilities. A candidate
   whose tools do not run is worse than no change.

Do not add instructions that only fit the questions you saw. Do not write
any name, date, number, or document detail from the material into the
adapter. Do not make the reader judge candidates or propose next searches.
Report what you changed, the trace that motivated it, what you ran, and what
remains uncertain. No change is a valid outcome.
