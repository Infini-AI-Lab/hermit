# audit-consumption-path — closing self-check: promise nothing unreachable

Run at the END of every session, after all edits.

The defect class this exists for: describing something the reader cannot
reach. Examples: a briefing pointing at a directory that is not mounted
where the host reads; a text promising a channel that does not exist in
this run; a path that exists on one machine but not inside the container
the reader runs in.

## The checks (all mechanical; run them, do not eyeball)

1. Every path named in consumption/brief.md exists under this repository.
2. Every [[name]] link resolves somewhere: memory/<name>.md, or a
   flat file / directory of that name under one of the type directories
   (memory/, examples/, tools/, agents/).
3. Every type directory has a README.md and its first line is a full sentence.
4. Every address a `derived_from:` line names resolves to a file under
   examples/.
5. Every tool named in the briefing has run.py and test.py; run each test.py —
   a failing admission test means the tool must not be advertised.
6. Anything you wrote this session that names a file — open that file.

If a check fails, fix it or remove the promise. Do not end the session with a
known dangling reference.
