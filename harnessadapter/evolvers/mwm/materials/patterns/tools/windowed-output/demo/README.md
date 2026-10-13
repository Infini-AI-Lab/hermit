# Demo: searching a large germination log through a window

`python3 test.py` — the bundled log has 150 rows, 25 of them from one
seed bank. Querying that seed bank returns page 1 of 3: ten HIT lines
and `MORE|15 more; narrow the query or request page 2`. Page 3 shows the
final five with no MORE line. A query matching all 150 rows exceeds the
hard cap of 60 and is refused with the count and guidance to narrow —
guidance instead of execution. Empty queries and out-of-range pages
are refused with CATCH lines.

Form (keep when re-domaining): the window/cap pair, the PAGE and MORE
line grammar with both escape routes named, cap refusal that reports
the match count, and defined refusals for empty or out-of-range input.

Content (replace): the log row format, the matching rule (substring
here; yours may be fielded), and the WINDOW and HARD_CAP values — size
those to the host's context budget, not to this demo.
