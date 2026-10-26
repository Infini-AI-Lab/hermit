# Security Policy

## Report a Vulnerability

Use the repository's [Security page](https://github.com/Infini-AI-Lab/hermit/security) and select **Report a
vulnerability** when private reporting is available. Include the affected version, a minimal reproducer, the
execution environment, and the impact you observed.

If private reporting is unavailable, open an issue requesting a private contact channel without including the
vulnerability details, exploit, credentials, or private data. A maintainer can arrange private follow-up.

Report ordinary setup errors and non-security bugs through the regular bug-report form.

## Execution Boundaries

Hermit executes task code, adapter tools, and learning programs. Treat an unfamiliar adapter or integration as
code that needs review before execution. A model endpoint or an agent CLI can also receive the material supplied
by the configured workflow.

Isolation depends on the configured roles, host capabilities, and sandbox enforcement. Linux program isolation
requires bubblewrap and usable namespaces. Disabling the sandbox or choosing a best-effort fallback changes the
protection available to a run. Trusted code that runs in the Trainer process must be reviewed as such.

An adapter passing its specification checks establishes structural validity, not that its code is safe or that
it improves task performance. See [sandbox configuration](docs/framework/configuration.md) and
[adapter specifications](docs/framework/adapter-manifest.md) for the supported boundaries.

## Versions

Reproduce reports against the affected tag or commit and, where possible, the current default branch. The
project does not currently commit to security backports for older versions.
