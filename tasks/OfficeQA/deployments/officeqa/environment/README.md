# This environment, as reachable from here

Everything under /environment mirrors what a host can reach of the
environment — nothing more. Facts about the environment itself are in
CONTRACT.md ("This environment") and in /evidence.

Channels in this run:
- documents: the corpus directory {corpus} (697 text files, read-only)
  and its full-text index {corpus.sqlite}, at the same paths a host uses;
  the client officeqa.py (a host has it in its workspace as officeqa.py;
  here it is /environment/officeqa.py):
  python3 officeqa.py --corpus {corpus} --index {corpus.sqlite} search '<query>' [--limit N]
  returns ranked excerpts with file name and line numbers, and
  python3 officeqa.py --corpus {corpus} --index {corpus.sqlite} read <filename> --start-line N --num-lines M
  returns numbered lines. The same files may be read directly. Reads do
  not count toward any host's score.
