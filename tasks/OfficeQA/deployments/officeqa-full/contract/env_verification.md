Validate native structure and run real tests for executable capabilities.
Every lesson's check list (verify_cell / verify_answer specs) is replayed by the
controller after the session and after every later block; a lesson is served
only while all its checks pass, otherwise it is demoted to candidate. The
controller checks integrity, provenance and normal completion, never semantic
quality or improved score. Native advice is recorded. No human/Astra review.
