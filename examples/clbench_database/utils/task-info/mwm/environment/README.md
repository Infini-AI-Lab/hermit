# This environment, as reachable from here

Everything under /environment mirrors what a host can reach of the environment — nothing more. Facts about the environment itself are in CONTRACT.md ("This environment") and in /evidence.

Channels in this run:
- query: /environment/bin/task-env query '<one SELECT statement>' — read-only, results capped at 50 rows, against the current instance database; does not count toward any host's score.
