# Execution environment and evidence

The adapter candidate is readable/writable. The evidence store is always readable and read-only; its README defines its format. Method materials and history are also read-only. Only adapter and isolated scratch accept writes.

Additional environment read scope: {"paths": []}
Default command working directory: {working_directory}
Agent tool/command network: disabled. Model API transport remains available.

Use get_policy for absolute roots; read_path/list_path for authorized environment files; run_command for sandboxed exploration; fetch_url for HTTP(S) when enabled. No benchmark-specific environment or evaluator is implicitly provided. Environment directories stay read-only even when used as a command working directory. MWM executable admission still uses its stricter test_python sandbox; ordinary run_command success is not admission.
