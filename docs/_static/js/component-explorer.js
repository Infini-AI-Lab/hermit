/* Local diagram controls; no model calls or external dependencies. */
document.querySelectorAll('.ha-explorer').forEach((root) => {
  const views = [...root.querySelectorAll('[data-view]')];
  const modes = [...root.querySelectorAll('[data-mode]')];
  const nodes = [...root.querySelectorAll('[data-step]')];
  const flow = root.querySelector('.ha-train-flow');
  const svg = root.querySelector('.ha-connections');
  const group = svg.querySelector('g');
  const details = [
    ['Run the current adapter', 'The Trainer exports the current Git version and gives its path and one task to your pipeline. The acting agent consumes the exports. The pipeline returns RunOutput: an answer plus any logs and usage it records.', 'task + adapter path → RunOutput'],
    ['Score the answer', 'The Trainer passes the task and RunOutput to your judge. Its score and feedback describe the adapter used for this batch, before the learner makes any edits.', 'task + RunOutput → score, feedback, extra fields'],
    ['Prepare evidence for the learner', 'The Trainer saves the results and available trajectories, then points learning_material/current at this step. The Evolver also gets task-info, adapter history, its persistent state, and the run’s tools.', 'results → learning_material/ + learner workspace'],
    ['Let the Evolver make an update', 'The learner reads the evidence and edits the working adapter directory. It may test candidates or return the previous version. It hands back one adapter on disk and a StepResult containing status, summary, and usage.', 'step(ctx) → edited adapter/ + StepResult'],
    ['Enforce the template specification', 'For a successful Evolver result, the Trainer checks allowed paths, fixed files, formats, and export loading. It restores violating files and checks again. This does not require a better task score.', 'candidate + template reference → conforming adapter'],
    ['Save the accepted version', 'The Trainer commits changes, saves the learner state and report, and records usage and checks. An invalid candidate and its repair are retained in history. The next batch uses the accepted version; no change means no new adapter commit.', 'accepted adapter → Git; state and results → run directory']
  ];
  let step = 0;
  function draw() {
    if (views.find((v) => v.dataset.view === 'train').hidden) return;
    const bounds = flow.getBoundingClientRect();
    svg.setAttribute('viewBox', `0 0 ${bounds.width} ${bounds.height}`);
    group.replaceChildren();
    nodes.slice(0, -1).forEach((node, i) => {
      const a = node.getBoundingClientRect();
      const b = nodes[i + 1].getBoundingClientRect();
      const sameRow = Math.abs(a.top - b.top) < 2;
      let x1, y1, x2, y2;
      if (sameRow) {
        const right = b.left > a.left;
        x1 = (right ? a.right : a.left) - bounds.left;
        x2 = (right ? b.left : b.right) - bounds.left;
        y1 = a.top + a.height / 2 - bounds.top;
        y2 = b.top + b.height / 2 - bounds.top;
      } else {
        x1 = a.left + a.width / 2 - bounds.left;
        x2 = b.left + b.width / 2 - bounds.left;
        y1 = a.bottom - bounds.top;
        y2 = b.top - bounds.top;
      }
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${x1} ${y1} L ${x2} ${y2}`);
      path.setAttribute('marker-end', 'url(#ha-arrowhead)');
      path.classList.toggle('is-active', i === step - 1);
      group.append(path);
      // What this step hands to the next: above a horizontal arrow (one word per line), left of a vertical one.
      const lines = (node.dataset.handoff || '').split('|').filter(Boolean);
      if (!lines.length) return;
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.classList.add('ha-handoff');
      label.classList.toggle('is-active', i === step - 1);
      label.setAttribute('text-anchor', sameRow ? 'middle' : 'end');
      const rows = sameRow ? lines : [lines.join(' ')];
      rows.forEach((row, j) => {
        const span = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
        span.setAttribute('x', sameRow ? (x1 + x2) / 2 : x1 - 8);
        span.setAttribute('y', sameRow ? y1 - 7 - (rows.length - 1 - j) * 13 : (y1 + y2) / 2 + 4);
        span.textContent = row;
        label.append(span);
      });
      group.append(label);
    });
  }
  function select(index) {
    step = index;
    nodes.forEach((n, i) => n.setAttribute('aria-pressed', String(i === step)));
    root.querySelector('[data-detail-title]').textContent = `${step + 1}. ${details[step][0]}`;
    root.querySelector('[data-detail-body]').textContent = details[step][1];
    root.querySelector('[data-detail-handoff]').textContent = details[step][2];
    root.querySelector('[data-progress]').textContent = `${step + 1} / ${nodes.length}`;
    root.querySelector('[data-prev]').disabled = step === 0;
    root.querySelector('[data-next]').disabled = step === nodes.length - 1;
    draw();
  }
  modes.forEach((button) => button.addEventListener('click', () => {
    modes.forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
    views.forEach((v) => { v.hidden = v.dataset.view !== button.dataset.mode; });
    draw();
  }));
  nodes.forEach((node, i) => node.addEventListener('click', () => select(i)));
  root.querySelector('[data-prev]').addEventListener('click', () => select(Math.max(0, step - 1)));
  root.querySelector('[data-next]').addEventListener('click', () => select(Math.min(nodes.length - 1, step + 1)));
  root.querySelector('.ha-switch').hidden = false;
  root.querySelector('.ha-step-controls').hidden = false;
  views.find((v) => v.dataset.view === 'train').hidden = true;
  new ResizeObserver(draw).observe(flow);
});
