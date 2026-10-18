(() => {
  'use strict';
  const { options, render } = window.AdapterArrowStyles;
  const list = document.getElementById('arrow-options');
  options.forEach(option => {
    const article = document.createElement('article');
    article.className = 'arrow-option';
    article.innerHTML = `<div class="option-title"><h2>${option.id} · ${option.name}</h2><a href="animation.html?arrow=${option.id}">在完整动画里预览 ↗</a></div><svg viewBox="0 0 560 96" role="img" aria-label="${option.name}"><rect x="16" y="24" width="144" height="48" class="sample-core"/><text x="88" y="54" text-anchor="middle">Agent</text>${render(option.id, 190, 318, 48, `sample-${option.id}`)}<rect x="350" y="24" width="194" height="48" class="sample-scenario"/><text x="447" y="54" text-anchor="middle">New scenario</text></svg><p>${option.note}</p>`;
    list.append(article);
  });
})();
