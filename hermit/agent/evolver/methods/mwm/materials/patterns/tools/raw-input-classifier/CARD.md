name: raw-input-classifier
when: the actor pre-digests raw records before judging them, so classification errors enter upstream of any check
uses: large-text-input, tool-output-surface

## Applicable when

Evidence shows the actor reading raw records, applying written rules in
its head, and acting on its own summary — with errors born at the
summarize step: a field misread, a threshold half-remembered, a row
silently skipped. The fix moves classification into code that consumes
records VERBATIM; the actor's job shrinks to fetch-and-paste.

## Skeleton

```
classify(raw_text, params) -> verdict lines
  for each line of raw_text:
    parse against a STRICT grammar (regex or schema)
    no parse -> "CATCH: line N is not a verbatim record; paste it
      unaltered"  (a paraphrase is refused, never guessed at)
    parse    -> apply params -> "VERDICT|<id>|<label>|<reason>"
  params unparseable -> refuse everything, nonzero result
```

Two design rules: (1) the grammar is strict enough that a retyped or
paraphrased row cannot pass as data; (2) every verdict carries its
reason, so a wrong parameter shows up in the output, not in silence.

## Demo

See `demo/`. A pasted germination-log excerpt is classified against
bench-zone capacity and condition parameters: clean accepts, one row
over its zone cap, one wilted row held pending a pest-report tag — and
one retyped row the grammar refuses with a CATCH line. A broken
parameters file makes the whole run refuse. The strict-parse-then-classify loop,
the refusal convention, and reasons-on-every-verdict are form; the row
grammar, labels, and parameter names are content.

## Integration

A tool file with test.py. The briefing routes: "never classify records
yourself; paste them into the tool raw." The tool's refusal of
paraphrases is what makes that routing stick.

## Instrumentation

Count calls (adoption), CATCH refusals (paraphrase attempts caught),
and verdicts the host overrides afterwards. Frequent overrides mean the
parameters are wrong; zero calls means the routing line is not landing.
