# Visual showcase: Wind-driven silk

The acting agent, GLM 5.3 Flash, writes one offline, interactive, animated web page, `index.html`, for the brief
below. This is the only case this adapter is trained on.

## How the agent runs

The agent works in a tool loop: `write_file` (the whole `index.html`), `read_file` (`index.html` or the brief as
`README.md`), `replace_text` (one exact replacement) and `inspect_page` (runs the current page in an offline Chrome and
returns its observations and five screenshots). With a learned adapter, the adapter's knowledge package is appended to
the system prompt and the read-only `wm` tool (`list`, `memory`, `recall`, `search`, `read`, `trace`) is offered too.
Limits per run: 600 seconds, 12 model requests, 24 tool calls, 3 inspections and 65,536 completion tokens. Only the
last `index.html` is submitted. `judge.md` says how a run is observed and recorded.

## The brief (the agent's user message, verbatim)

Wind-driven silk

Create an animated piece of silk moving in wind, with a clearly visible support or anchored edge and a free region that billows and ripples. Make the fabric feel light and flexible: folds should travel or change over time, and shading or highlights should help the viewer understand the changing surface. The supported region should remain plausibly attached while the free part moves. The intensity control represents wind strength: increasing it should visibly increase the fabric's response without the sheet exploding, collapsing into a point or leaving the composition.

Choose the fabric's color, weave impression, environment and presentation. A banner, hanging drape or sculptural suspended sheet is acceptable; no particular cloth solver, mesh density or camera interaction is required. Prioritize a coherent surface and appealing motion rather than explanatory overlays. A nonphysical deformation can be appropriate if it convincingly communicates silk responding to wind. Keep the fabric recognizable and the support relationship visible on mobile as well as desktop.

Deliver exactly one authored file, index.html, that runs offline in the provided browser. Write HTML, CSS and JavaScript inside that file. You may import the supplied local ES module /vendor/three.module.min.js; its matching core module is supplied too. Plain Canvas or WebGL is equally acceptable. Do not request external URLs, fonts, images, libraries, services or other assets. Do not copy an existing demo. Create your own composition; the visual result matters more than any particular rendering technique. Do not claim validated physical accuracy.

Make the animation start automatically and visibly change within the first five seconds. Put its main rendered content inside one element with data-testid="scene", either a canvas or a container. Keep the scene substantial and readable at both 1280×800 and 390×844. Controls must stay visible, legible and usable without covering the main subject or requiring horizontal scrolling. Choose your own palette, lighting, framing and visual style. Extra features are optional; prioritize the core scene.

Provide these visible, labeled controls:
- A native button with data-testid="pause". Toggle pause/resume; aria-pressed="true" means paused and "false" means playing. Pause freezes all scene motion and simulation time.
- A native button with data-testid="reset". Restore the initial scene, camera if any, simulation time and intensity; resume playing.
- A native input type="range" with data-testid="intensity", min="0", max="1", step="0.01", and initial value "0.4". Increasing intensity must visibly strengthen the specified behavior, not merely change a number. Keep the scene usable across this range.

Expose window.__SHOWCASE__.getState(), returning an object with ready (boolean), paused (boolean), simTime (finite seconds), intensity (number), and resetCount (integer starting at zero and increasing on each reset). Set ready only after the initial scene has rendered. Simulation time progresses while playing and freezes while paused. This API is diagnostic; screenshots and working controls are independently inspected.

You may use the common browser inspection tool within your allowed budget to inspect and improve your own actual output. The inspector records desktop views near 0, 2 and 5 seconds, a higher-intensity view at 0.75, and a mobile view; it also tests pause and reset. Do not implement special evaluator-only visuals.

The five visual review dimensions are:
1. A coherent fabric surface and its support or anchored edge are recognizable.
2. Changing folds, bends or ripples make the surface feel flexible.
3. Free fabric moves continuously while its supported region stays attached.
4. Intensity visibly changes the wind response; pause and reset behave correctly.
5. Shading reveals folds and the complete composition remains usable on desktop and mobile.

## The common contract

The host-side contract shared by all briefs of this experiment (`cases.json` `common_contract`):

```json
{
  "submission": "index.html",
  "dependency_url": "/vendor/three.module.min.js",
  "dependency_core_url": "/vendor/three.core.min.js",
  "network": "No external requests",
  "desktop": [
    1280,
    800
  ],
  "mobile": [
    390,
    844
  ],
  "scene_selector": "[data-testid=\"scene\"]",
  "pause_selector": "button[data-testid=\"pause\"]",
  "reset_selector": "button[data-testid=\"reset\"]",
  "intensity_selector": "input[type=\"range\"][data-testid=\"intensity\"]",
  "intensity": {
    "min": 0,
    "max": 1,
    "step": 0.01,
    "initial": 0.4,
    "inspection": 0.75
  },
  "diagnostic_api": "window.__SHOWCASE__.getState()",
  "diagnostic_fields": [
    "ready",
    "paused",
    "simTime",
    "intensity",
    "resetCount"
  ],
  "reset_behavior": "Restore initial visual state, camera if applicable, simTime and intensity; resume playing; increment resetCount.",
  "observation": "Actual screenshots near t=0,2,5 seconds, high intensity, mobile, plus pause/reset diagnostics. Self-reported state is not independent visual or physical evidence."
}
```
