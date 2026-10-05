/* Overview diagrams: native HTML nodes and inline SVG connections. */
(() => {
  function init() {
    document.querySelectorAll('.ha-live-map').forEach((root) => {
      if (root.dataset.ready) return;
      root.dataset.ready = 'true';
      const nodes = [...root.querySelectorAll('[data-node]')];
      const edges = [...root.querySelectorAll('.ha-map-edge')];
      const byId = new Map(nodes.map((node) => [node.dataset.node, node]));
      const reset = root.querySelector('[data-reset]');
      const size = root.querySelector('[data-size]');
      const detail = root.querySelector('.ha-map-detail');
      const viewport = root.querySelector('.ha-map-viewport');
      const stage = root.querySelector('.ha-map-stage');
      const canvas = root.querySelector('.ha-map-canvas');
      let selected = null;
      let enlarged = false;

      function fit() {
        // Keep labels legible on phones; the viewport can scroll horizontally.
        const available = viewport.clientWidth || 1000;
        const scale = enlarged ? 1 : Math.max(0.65, Math.min(1, available / 1000));
        canvas.style.transform = `scale(${scale})`;
        stage.style.width = `${1000 * scale}px`;
        stage.style.height = `${Number(root.dataset.height) * scale}px`;
      }

      function select(id) {
        selected = id;
        const connections = edges.filter((e) => e.dataset.from === id || e.dataset.to === id);
        const related = new Set([id]);
        connections.forEach((e) => { related.add(e.dataset.from); related.add(e.dataset.to); });
        nodes.forEach((node) => {
          node.setAttribute('aria-pressed', String(node.dataset.node === id));
          node.classList.toggle('ha-map-muted', id !== null && !related.has(node.dataset.node));
        });
        edges.forEach((edge) => {
          edge.classList.toggle('ha-map-muted', id !== null && !connections.includes(edge));
          edge.classList.toggle('ha-map-highlight', id !== null && connections.includes(edge));
        });
        reset.disabled = id === null;
        detail.hidden = id === null;
        if (id === null) return;
        const node = byId.get(id);
        root.querySelector('[data-detail-title]').textContent = node.querySelector('strong').textContent;
        root.querySelector('[data-detail-text]').textContent = node.dataset.detail;
        const list = root.querySelector('[data-detail-links]');
        list.replaceChildren();
        connections.forEach((edge) => {
          const item = document.createElement('li');
          const from = byId.get(edge.dataset.from).querySelector('strong').textContent;
          const to = byId.get(edge.dataset.to).querySelector('strong').textContent;
          item.textContent = `${from} → ${to}${edge.dataset.label === 'connects to' ? '' : ': ' + edge.dataset.label}`;
          list.append(item);
        });
      }

      nodes.forEach((node) => {
        if (Number.parseFloat(node.style.height) < 80) node.classList.add('ha-map-compact');
        node.addEventListener('click', () => select(selected === node.dataset.node ? null : node.dataset.node));
      });
      reset.addEventListener('click', () => select(null));
      root.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') select(null);
      });
      size.addEventListener('click', () => {
        enlarged = !enlarged;
        size.setAttribute('aria-pressed', String(enlarged));
        size.textContent = enlarged ? 'Fit width' : 'Enlarge';
        fit();
      });
      root.querySelector('.ha-map-toolbar').hidden = false;
      fit();
      if (typeof ResizeObserver !== 'undefined') new ResizeObserver(fit).observe(viewport);
      else window.addEventListener('resize', fit);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
