# Semantic check for derived answers

Use this procedure after locating the evidence and before doing arithmetic whenever the request asks for a
statistic, comparison, interval, extremum, forecast, or any other derived quantity.

1. Write an interpretation card in the reasoning, using the question's wording rather than the easiest available
   table: `target = ...; eligible observations = ...; endpoints/inclusion = ...; operation = ...; convention = ...;
   output scale/rounding = ...`.
2. Distinguish a value at an endpoint from a change over an interval. A request for a value, level, balance, rate,
   or endpoint uses that observation; a request for change, difference, growth, or spread specifies the two endpoints
   and the direction. Do not add or sum observations unless the wording asks for aggregation.
3. For a named statistic, map the noun and qualifiers exactly. In particular, “standard deviation” still requires
   deciding population versus sample from the stated set and source context; “trimmed” requires an explicit
   per-side removal rule; “volatility” requires the stated return window and denominator; and “percent” is not
   automatically a percentage-point difference. If two conventions remain plausible, calculate both once and use
   only the one supported by the wording or definition in the source; never choose because it is numerically nicer.
4. For minimum, maximum, earliest/latest, peak, or closest requests, enumerate every eligible candidate and record
   the comparison before selecting one. A plausible candidate or a nearby table is not evidence that the set is
   complete.
5. Re-read the interpretation card against the question and source headers. Only then calculate from the already
   source-locked ledger, preserving precision and applying the requested final format.

For a multi-observation request, or whenever a table repeats year/period headers or has several plausible rows,
perform one independent extraction pass before calculating: start from the requested row label and each requested
period/header rather than reusing the assembled number list, then compare the resulting period-to-value pairs with
the ledger. Resolve every mismatch from the visible headers and row context; a count/uniqueness check does not prove
that the values belong to the requested series. This pass is bounded to once and is unnecessary for a single,
unambiguous observation.

This is one bounded pass: do not keep searching after the card, evidence check, and calculation agree. If the source
does not expose a chart's plotted values, do not replace the chart with a neighboring table; continue only with a
directly equivalent representation that actually contains the requested series.
