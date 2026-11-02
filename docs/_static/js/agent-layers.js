/* Agent layers: highlight the cells one call path goes through. Without this script every cell shows. */
(() => {
  const NOTES = {
    all: 'Every agent goes through all three layers. A method that decides every step in its own code still has a Base Agent: the simplest one, direct.',
    officeqa: "Acting: OfficeQA's task functions place task.md and the corpus in the workspace, the adapter is deployed under hermit/<id>/ as hermit deploy does, and Codex answers in a container of its own.",
    clbench: "Acting: CL-Bench's runner keeps its own loop (the game, its turns and scoring) in Layer 3 and reaches its model through a local endpoint; each request is one direct call.",
    gepa: 'Learning: GEPA decides every step in its own code and needs only text back from the model, so its Base Agent is direct (ctx.agent).',
    mha: "Learning: Meta-Harness Adapter's proposer is a Codex session that reads the learning material and writes candidate adapters.",
    harness2: "Learning: Harness²'s proposer is started by the upstream code; each of its sessions is one claude call.",
  };
  function init() {
    document.querySelectorAll('.ha-layers').forEach((root) => {
      if (root.dataset.ready) return;
      root.dataset.ready = 'true';
      const stack = root.querySelector('.ha-layers-stack');
      const note = root.querySelector('.ha-layers-note');
      const buttons = root.querySelectorAll('.ha-layers-paths button');
      function show(path) {
        buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.path === path)));
        stack.classList.toggle('ha-layers-filtered', path !== 'all');
        stack.querySelectorAll('.ha-layers-cell').forEach((cell) => {
          cell.classList.toggle('ha-layers-on', (cell.dataset.on || '').split(' ').includes(path));
        });
        note.textContent = NOTES[path];
      }
      buttons.forEach((b) => b.addEventListener('click', () => show(b.dataset.path)));
      root.querySelector('.ha-layers-paths').hidden = false;
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
