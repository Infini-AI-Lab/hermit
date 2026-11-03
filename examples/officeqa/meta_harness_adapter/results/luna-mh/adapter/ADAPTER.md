# OfficeQA solving protocol

Solve the question from the supplied Treasury Bulletin files and return exactly one `<answer>...</answer>` wrapper.
Inside it put only the requested value: no explanation, labels, citations, markdown, extra units, or alternative
answer. Preserve every format explicitly requested (brackets, separators, signs, currency/percent symbols, date
wording, order, spaces, and trailing zeroes).

## Locate and freeze the evidence

Break the question into the output and its required operands. For each operand, search for the distinctive table or
chart title and exact row/series label, then read the complete header and footnotes. Privately record `(source file,
publication vintage, table/chart, row/series, column or period, raw value, unit/scale, calendar or fiscal basis,
average or end-period basis, actual or estimate)`. Do not use a nearby match until all applicable fields agree. Use
the requested publication vintage when several files contain revisions, and check continuation pages. Enumerate
every inclusive period in a range; for a plot, use the legend, axis, and full relevant sequence rather than visual
proximity.

## Compute and check

Write the exact formula and index set before substituting the frozen values. Apply the stated convention literally:
population versus sample deviation, log base, trimming, geometric mean, regression index, interpolation, smoothing
initialization and weights, exchange-rate direction, percentage versus percentage points, and signed versus absolute
difference. Use a calculator or short local script for multi-value arithmetic, retain precision until final rounding,
and independently recheck the operands, count, denominator, units, sign, scale, extrema, and list order. Then emit
only the requested answer in its exact type and syntax.
