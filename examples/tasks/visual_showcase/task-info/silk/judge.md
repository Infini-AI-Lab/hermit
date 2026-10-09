# How a run is observed

After the agent's run ends, the host runs the submitted `index.html` once more with the same browser capturer that
`inspect_page` uses (headless Chrome, offline; only `index.html` and the two Three.js files are served). This final
capture is never shown to the agent. It writes the run's `capture/` directory:

- `desktop-t0.png`, `desktop-t2.png`, `desktop-t5.png`: 1280×800 screenshots about 0, 2 and 5 seconds after the page
  reports ready, at the initial intensity 0.4;
- `desktop-interaction.png`: about two seconds after pause, reset and setting the intensity to 0.75;
- `phone.png`: the 390×844 viewport after the same interaction;
- `diagnostic-paused.png`: the scene after the pause interval, kept for diagnosis;
- `summary.json`: page and console errors, whether `getState().ready` became true, the fraction of scene pixels that
  changed between the timed screenshots, the pause, reset, intensity and keyboard checks, control positions in both
  viewports, frame timing and the `getState` values reported at each screenshot.

The run directory also holds `request.json` (the system prompt and tools the agent got), `calls/NN.json` (one record
per model request, without private reasoning), `tools/NN.json` (one record per tool call), `checks/` (the agent's own
inspections, as it saw them), `workspace/`, `submission/index.html` and `result.json` (status, stop reason, requests,
tool calls, inspections and token usage).

Frame differences show visible change, not physical correctness or beauty; `getState` values are the page's own
reports.
