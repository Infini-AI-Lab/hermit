name: windowed-output
when: one broad read or query returns so much that it evicts the very working state the actor was holding
uses: large-text-input, tool-output-surface

## Applicable when

Evidence shows a single tool return dominating the context — a full-file
read, an unanchored search — after which the actor forgets earlier
commitments or re-asks settled questions. The volume was legal but
ruinous. The fix makes the TOOL ration its own output; asking the actor
to request less does not survive long interactions.

## Skeleton

```
search(corpus, query, page=1) -> lines
  query empty/unparseable -> CATCH: refuse, say what a query needs
  matches = scan(corpus)
  len(matches) > HARD_CAP -> CATCH: report the count, refuse to dump,
      tell the actor to narrow      (guidance instead of execution)
  page out of range -> CATCH with the valid range
  emit: PAGE|p/total|showing a-b of N
        one HIT line per match (<= WINDOW)
        MORE|k more; narrow the query or request page p+1
```

Two dials: WINDOW is what fits comfortably per return; HARD_CAP is
where even paging stops making sense — the query itself is the
problem. The MORE line always names both escape routes, so truncation
never strands the actor.

## Demo

See `demo/`. Searching a 150-row germination log: a seed-bank query
gets page 1 of 3 with a MORE line, page 3 closes without one, a query
matching the whole log is refused at the cap with guidance, and empty
or out-of-range requests are refused. The window/cap/MORE mechanics
and refusal wording are form; the row format and both dial values are
content — size them to the host's context budget.

## Integration

Wraps whatever read or search tool the evidence shows flooding.
Strongest when the unwindowed original is removed, so the rationed
path is the only path.

## Instrumentation

Track result sizes (should cluster under WINDOW), cap refusals
followed by a narrower query, and page-2+ requests. Many cap refusals
with no narrowed retry mean the guidance text is not actionable —
rewrite it before resizing dials.
