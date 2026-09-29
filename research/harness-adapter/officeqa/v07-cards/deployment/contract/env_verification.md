Validate checks that ADAPTER.md keeps its five sections and that adapter.json
and EVOLVER.md are unchanged. Every lesson's check list (verify_cell /
verify_answer specs) is replayed by the controller after the session and after
every later block; a lesson is served as verified only while all its checks
pass, otherwise its status line is rewritten to candidate. The controller checks
integrity, provenance and normal completion, never semantic quality or improved
score. No human/Astra review.
