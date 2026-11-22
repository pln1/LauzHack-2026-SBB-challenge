// Incident list panel: resize it next to the map (drag the splitter) or detach it into a floating window that can be
// moved and resized on its own. State is remembered in localStorage; below 1024 px the panel stays a normal block.
(function () {
  const KEY = 'sbbListPanel';
  const MIN_W = 300, MIN_H = 240;
  const desktop = () => window.matchMedia('(min-width: 1024px)').matches;
  let st = { floating: false, w: 0.42, x: 24, y: 120, fw: 460, fh: 620 };
  try { Object.assign(st, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (_) {}
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (_) {} };
  const $ = id => document.getElementById(id);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function apply() {
    const grid = document.querySelector('.workspace-grid'), panel = $('incident-list-section'), btn = $('panel-float-btn');
    if (!grid || !panel) return;
    const floating = st.floating && desktop();
    grid.classList.toggle('list-floating', floating);
    panel.classList.toggle('is-floating', floating);
    grid.style.setProperty('--list-w', `${Math.round(clamp(st.w, 0.25, 0.65) * 1000) / 10}%`);
    if (floating) {
      // the window always stays fully inside the viewport (so its resize corner can be reached)
      st.fw = clamp(st.fw, MIN_W, window.innerWidth - 8); st.fh = clamp(st.fh, MIN_H, window.innerHeight - 8);
      st.x = clamp(st.x, 0, window.innerWidth - st.fw); st.y = clamp(st.y, 0, window.innerHeight - st.fh);
      Object.assign(panel.style, { left: st.x + 'px', top: st.y + 'px', width: st.fw + 'px', height: st.fh + 'px' });
    } else {
      ['left', 'top', 'width', 'height'].forEach(k => { panel.style[k] = ''; });
    }
    if (btn) {
      const key = floating ? 'panelDock' : 'panelFloat';
      btn.textContent = floating ? '⇲ ' + t(key) : '⇱ ' + t(key);
      btn.title = t(floating ? 'panelDockTitle' : 'panelFloatTitle');
      btn.setAttribute('aria-pressed', String(floating));
    }
    const handle = $('panel-drag-handle'); if (handle) handle.title = t('panelHandleTitle');
    const grip = $('panel-resize-grip'); if (grip) grip.title = t('panelResizeTitle');
    if (typeof map !== 'undefined' && map) setTimeout(() => map.invalidateSize(), 60);   // the map area changed size
    queueSync();
  }

  function drag(el, onMove) {
    el.addEventListener('pointerdown', ev => {
      if (ev.button !== 0 || !desktop() || ev.target.closest('button, a, input, select')) return;   // buttons keep their own click
      ev.preventDefault(); el.setPointerCapture(ev.pointerId);
      const sx = ev.clientX, sy = ev.clientY;
      const move = e => onMove(e.clientX - sx, e.clientY - sy, e);
      const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); save(); apply(); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    });
  }

  window.togglePanelFloat = function () {
    st.floating = !st.floating;
    if (st.floating) {   // start where the docked panel is
      const r = $('incident-list-section').getBoundingClientRect();
      st.x = Math.round(r.left); st.y = Math.round(Math.max(r.top, 80)); st.fw = Math.round(r.width); st.fh = Math.min(Math.round(window.innerHeight - st.y - 16), 760);
    }
    save(); apply();
  };
  window.applyPanelLayout = apply;

  // the docked list sticks just below the sticky header: measure the header once and on resize
  function setStickyTop() {
    const h = $('app-header');
    document.documentElement.style.setProperty('--sticky-top', (h ? Math.round(h.getBoundingClientRect().height) + 12 : 132) + 'px');
  }
  // docked layout: the list is as tall as the right column (map + selected incident + SBB links) and scrolls inside;
  // both columns stay under the header while the page scrolls, if the right column fits on screen
  function syncHeights() {
    const grid = document.querySelector('.workspace-grid'), right = $('map-section'), root = document.documentElement;
    if (!grid || !right) return;
    const docked = desktop() && !grid.classList.contains('list-floating');
    if (!docked) { root.style.removeProperty('--list-h'); grid.classList.remove('ws-sticky'); return; }
    setStickyTop();
    const h = Math.round(right.offsetHeight);
    if (h > 0) root.style.setProperty('--list-h', h + 'px');
    const top = parseInt(getComputedStyle(root).getPropertyValue('--sticky-top'), 10) || 132;
    grid.classList.toggle('ws-sticky', h + top + 12 <= window.innerHeight);
  }
  window.syncPanelHeights = syncHeights;
  let syncTimer = 0;
  const queueSync = () => { cancelAnimationFrame(syncTimer); syncTimer = requestAnimationFrame(syncHeights); };
  const fixMap = () => { if (typeof map !== 'undefined' && map) map.invalidateSize(); };
  window.addEventListener('resize', () => { setStickyTop(); queueSync(); fixMap(); });
  window.addEventListener('hashchange', () => setTimeout(fixMap, 60));

  window.addEventListener('DOMContentLoaded', () => {
    setStickyTop();
    if (window.ResizeObserver) {
      const right = $('map-section'), mapEl = $('swiss-map');
      if (right) new ResizeObserver(queueSync).observe(right);
      if (mapEl) new ResizeObserver(fixMap).observe(mapEl);
    }
    queueSync();
    const panel = $('incident-list-section'), grid = document.querySelector('.workspace-grid');
    if (!panel || !grid) return;
    // splitter between the docked list and the map
    const splitter = $('panel-splitter');
    if (splitter) {
      let startW = 0;
      splitter.addEventListener('pointerdown', () => { startW = panel.getBoundingClientRect().width; });
      drag(splitter, (dx) => { st.w = clamp((startW + dx) / grid.getBoundingClientRect().width, 0.25, 0.65); grid.style.setProperty('--list-w', `${st.w * 100}%`); });
      splitter.addEventListener('keydown', e => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { st.w = clamp(st.w + (e.key === 'ArrowRight' ? 0.02 : -0.02), 0.25, 0.65); save(); apply(); e.preventDefault(); }
      });
    }
    // move the floating window by its header
    const handle = $('panel-drag-handle');
    if (handle) {
      let ox = 0, oy = 0;
      handle.addEventListener('pointerdown', () => { ox = st.x; oy = st.y; });
      drag(handle, (dx, dy) => { if (!st.floating) return; st.x = ox + dx; st.y = oy + dy; apply(); });
    }
    // resize the floating window from its corner
    const grip = $('panel-resize-grip');
    if (grip) {
      let ow = 0, oh = 0;
      grip.addEventListener('pointerdown', () => { ow = st.fw; oh = st.fh; });
      drag(grip, (dx, dy) => { st.fw = ow + dx; st.fh = oh + dy; apply(); });
    }
    window.addEventListener('resize', apply);
    apply();
  });
})();
