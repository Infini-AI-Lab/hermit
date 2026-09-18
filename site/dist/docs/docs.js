'use strict';
document.querySelectorAll('pre').forEach(pre => {
  const button = document.createElement('button');
  button.textContent = 'Copy';
  button.setAttribute('aria-label', 'Copy code example');
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(pre.querySelector('code').textContent); button.textContent = 'Copied'; }
    catch { button.textContent = 'Select to copy'; }
    setTimeout(() => button.textContent = 'Copy', 1800);
  });
  pre.append(button);
});
