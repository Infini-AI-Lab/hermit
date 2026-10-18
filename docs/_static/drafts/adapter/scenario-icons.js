(() => {
  'use strict';
  const root = document.getElementById('icon-picker');
  const summary = document.getElementById('selected-codes');
  const inputs = [...root.querySelectorAll('input[name="scenario"]')];
  const storageKey = 'harness-adapter-scenario-study-v1';
  function update(input) {
    const label = input.closest('label');
    const svg = label.querySelector('[data-icon] svg');
    root.querySelectorAll('[data-preview="scenario"]').forEach(target => {
      const copy = svg.cloneNode(true);
      copy.setAttribute('aria-hidden', 'true');
      target.replaceChildren(copy);
    });
    summary.textContent = label.querySelector('strong').textContent;
  }
  inputs.forEach(input => input.addEventListener('change', () => {
    update(input);
    try { localStorage.setItem(storageKey, input.value); } catch (_) { /* Optional. */ }
  }));
  let selected = inputs.find(input => input.checked);
  try {
    const stored = inputs.find(input => input.value === localStorage.getItem(storageKey));
    if (stored) { stored.checked = true; selected = stored; }
  } catch (_) { /* S12 remains the default selection. */ }
  update(selected);
})();
