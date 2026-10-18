(() => {
  'use strict';
  const inputs = [...document.querySelectorAll('input[name="harness-adapter-icon"]')];
  const preview = document.getElementById('preview-glyph');
  const selectedLabel = document.getElementById('selected-icon');
  const storageKey = 'harness-adapter-gallery-preview-v2';
  function update(input) {
    const choice = input.closest('label');
    const svg = choice.querySelector('svg').cloneNode(true);
    svg.setAttribute('aria-hidden', 'true');
    preview.replaceChildren(svg);
    selectedLabel.textContent = choice.querySelector('strong').textContent;
  }
  inputs.forEach(input => input.addEventListener('change', () => {
    update(input);
    try { localStorage.setItem(storageKey, input.value); } catch (_) { /* Preview also works without storage. */ }
  }));
  let selected = inputs.find(input => input.checked);
  try {
    const saved = inputs.find(input => input.value === localStorage.getItem(storageKey));
    if (saved) { saved.checked = true; selected = saved; }
  } catch (_) { /* Use the current adapter icon. */ }
  update(selected);
})();
