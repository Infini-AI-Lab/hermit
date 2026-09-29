# S4-core review: requests to other owners

None of these block S4. The review changed only S4-owned files.

1. **S3 (controller_runtime.py), `check_final` input.**
   - `on_final` passes the whole final assistant message to `Gate.check_final`. Every number in the prose becomes part
     of the candidate: `count_mismatch` fires on "In FY2004 the total was 12.3, up from 11.1", and `not_from_compute`
     checks the context numbers too.
   - Either pass only the answer span (for example the last line, or the pack's answer extraction), or confirm that
     re-query feedback on prose is intended. Pilot metric: the enforce fire rate on right episodes.
2. **S3, controller logs.** `SandboxRunner.last['stderr_tail']` is the tail of model-authored code's stderr. Treat it as
   model text if it is logged; it is safe to store now that it can no longer be redirected to other files.
3. **S6, pytest config.** Register the `slow` marker (repeats S4-core-requests item 14).
4. **Contracts owner, documentation only.**
   - `Compute.values` excludes lines that carry a Codex truncation marker.
   - `Compute.inputs` omits literals on code lines that carry one.
   - `ungrounded_inputs` treats numbers stated in the question as grounded. This covers the number itself, its percent
     as a fraction, and 1 ± p/100 for |p| < 100.
