# Visual showcase: Ocean waves

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

Ocean waves

Create an evocative animated ocean scene that makes the movement of water immediately understandable. A viewer should see a continuous water surface, overlapping or evolving swells, and highlights or shading that move coherently with that surface. Frame the ocean so depth, scale and the progression of waves are easy to read rather than hidden by an extreme camera angle. The intensity control represents wind or sea strength: increasing it should make the water visibly more energetic while preserving a believable, stable scene.

You decide whether the mood is calm, dramatic, stylized or realistic. There is no required sunset, vessel, foam technique, camera interaction or numerical solver. Decorative details may support the scene but must not substitute for animated water. Avoid presenting a static gradient with unrelated floating particles as the ocean. Aim for a scene people would enjoy watching for more than a single screenshot.

Deliver exactly one authored file, index.html, that runs offline in the provided browser. Write HTML, CSS and JavaScript inside that file. You may import the supplied local ES module /vendor/three.module.min.js; its matching core module is supplied too. Plain Canvas or WebGL is equally acceptable. Do not request external URLs, fonts, images, libraries, services or other assets. Do not copy an existing demo. Create your own composition; the visual result matters more than any particular rendering technique. Do not claim validated physical accuracy.

Make the animation start automatically and visibly change within the first five seconds. Put its main rendered content inside one element with data-testid="scene", either a canvas or a container. Keep the scene substantial and readable at both 1280×800 and 390×844. Controls must stay visible, legible and usable without covering the main subject or requiring horizontal scrolling. Choose your own palette, lighting, framing and visual style. Extra features are optional; prioritize the core scene.

Provide these visible, labeled controls:
- A native button with data-testid="pause". Toggle pause/resume; aria-pressed="true" means paused and "false" means playing. Pause freezes all scene motion and simulation time.
- A native button with data-testid="reset". Restore the initial scene, camera if any, simulation time and intensity; resume playing.
- A native input type="range" with data-testid="intensity", min="0", max="1", step="0.01", and initial value "0.4". Increasing intensity must visibly strengthen the specified behavior, not merely change a number. Keep the scene usable across this range.

Expose window.__SHOWCASE__.getState(), returning an object with ready (boolean), paused (boolean), simTime (finite seconds), intensity (number), and resetCount (integer starting at zero and increasing on each reset). Set ready only after the initial scene has rendered. Simulation time progresses while playing and freezes while paused. This API is diagnostic; screenshots and working controls are independently inspected.

You may use the common browser inspection tool within your allowed budget to inspect and improve your own actual output. The inspector records desktop views near 0, 2 and 5 seconds, a higher-intensity view at 0.75, and a mobile view; it also tests pause and reset. Do not implement special evaluator-only visuals.

The five visual review dimensions are:
1. The ocean and its wave forms are immediately recognizable.
2. Wave motion evolves coherently without distracting popping or flicker.
3. Lighting or shading reveals surface shape and depth without obscuring it.
4. Intensity visibly changes sea strength; pause and reset behave correctly.
5. The composition and controls remain readable on desktop and mobile.

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
