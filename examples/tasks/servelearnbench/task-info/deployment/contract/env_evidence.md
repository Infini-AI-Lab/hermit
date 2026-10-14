The raw record is mounted read-only at /evidence: per request, the
  complete record — the customer message the host was given, every tool
  call it made with the observation that came back, the write calls it
  issued, its reasoning, what it returned, and the reward — one file per
  request (`e<index>.json`), one directory per block. The prompt carries
  each request's result and the record's address.

Record addresses — used in `derived_from:` lines and briefing citations —
take the form `e<index>` (`e41`, or `e41, e88` for several), the `i` key
in the record. It is unique per request. The `t` field beside it is the
stream position and is NOT unique — several requests can carry one — so
an address built from `t` names a set, not a record.

The record carries no field for the policy in force at the time of a
request.

A tool observation longer than 2,000 characters is cut there in the record; a line at the cut says how many characters were left out.
