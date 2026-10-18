(() => {
  'use strict';
  const duration = 5;
  const byId = id => document.getElementById(id);
  const timeline = byId('timeline');
  const playButton = byId('play-pause');
  const status = byId('animation-status');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const clamp = x => Math.max(0, Math.min(1, x));
  const ease = x => { const t = clamp(x); return t * t * (3 - 2 * t); };
  const fade = (time, at, length = .45) => ease((time - at) / length);
  const alpha = (element, value) => element.setAttribute('opacity', value.toFixed(3));
  const icon = new URLSearchParams(location.search).get('icon');
  if (/^0[1-6]$/.test(icon || '')) byId('model-adapter-glyph').setAttribute('href', `#adapter-icon-${icon}`);
  const arrow = new URLSearchParams(location.search).get('arrow') || '04';
  if (window.AdapterArrowStyles.options.some(option => option.id === arrow)) {
    ['model', 'harness'].forEach(name => {
      byId(`${name}-flow`).innerHTML = window.AdapterArrowStyles.render(arrow, 454, 582, 82, `main-${name}`);
    });
  }
  const subtitleTimes = [.1, 1.8, 2.7];
  const rows = ['model', 'harness'].map((name, i) => ({
    name, adapter: byId(`${name}-adapter`),
    limited: byId(`${name}-limited`), better: byId(`${name}-better`),
    scenarioBox: byId(`${name}-limited`).closest('.scenario').querySelector('.scenario-box'),
    adapterAt: i === 0 ? .8 : 2.7,
  }));
  let time = 0, playing = false, previous = null, frame = null, currentPhase = '';
  function render(t) {
    time = Math.min(duration, Math.max(0, t));
    // Reveal the lower comparison only after the model adapter has settled.
    alpha(byId('harness-row'), fade(time, 1.65, .35));
    rows.forEach(row => {
      const adapterAmount = fade(time, row.adapterAt, .35);
      alpha(row.adapter, adapterAmount);
      alpha(row.limited, 1 - adapterAmount);
      alpha(row.better, adapterAmount);
      row.scenarioBox.style.stroke = `color-mix(in srgb, var(--fail) ${(1 - adapterAmount) * 100}%, var(--pass) ${adapterAmount * 100}%)`;
    });
    subtitleTimes.forEach((at, i) => { byId(`subtitle-${i + 1}`).style.opacity = fade(time, at, .9).toFixed(3); });
    timeline.value = time.toFixed(2);
    timeline.style.setProperty('--progress', `${time / duration * 100}%`);
    byId('time').textContent = `${time.toFixed(1)} / ${duration}s`;
    const phase = time < .8 ? 'A frozen model meets a new scenario.' : time < 1.8 ? 'A model adapter improves the fit.' : time < 2.7 ? 'An agent meets a new scenario. Its model and harness are frozen.' : 'A harness adapter improves adaptation while the original model and harness stay unchanged.';
    if (phase !== currentPhase) { status.textContent = phase; currentPhase = phase; }
  }
  function setPlaying(value) {
    playing = value;
    playButton.setAttribute('aria-label', value ? 'Pause animation' : 'Play animation');
    byId('pause-icon').toggleAttribute('hidden', !value);
    byId('play-icon').toggleAttribute('hidden', value);
    previous = null;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = value ? requestAnimationFrame(tick) : null;
  }
  function tick(now) {
    if (!playing) return;
    if (previous !== null) render(time + (now - previous) / 1000);
    previous = now;
    if (time >= duration) setPlaying(false);
    else frame = requestAnimationFrame(tick);
  }
  playButton.addEventListener('click', () => {
    if (playing) setPlaying(false);
    else { if (time >= duration) render(0); setPlaying(true); }
  });
  byId('replay').addEventListener('click', () => { render(0); setPlaying(true); });
  timeline.addEventListener('input', () => { setPlaying(false); render(Number(timeline.value)); });
  reducedMotion.addEventListener('change', event => { if (event.matches) { setPlaying(false); render(duration); } });
  document.addEventListener('visibilitychange', () => { if (document.hidden) previous = null; });
  render(0);
  document.fonts.ready.then(() => {
    document.querySelectorAll('[data-center-x]').forEach(group => {
      const bounds = group.getBBox();
      const offset = Number(group.dataset.centerX) - bounds.x - bounds.width / 2;
      group.setAttribute('transform', `translate(${offset.toFixed(3)} 0)`);
    });
    if (reducedMotion.matches) { render(duration); setPlaying(false); }
    else setPlaying(true);
  });
})();
