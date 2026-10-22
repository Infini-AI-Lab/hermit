# Demo: propagation requests against per-seed-bank bench allowances

`python3 test.py` — six propagation requests arrive in submission order
with some allowance already consumed. Items 1-4 fit and get ACCEPT lines
with running `used=n/limit` counts plus the exact `book_bench(...)`
arguments to copy; item 5 names a seed bank with no allowance on file,
and item 6 hits an exhausted allowance — the earlier accepted requests
already consumed the last bench — both refused with CATCH lines. A
second batch with one prose row is refused whole before any verdict.

Form (keep when re-domaining): the ordered fold over consumable state,
all-or-nothing batch parsing, CATCH refusals with the running count,
and copy-ready write arguments on every ACCEPT.

Content (replace): the capacity keys and limits, the item fields, and
the name/shape of the write call — take them from your domain's
evidence, not from this demo.
