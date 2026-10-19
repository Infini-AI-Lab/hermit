(() => {
  'use strict';
  const duration = 9;
  const byId = id => document.getElementById(id);
  const timeline = byId('timeline');
  const playButton = byId('play-pause');
  const status = byId('animation-status');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const clamp = x => Math.max(0, Math.min(1, x));
  const ease = x => { const t = clamp(x); return t * t * (3 - 2 * t); };
  const fade = (time, at, length = .45) => ease((time - at) / length);
  const alpha = (element, value) => element.setAttribute('opacity', value.toFixed(3));
  const arrow = new URLSearchParams(location.search).get('arrow') || '04';
  if (window.AdapterArrowStyles.options.some(option => option.id === arrow)) {
    ['harness'].forEach(name => {
      byId(`${name}-flow`).innerHTML = window.AdapterArrowStyles.render(arrow, 436, 602, 82, `main-${name}`);
    });
  }
  const subtitleTimes = [.1, 1.6, 2.8];
  const rows = ['harness'].map((name, i) => ({
    name, adapter: byId(`${name}-adapter`),
    limited: byId(`${name}-limited`), better: byId(`${name}-better`),
    scenarioBox: byId(`${name}-limited`).closest('.scenario').querySelector('.scenario-box'),
    feedback: byId(`${name}-feedback`),
    versions: [0, 1, 2, 3, 4, 5].map(version => byId(`${name}-v${version}`)),
    frozenAt: 1.6, adapterAt: 2.2,            // the badge, then the plugin, before any training
    feedbackAt: 2.8,
    trainAt: 3,
    trainFor: 3,
  }));
  let time = 0, playing = false, previous = null, frame = null, currentPhase = '';
  function render(t) {
    time = Math.min(duration, Math.max(0, t));
    rows.forEach(row => {
      const trainEnd = row.trainAt + row.trainFor;
      const progress = clamp((time - row.trainAt) / row.trainFor);
      const fit = fade(time, trainEnd - .25, .25);
      const stateColor = progress < .5
        ? `color-mix(in srgb, var(--fail) ${(1 - progress * 2) * 100}%, #d8a036 ${progress * 200}%)`
        : `color-mix(in srgb, #d8a036 ${(2 - progress * 2) * 100}%, var(--pass) ${(progress * 2 - 1) * 100}%)`;
      alpha(byId('frozen-badge'), fade(time, row.frozenAt, .4));
      alpha(row.adapter, fade(time, row.adapterAt, .4));
      row.versions.forEach((element, version) => {
        const last = row.versions.length - 1, step = row.trainFor / last;
        const enter = version === 0 ? 1 : fade(time, row.trainAt + version * step, .08);
        const leave = version === last ? 0 : fade(time, row.trainAt + (version + 1) * step, .08);
        alpha(element, enter * (1 - leave));
      });
      alpha(row.feedback, fade(time, row.feedbackAt, .25) * (1 - .35 * fade(time, trainEnd + .2, .3)));
      alpha(row.limited, 1 - fit);
      alpha(row.better, fit);
      row.scenarioBox.style.stroke = stateColor;
    });
    subtitleTimes.forEach((at, i) => { byId(`subtitle-${i + 1}`).style.opacity = fade(time, at, .9).toFixed(3); });
    timeline.value = time.toFixed(2);
    timeline.style.setProperty('--progress', `${time / duration * 100}%`);
    byId('time').textContent = `${time.toFixed(1)} / ${duration}s`;
    const active = rows[0];
    const last = active.versions.length - 1;
    const activeVersion = Math.min(last, Math.max(0, Math.floor((time - active.trainAt) / (active.trainFor / last))));
    const phase = `Harness adapter V${activeVersion}: trained from task feedback while the agent stays frozen.`;
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
