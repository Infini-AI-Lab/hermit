(() => {
  'use strict';
  const root = document.getElementById('icon-picker');
  const groups = ['model', 'harness', 'scenario'];
  const summary = document.getElementById('selected-codes');
  const preview = document.getElementById('combined-preview');
  const clear = document.getElementById('clear-choices');
  const directions = document.getElementById('show-directions');
  if (directions) {
    directions.addEventListener('change', () => {
      root.dataset.arrows = directions.checked ? 'on' : 'off';
    });
  }
  const key = root.dataset.storageKey || (root.dataset.defaultHarness ? 'harness-adapter-symbol-studies-v1' : 'harness-adapter-icon-choices-v2');
  const chosen = { model: root.dataset.fixedModel, scenario: root.dataset.fixedScenario, ...(root.dataset.defaultHarness ? {harness:root.dataset.defaultHarness} : {}) };
  function update() {
    const any = Object.keys(chosen).length > 0;
    const symbolInput = [...root.querySelectorAll('input[name="harness"]')].find(input => input.value === chosen.harness);
    summary.textContent = root.dataset.defaultHarness && symbolInput ? symbolInput.closest('label').querySelector('strong').textContent : any ? groups.map(group => chosen[group] || '—').join(' + ') : '尚未选择';
    clear.hidden = !chosen.harness;
    const complete = groups.every(group => chosen[group]);
    preview.hidden = !complete;
    if (complete) {
      groups.forEach(group => {
        const svg = root.querySelector(`[data-icon="${chosen[group]}"] svg`);
        root.querySelectorAll(`[data-preview="${group}"]`).forEach(target => {
          const copy = svg.cloneNode(true);
          copy.setAttribute('aria-hidden', 'true');
          target.replaceChildren(copy);
        });
      });
    }
  }
  root.querySelectorAll('input[type="radio"]').forEach(input => {
    input.addEventListener('change', () => {
      chosen[input.name] = input.value;
      update();
      try { localStorage.setItem(key, JSON.stringify(chosen)); } catch (_) { /* Storage is optional. */ }
    });
  });
  clear.addEventListener('click', () => {
    delete chosen.harness;
    root.querySelectorAll('input[name="harness"]').forEach(input => { input.checked = false; });
    try { localStorage.removeItem(key); } catch (_) { /* Storage is optional. */ }
    update();
  });
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '{}');
    groups.forEach(group => {
      const input = [...root.querySelectorAll(`input[name="${group}"]`)].find(node => node.value === saved[group]);
      if (input) {
        input.checked = true;
        chosen[group] = input.value;
      }
    });
  } catch (_) { /* An empty picker is the safe default. */ }
  chosen.model = root.dataset.fixedModel;
  chosen.scenario = root.dataset.fixedScenario;
  update();
})();
