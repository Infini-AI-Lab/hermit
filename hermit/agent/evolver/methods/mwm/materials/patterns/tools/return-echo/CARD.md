name: return-echo
when: state established early in a long interaction no longer influences decisions made late, even though it is still recorded somewhere
uses: tool-output-surface, persistent-files, stateful-tools

## Applicable when

Evidence shows late-interaction decisions that ignore commitments made
early — an open item forgotten, a count misremembered — while the true
state sat in a file nobody re-read. Advice to "check the worksheet"
decays; state that arrives unasked, riding the output of whatever
tool was called anyway, does not.

## Skeleton

A tiny helper every tool return passes through on its way out:

```
render_echo(worksheet) -> <= 6 lines: open items (ids), counts,
                          last action taken
with_echo(body, worksheet) -> body + "\n" + render_echo(worksheet)
```

Three commitments: (1) every tool in the set appends the echo — one
holdout re-creates the blind spot; (2) the echo renders from the
worksheet AT RETURN TIME — freshness by construction; (3) an
unreadable worksheet makes the tool refuse with a CATCH line rather
than echo stale memory. Keep the echo compact — a long echo becomes
the flooding problem it exists to prevent.

## Demo

See `demo/`. Two toy nursery tools (log a sown batch, mark one
benched) share the echo helper over a worksheet file; each return ends
with counts and last action, and the second call's echo reflects the
first call's change. A corrupted worksheet makes the next call refuse
with CATCH. The helper, the every-tool convention, and
refuse-on-unreadable are form; worksheet fields and tool verbs are
content.

## Integration

Ships as a helper module the host's tool wrappers import, or as the
tail of each tool's return template. Pairs with any persistent worksheet the tools keep — the echo is its
re-entry vehicle.

## Instrumentation

Grep transcripts for the echo block: presence per tool return
(coverage) and decisions that contradict the just-echoed state
(misses). High coverage with continuing misses means
the content, not the delivery, needs revising.
