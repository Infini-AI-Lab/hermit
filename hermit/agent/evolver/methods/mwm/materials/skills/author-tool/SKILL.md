# author-tool — the shape of a tool that fails soft

Read when decide-executable has answered "tool".

## Layout and sandbox facts

tools/<name>/: TOOL.md + run.py + test.py (+ references/). run.py holds a
literal SCHEMA dict (name, description, parameters as JSON Schema) and
run(**kwargs) returning something JSON-serializable; stdlib-only. The
wrapper executes it in a sandbox: no network, no environment access,
this repository read-only, the call's arguments as its only input. A
host calls it as wm(name=<SCHEMA name>, args={...}) and receives the
return value as that call's result. There is no command line: hosts
never see run.py, only its SCHEMA.

## The SCHEMA is the whole interface

The name, the description and each parameter's description are
everything a host reads when deciding whether to call the tool and what
to pass. Say in the description what the tool computes and what it
returns; in each parameter, what form the value takes. A description a
host cannot act on is a tool nobody calls. Arguments are validated
against the SCHEMA before run() is entered, so a parameter's type and
range belong in the schema, not in prose.

## TOOL.md sections

first line = trigger (the catalog one-liner); scope / last_confirmed_t /
derived_from / repairs header (repairs: the count the agenda's repair
rule keeps, 0 at creation); ## Invocation (the wm call, with an example args
object); ## Verification (how to prove it just worked). Cite the memory
entry the procedure came from with a [[link]] — the prose form is the
fallback and must remain.

## test.py is the admission gate

A plain script that raises on failure; the wrapper runs it in the same
sandbox before serving the tool, and parks the tool if it fails. Check
the SUBSTRATE the tool stands on (its inputs present, the module it
relies on importable, run() answering both ways), not the conclusion it
computes for any particular input.

## Style

Small, single-purpose, one result object — read by a model mid-task,
not by a human. A negative verdict is a value in the result ("valid":
false, with the reason); an exception is reserved for "could not run",
so the caller can tell an answer from a breakage.

These are considerations, not requirements. When your evidence argues
otherwise, follow the evidence and record the deviation in the journal.
