"""The real browser runtime (node, Playwright from runtime/node_modules, the installed Chrome) on a small
contract-following page. Opt in with VISUAL_REAL_CAPTURE=1: it launches Chrome for about 15 s. No model call."""
import json
import os
from pathlib import Path
import sys

import pytest

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
sys.path.insert(0, str(TASK))
import harness  # noqa: E402
import judge  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("VISUAL_REAL_CAPTURE") != "1",
                                reason="set VISUAL_REAL_CAPTURE=1 to launch the real browser capture")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Capture fixture</title>
<style>html,body{margin:0;height:100%;overflow:hidden;background:#000}canvas{width:100vw;height:100vh;display:block}
#ui{position:fixed;left:8px;bottom:8px;display:flex;gap:8px;background:#fff;padding:6px}</style></head><body>
<canvas data-testid="scene"></canvas><div id="ui"><button data-testid="pause" aria-pressed="false">Pause</button>
<button data-testid="reset">Reset</button><label>Intensity <input type="range" data-testid="intensity" min="0" max="1"
step="0.01" value="0.4"></label></div><script>
const c=document.querySelector('canvas'),g=c.getContext('2d'),slider=document.querySelector('[data-testid=intensity]');
const pause=document.querySelector('[data-testid=pause]');let t=0,last=null,paused=false,resets=0,ready=false;
pause.onclick=()=>{paused=!paused;pause.setAttribute('aria-pressed',String(paused));};
document.querySelector('[data-testid=reset]').onclick=()=>{t=0;paused=false;pause.setAttribute('aria-pressed','false');
slider.value='0.4';resets++;};
window.__SHOWCASE__={getState:()=>({ready,paused,simTime:t,intensity:Number(slider.value),resetCount:resets})};
function frame(now){if(last!==null&&!paused)t+=(now-last)/1000*(0.5+Number(slider.value));last=now;
c.width=innerWidth;c.height=innerHeight;for(let x=0;x<c.width;x+=8){const v=128+127*Math.sin(x/40+t*3);
g.fillStyle=`rgb(${v|0},${(255-v)|0},200)`;g.fillRect(x,0,8,c.height);}ready=true;requestAnimationFrame(frame);}
requestAnimationFrame(frame);</script></body></html>"""


def test_real_capture_of_a_contract_page(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "index.html").write_text(PAGE)
    summary = harness.capture(tmp_path / "ws", tmp_path / "out", "waves")
    assert summary["capture_ok"] is True, json.dumps(summary.get("capture_errors"))
    assert sorted(p.name for p in (tmp_path / "out").glob("*.png")) == [
        "desktop-interaction.png", "desktop-t0.png", "desktop-t2.png", "desktop-t5.png", "diagnostic-paused.png",
        "phone.png"]
    assert summary["input"]["index_sha256"] == harness.sha(tmp_path / "ws" / "index.html")
    items = judge.mechanical(summary, {"valid_submission": True})
    assert all(i["passed"] for i in items), [i for i in items if not i["passed"]]
