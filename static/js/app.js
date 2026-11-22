// =========================================================================
    // 1. UI TRANSLATIONS (EN default + DE / FR / IT)
    // =========================================================================
    // UI text lives in static/i18n/{en,de,fr,it}.json (flat key -> text map per language); edit those files to change wording.
    const I18N = { EN: {}, DE: {}, FR: {}, IT: {} };
    async function loadI18n() {
      await Promise.all(Object.keys(I18N).map(async l => {
        try {
          const res = await fetch(`/static/i18n/${l.toLowerCase()}.json`, { cache: 'no-cache' });
          if (res.ok) I18N[l] = await res.json();
        } catch (e) { console.warn('[SBB] translations', l, e); }
      }));
    }

    // =========================================================================
    // 2. STATE & API CLIENT
    // =========================================================================
    const API = '/api';
    const state = {
      lang: 'EN',
      dataMode: 'live',
      statusFilter: 'ALL',
      quickFilter: null,
      searchQuery: '',
      dateFilter: '',
      selectedIncidentId: null,
      incidents: [],
      kpis: null,
      totalCount: 0,
      detail: null,
      auditExpanded: true,
      contextExpanded: false,
      drawerOpen: false,
      sessionId: null,
      renderedMessageIds: new Set(),
      selectedRouteChip: null,
      datasetDates: [],
      busy: false
    };
    const ACCESSIBILITY_PREFS_KEY = 'sbb_accessibility_preferences';
    const ACCESSIBILITY_LABELS = {
      EN: ['Accessibility', 'Display and accessibility', 'Larger text', 'High contrast', 'Display settings are saved on this device. Screen-reader and keyboard support are available without turning on a setting.', 'Skip to main content', 'Press Enter or Space to select this incident on the map.'],
      DE: ['Barrierefreiheit', 'Anzeige und Barrierefreiheit', 'Grössere Schrift', 'Hoher Kontrast', 'Anzeigeeinstellungen werden auf diesem Gerät gespeichert. Tastatur- und Screenreader-Unterstützung ist immer verfügbar.', 'Zum Hauptinhalt springen', 'Drücken Sie Enter oder die Leertaste, um diese Störung auf der Karte auszuwählen.'],
      FR: ['Accessibilité', 'Affichage et accessibilité', 'Texte agrandi', 'Contraste élevé', 'Les réglages sont enregistrés sur cet appareil. La navigation au clavier et avec un lecteur d’écran est toujours disponible.', 'Aller au contenu principal', 'Appuyez sur Entrée ou Espace pour sélectionner cet incident sur la carte.'],
      IT: ['Accessibilità', 'Visualizzazione e accessibilità', 'Testo più grande', 'Contrasto elevato', 'Le impostazioni vengono salvate su questo dispositivo. Il supporto per tastiera e lettori di schermo è sempre disponibile.', 'Vai al contenuto principale', 'Premi Invio o Spazio per selezionare questo guasto sulla mappa.']
    };
    let accessibilityPrefs = { largeText: false, highContrast: false, darkTheme: false };
    try { accessibilityPrefs = { ...accessibilityPrefs, ...JSON.parse(localStorage.getItem(ACCESSIBILITY_PREFS_KEY) || '{}') }; } catch (_) {}
    let map = null, leaderGroup = null, mapLayersGroup = null, detourLayerGroup = null, searchTimer = null, pollTimer = null;

    const apiLang = () => state.lang.toLowerCase();
    function t(key) { return (I18N[state.lang] && I18N[state.lang][key]) || I18N.EN[key] || key; }

    async function api(path, options = {}) {
      const res = await fetch(API + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
        body: options.body ? JSON.stringify(options.body) : undefined
      });
      if (!res.ok) {
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        try { err.detail = (await res.json()).detail; } catch (_) {}
        throw err;
      }
      return res.status === 204 ? null : res.json();
    }

    function escapeHtml(str) {
      return String(str ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    // Escape, then allow **bold** and line breaks from the API (never raw HTML)
    function richText(str) {
      return escapeHtml(str).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
    }
    function statusStyles(status) {
      return status === 'ACTIVE' ? { badge: 'bg-red-100 text-[#EB0000] border-red-200', color: '#EB0000' }
           : status === 'RESTRICTED' ? { badge: 'bg-amber-100 text-amber-900 border-amber-200', color: '#F59E0B' }
           : { badge: 'bg-emerald-100 text-emerald-800 border-emerald-200', color: '#10B981' };
    }
    function validPoints(points) {
      return (points || []).filter(p => Array.isArray(p) && p.length === 2 && isFinite(p[0]) && isFinite(p[1]));
    }

    // =========================================================================
    // 3. INITIALISATION & LEAFLET MAP
    // =========================================================================
    window.addEventListener('DOMContentLoaded', async () => {
      await loadI18n();
      applyAccessibilitySettings();
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !document.getElementById('accessibility-panel').classList.contains('hidden')) {
          toggleAccessibilityPanel(false);
        }
      });
      initSwissMap();
      applyStaticTranslations();
      await loadDashboard(true);
      loadDatasetDates();
      pollTimer = setInterval(pollLive, 20000);
    });

    function applyAccessibilitySettings() {
      const root = document.documentElement;
      root.classList.toggle('accessibility-large-text', !!accessibilityPrefs.largeText);
      root.classList.toggle('accessibility-contrast', !!accessibilityPrefs.highContrast);
      root.classList.toggle('dark-theme', !!accessibilityPrefs.darkTheme);
      document.getElementById('accessibility-large-text').checked = !!accessibilityPrefs.largeText;
      document.getElementById('accessibility-high-contrast').checked = !!accessibilityPrefs.highContrast;
      const themeButton = document.getElementById('theme-toggle');
      const darkEnabled = !!accessibilityPrefs.darkTheme;
      themeButton.setAttribute('aria-pressed', String(darkEnabled));
      themeButton.setAttribute('aria-label', t(darkEnabled ? 'themeEnableLight' : 'themeEnableDark'));
      themeButton.title = t(darkEnabled ? 'themeEnableLight' : 'themeEnableDark');
      document.getElementById('theme-icon-moon').classList.toggle('hidden', darkEnabled);
      document.getElementById('theme-icon-sun').classList.toggle('hidden', !darkEnabled);
    }

    function setAccessibilityPreference(name, enabled) {
      if (!(name in accessibilityPrefs)) return;
      accessibilityPrefs[name] = !!enabled;
      try { localStorage.setItem(ACCESSIBILITY_PREFS_KEY, JSON.stringify(accessibilityPrefs)); } catch (_) {}
      applyAccessibilitySettings();
    }

    function toggleDarkTheme() {
      setAccessibilityPreference('darkTheme', !accessibilityPrefs.darkTheme);
    }

    function toggleAccessibilityPanel(forceOpen) {
      const panel = document.getElementById('accessibility-panel');
      const button = document.getElementById('accessibility-toggle');
      const open = forceOpen === undefined ? panel.classList.contains('hidden') : forceOpen;
      panel.classList.toggle('hidden', !open);
      button.setAttribute('aria-expanded', String(open));
      if (open) document.getElementById('accessibility-large-text').focus();
      else button.focus();
    }

    function applyAccessibilityLabels() {
      const labels = ACCESSIBILITY_LABELS[state.lang] || ACCESSIBILITY_LABELS.EN;
      ['accessibility-toggle', 'accessibility-heading', 'accessibility-large-label', 'accessibility-contrast-label', 'accessibility-help', 'skip-main']
        .forEach((id, i) => { document.getElementById(id).textContent = labels[i]; });
    }

    const SWISS_BOUNDS = [[45.80, 5.90], [47.85, 10.55]];
    let mapData = null, mapSig = '', renderedMapData = null, detourFocusMode = false;   // GeoJSON from GET /api/map (+ change signature)
    let incidentMapCache = { key: null, fc: null };   // GeoJSON from GET /api/incidents/{id}/map
    let stationLayerGroup = null, mapNoteTimer = null;

    function initSwissMap() {
      map = L.map('swiss-map', { zoomControl: true, scrollWheelZoom: true, minZoom: 6, maxBounds: [[44.2, 4.0], [49.2, 12.6]], maxBoundsViscosity: 0.5 })
        .fitBounds(SWISS_BOUNDS);
      const attribution = '&copy; <a href="https://www.swisstopo.admin.ch" target="_blank" rel="noopener">swisstopo</a> | ' +
        '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors | Stations: trainline-eu (ODbL)';
      // No API key needed. OpenStreetMap sits underneath the swisstopo tiles: it fills the area outside Switzerland and
      // shows through if the swisstopo service is unreachable, so the map is never blank.
      const osmUnder = () => L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution });
      const swisstopo = layer => L.tileLayer(`https://wmts.geo.admin.ch/1.0.0/${layer}/default/current/3857/{z}/{x}/{y}.jpeg`,
        { maxZoom: 18, bounds: [[45.70, 5.70], [47.95, 10.75]], attribution });
      const greyBase = L.layerGroup([osmUnder(), swisstopo('ch.swisstopo.pixelkarte-grau')]).addTo(map);
      const colourBase = L.layerGroup([osmUnder(), swisstopo('ch.swisstopo.pixelkarte-farbe')]);
      const osmBase = L.layerGroup([osmUnder()]);
      L.control.layers({ 'swisstopo (grey)': greyBase, 'swisstopo (colour)': colourBase, 'OpenStreetMap': osmBase }, null, { position: 'topright', collapsed: true }).addTo(map);
      mapLayersGroup = L.layerGroup().addTo(map);
      stationLayerGroup = L.layerGroup().addTo(map);
      detourLayerGroup = L.layerGroup().addTo(map);
      map.on('zoomend', declutterMarkers);
    }

    function setMapNote(text, transient) {
      const el = document.getElementById('map-note');
      if (!el) return;
      clearTimeout(mapNoteTimer);
      el.textContent = text || '';
      el.classList.toggle('hidden', !text);
      if (text && transient) mapNoteTimer = setTimeout(updateUnmappedNote, 4500);
    }
    function updateUnmappedNote() {
      const n = mapData && mapData.meta ? mapData.meta.unmappedCount : 0;
      setMapNote(n ? t('unmappedNote').replace('{n}', n) : '', false);
    }

    // Draws segments + markers from the GeoJSON of GET /api/map. Colour = status (red interrupted, amber dashed restricted, green resumed).
    function restyleMapSelection() {
      const sel = state.selectedIncidentId;
      mapLayersGroup.eachLayer(group => group.eachLayer && group.eachLayer(l => {
        const p = l.feature && l.feature.properties;
        if (!p || p.kind === 'detour') return;
        if (p.kind === 'segment') l.setStyle({ weight: p.id === sel ? 7 : 5, opacity: p.id === sel ? 0.95 : 0.75 });
        else l.setRadius(p.id === sel ? 9 : 7);
      }));
      mapLayersGroup.eachLayer(group => group.eachLayer && group.eachLayer(l => { if (l.feature && l.feature.properties.id === sel && l.feature.properties.kind === 'segment') l.bringToFront(); }));
      mapLayersGroup.eachLayer(group => group.eachLayer && group.eachLayer(l => { if (l instanceof L.CircleMarker) l.bringToFront(); }));
    }

    function renderMapMarkers() {
      if (!mapLayersGroup) return;
      if (renderedMapData === mapData && mapData) { restyleMapSelection(); return; }   // only the selection changed
      renderedMapData = mapData;
      mapLayersGroup.clearLayers();
      if (!mapData || !mapData.features) return;
      const sel = state.selectedIncidentId;
      const visibleIncidentIds = state.quickFilter ? getVisibleIncidentIds() : null;
      const layer = L.geoJSON(mapData, {
        filter: f => {
          const p = f.properties;
          if (visibleIncidentIds && !visibleIncidentIds.has(p.id)) return false;
          if (detourFocusMode) return p.id === sel && (p.kind === 'segment' || p.kind === 'incident');
          return p.kind === 'segment' || p.kind === 'incident' || p.kind === 'detour';
        },
        style: f => {
          const p = f.properties;
          if (p.kind === 'detour') return { color: p.color, weight: 5, opacity: 0.85, dashArray: '10, 8' };
          return { color: p.color, weight: p.id === sel ? 7 : 5, opacity: p.id === sel ? 0.95 : 0.75, dashArray: p.dashed ? '8, 6' : (p.approximate ? '2, 7' : null), lineCap: p.approximate && !p.dashed ? 'round' : 'butt' };
        },
        pointToLayer: (f, ll) => L.circleMarker(ll, {
          radius: f.properties.id === sel ? 9 : 7, fillColor: f.properties.color, color: '#FFFFFF', weight: 2.5, opacity: 1, fillOpacity: 1
        }),
        onEachFeature: (f, lyr) => {
          const p = f.properties;
          if (p.kind === 'detour') return;
          lyr.on('click', () => onMapPick(p.id));
          if (p.kind === 'segment') { lyr.bindTooltip(escapeHtml(p.title), { sticky: true }); return; }
          lyr.bindPopup(`
            <div class="p-1 font-sans">
              <div class="text-[10px] font-bold uppercase tracking-wider" style="color:${escapeHtml(p.color)}">${escapeHtml(p.statusLabel)}${p.hasReplacementBus ? ' · 🚌' : ''}</div>
              <div class="text-xs font-extrabold text-slate-900 mt-0.5">${escapeHtml(p.title)}</div>
              <div class="text-[11px] text-slate-500 mt-0.5">${t('linesLabel')}: ${escapeHtml((p.affectedLines || []).join(', ') || '—')} · v${Number(p.version)}</div>
              <button onclick="openAssistantDrawer(${Number(p.id)})" class="mt-2 w-full py-1.5 px-2.5 rounded-lg bg-[#EB0000] text-white text-[11px] font-bold">${t('popupBtn')}</button>
            </div>`, { maxWidth: 280 });
        }
      }).addTo(mapLayersGroup);
      layer.eachLayer(l => { if (l.feature && l.feature.properties.id === sel && l.feature.properties.kind === 'segment') l.bringToFront(); });
      layer.eachLayer(l => { if (l instanceof L.CircleMarker) l.bringToFront(); });
      declutterMarkers();
    }

    // Markers that would overlap on screen are spread on a small ring around their common centre, with a thin
    // leader line to the true position. Recomputed on every zoom, so zooming in dissolves the clusters.
    function declutterMarkers() {
      if (!mapLayersGroup || !map) return;
      if (!leaderGroup) leaderGroup = L.layerGroup().addTo(map);
      leaderGroup.clearLayers();
      const marks = [];
      mapLayersGroup.eachLayer(g => g.eachLayer && g.eachLayer(l => { if (l instanceof L.CircleMarker) marks.push(l); }));
      marks.forEach(m => { if (!m._orig) m._orig = m.getLatLng(); m.setLatLng(m._orig); });
      const R = 22, used = new Set();
      const px = marks.map(m => map.latLngToLayerPoint(m._orig));
      marks.forEach((m, i) => {
        if (used.has(i)) return;
        const grp = [i];
        marks.forEach((_, j) => { if (j !== i && !used.has(j) && px[i].distanceTo(px[j]) < R) grp.push(j); });
        grp.forEach(k => used.add(k));
        if (grp.length < 2) return;
        const c = grp.reduce((a, k) => a.add(px[k]), L.point(0, 0)).divideBy(grp.length);
        const ring = Math.max(R, grp.length * 5);
        grp.forEach((k, n) => {
          const ang = (2 * Math.PI * n) / grp.length - Math.PI / 2;
          const to = L.point(c.x + ring * Math.cos(ang), c.y + ring * Math.sin(ang));
          marks[k].setLatLng(map.layerPointToLatLng(to));
          L.polyline([marks[k]._orig, map.layerPointToLatLng(to)], { color: '#64748B', weight: 1, opacity: 0.7, interactive: false }).addTo(leaderGroup);
        });
      });
      marks.forEach(l => l.bringToFront());
    }

    // Click on the map: select the card, scroll it into view, and switch an open drawer to this incident.
    function onMapPick(id) {
      selectIncident(id, false);
      const card = document.querySelector(`#incidents-list article[data-incident-id="${id}"]`);
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      if (state.drawerOpen) openAssistantDrawer(id);
    }

    function fitIncident(id) {
      const f = mapData && (mapData.features || []).find(x => x.properties.kind === 'incident' && x.properties.id === id);
      if (f && f.properties.bounds) map.fitBounds(f.properties.bounds, { padding: [70, 70], maxZoom: 12 });
      else setMapNote(t('noCoords'), true);   // incident without coordinates: card only, the map is left alone
    }

    // Station points of the selected incident (GET /api/incidents/{id}/map), refreshed when its version changes.
    async function loadIncidentMap(id) {
      if (!stationLayerGroup) return;
      const inc = currentIncident();
      const key = id ? `${id}:${inc ? inc.currentVersion : ''}:${apiLang()}` : null;
      if (key && key === incidentMapCache.key && incidentMapCache.fc) return;
      stationLayerGroup.clearLayers();
      incidentMapCache = { key: null, fc: null };
      if (!id) return;
      let fc;
      try { fc = await api(`/incidents/${id}/map?lang=${apiLang()}`); } catch (e) { console.warn('[SBB] incident map', e); return; }
      incidentMapCache = { key, fc };
      if (state.selectedIncidentId !== id) return;
      L.geoJSON(fc, {
        filter: f => f.properties.kind === 'station',
        pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, fillColor: '#FFFFFF', color: f.properties.color, weight: 3, fillOpacity: 1 })
          .bindTooltip(escapeHtml(f.properties.name), { permanent: true, direction: f.properties.role === 'to' ? 'left' : 'right', offset: [f.properties.role === 'to' ? -6 : 6, 0], className: 'station-label' })
      }).addTo(stationLayerGroup);
    }

    // Detours: every alternative of the selected incident (GET /api/incidents/{id}/map). Solid green = routed along the rail
    // network; dotted = approximate (no track data for that stretch). Falls back to the route carried by the chat widget.
    function drawDetours(list, summaryFallback) {
      if (!detourLayerGroup) return;
      detourLayerGroup.clearLayers();
      const all = [];
      let bounds = null;
      const solid = list.filter(d => !d.approximate);
      list = (solid.length ? solid : list.slice(0, 1)).slice(0, 1);   // focused view shows the primary alternative only
      list.forEach((d, i) => {
        const pts = validPoints(d.route);
        if (pts.length < 2) return;
        const col = d.color || '#059669';
        const bus = d.kind === 'bus';
        const line = L.polyline(pts, { color: col, weight: bus ? 5 : 6, opacity: 0.95,
          dashArray: bus ? '2, 9' : (d.approximate ? '2, 8' : null), lineCap: bus ? 'round' : (d.approximate ? 'round' : 'butt') }).addTo(detourLayerGroup);
        if (bus) {
          line.bindTooltip(`🚌 ${t('busLabel')}`, { sticky: true });
          (d.stops || []).forEach(sp => L.marker(sp, { icon: L.divIcon({ className: '', html: '<div style="background:#2563EB;color:#fff;border:2px solid #fff;border-radius:6px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:12px;box-shadow:0 1px 4px #0006">🚌</div>', iconSize: [20, 20], iconAnchor: [10, 10] }), interactive: false }).addTo(detourLayerGroup));
        }
        line.bringToFront();
        const km = d.lengthKm ? ` · ${Number(d.lengthKm).toFixed(0)} km` : '';
        const sum = String(d.summary || summaryFallback || '');
        const popup = `<div class="text-xs font-bold ${bus ? 'text-blue-700' : 'text-emerald-800'}">${bus ? '🚌 ' + t('busLabel') + ': ' : ''}${escapeHtml(d.label || t('detourPopupTitle'))}${km}</div>`
          + `<div class="text-[11px] text-slate-600 mt-0.5">${richText(sum.length > 150 ? sum.slice(0, 147) + '…' : sum)}</div>`
          + (d.source === 'offset' ? `<div class="text-[10px] text-amber-700 mt-1">${t('busApprox')}</div>` : (d.approximate ? `<div class="text-[10px] text-amber-700 mt-1">${t('approxRoute')}</div>` : ''));
        line.bindPopup(popup, { maxWidth: 280 });
        if (i === 0) bounds = line.getBounds();   // fit to the primary alternative; the others stay drawn around it
        all.push(line);
      });
      if (!bounds) return;
      const f = mapData && (mapData.features || []).find(x => x.properties.kind === 'incident' && x.properties.id === state.selectedIncidentId);
      if (f && f.properties.bounds) bounds = bounds.extend(L.latLngBounds(f.properties.bounds));   // show the disruption and its detours together
      map.fitBounds(bounds, { padding: [60, 60] });
    }

    async function showDetourForSelected(fallback) {
      const id = state.selectedIncidentId;
      let list = [];
      try {
        const fc = (incidentMapCache.fc && incidentMapCache.key && incidentMapCache.key.startsWith(id + ':')) ? incidentMapCache.fc
          : await api(`/incidents/${id}/map?lang=${apiLang()}`);
        list = (fc.features || []).filter(x => x.properties.kind === 'detour').sort((a, b) => a.properties.index - b.properties.index)
          .map(x => ({ route: x.geometry.coordinates.map(c => [c[1], c[0]]), label: x.properties.label, kind: x.properties.detourKind,
                       approximate: x.properties.approximate, lengthKm: x.properties.lengthKm, source: x.properties.source, stops: x.properties.stops, summary: x.properties.summary, color: x.properties.color }));
      } catch (e) { console.warn('[SBB] detour geometry', e); }
      if (!list.length && fallback && validPoints(fallback.route).length >= 2) list = [{ route: fallback.route, summary: fallback.summary, approximate: true, label: fallback.label }];
      if (!list.length) { setMapNote(t('noDetour'), true); return; }
      detourFocusMode = true;
      renderedMapData = null;
      renderMapMarkers();
      drawDetours(list, fallback && fallback.summary);
    }

    function resetMapView() {
      detourFocusMode = false;
      if (detourLayerGroup) detourLayerGroup.clearLayers();
      renderedMapData = null;
      renderMapMarkers();
      if (map) { map.closePopup(); map.fitBounds(SWISS_BOUNDS); }
      updateUnmappedNote();
    }

    function toggleMapIncidentOverlay(show) {
      document.getElementById('map-selected-overlay').classList.toggle('hidden', !show);
      document.getElementById('map-overlay-show').classList.toggle('hidden', show);
    }

    // =========================================================================
    // 4. DASHBOARD (GET /api/incidents)
    // =========================================================================
    function dashboardQuery() {
      const p = new URLSearchParams({ lang: apiLang(), mode: state.dataMode, status: state.statusFilter });
      if (state.searchQuery.trim()) p.set('search', state.searchQuery.trim());
      if (state.dataMode === 'dataset' && state.dateFilter) p.set('date', state.dateFilter);
      return p.toString();
    }

    async function loadDashboard(selectFirst = false) {
      try {
        const [data, geo] = await Promise.all([api(`/incidents?${dashboardQuery()}`), api(`/map?${dashboardQuery()}`).catch(e => { console.warn('[SBB] map', e); return null; })]);
        const sig = geo ? JSON.stringify([(geo.features || []).map(f => [f.properties.kind, f.properties.id, f.properties.status, f.properties.version]), geo.meta && geo.meta.unmappedCount]) : '';
        if (sig !== mapSig || !mapData) { mapData = geo; mapSig = sig; }   // unchanged data keeps the layers (and any open popup) untouched
        state.incidents = data.incidents;
        state.kpis = data.kpis;
        state.totalCount = data.totalCount;
        if (state.dataMode === 'dataset') renderDateOptions(data.availableDates || []);
        const visibleIncidents = getVisibleIncidents();
        if (selectFirst || !visibleIncidents.some(i => i.id === state.selectedIncidentId)) {
          state.selectedIncidentId = visibleIncidents.length ? visibleIncidents[0].id : null;
        }
        renderDashboard();
        updateOverlay();
        updateUnmappedNote();
        loadIncidentMap(state.selectedIncidentId);
        return data;
      } catch (e) {
        console.error('[SBB] dashboard', e);
        document.getElementById('incidents-list').innerHTML =
          `<div class="bg-white rounded-2xl p-6 border border-slate-200 text-center text-sm text-slate-600">${t('apiError')}</div>`;
      }
    }

    function updateKpiCounters() {
      const k = state.kpis || { active: 0, replacementBus: 0, uncertainForecast: 0, resumed: 0 };
      document.getElementById('kpi-active').textContent = k.active;
      document.getElementById('kpi-bus').textContent = k.replacementBus;
      document.getElementById('kpi-unknown').textContent = k.uncertainForecast;
      document.getElementById('kpi-resumed').textContent = k.resumed;
      document.querySelectorAll('[data-kpi]').forEach(button => {
        const key = button.dataset.kpi;
        const active = key === 'UNCERTAIN' ? state.quickFilter === key : !state.quickFilter && state.statusFilter === key;
        button.setAttribute('aria-pressed', String(active));
      });
    }

    function getVisibleIncidents() {
      if (state.quickFilter === 'UNCERTAIN') return state.incidents.filter(inc => !!inc.forecastWarning);
      return state.incidents;
    }

    function getVisibleIncidentIds() {
      return new Set(getVisibleIncidents().map(inc => inc.id));
    }

    function sourceChips(sources) {
      return (sources || []).map(s => `<span title="${escapeHtml(t('source' + s))}" class="source-chip bg-slate-100 border border-slate-200 text-slate-500">${s === 'INTERNAL' ? 'KULT' : s}</span>`).join('');
    }

    function renderDashboard() {
      updateKpiCounters();
      const listContainer = document.getElementById('incidents-list');
      const visibleIncidents = getVisibleIncidents();
      document.getElementById('visible-count').textContent = visibleIncidents.length;
      const allChip = document.querySelector('[data-filter="ALL"]');
      if (allChip) allChip.textContent = `${t('filterAll')} (${state.totalCount})`;

      if (visibleIncidents.length === 0) {
        listContainer.innerHTML = `
          <div class="bg-white rounded-2xl p-8 border border-slate-200 text-center space-y-2">
            <p class="text-sm font-bold text-slate-800">${t('emptyTitle')}</p>
            <p class="text-xs text-slate-500">${t('emptySub')}</p>
            <button onclick="resetAllFilters()" class="mt-2 px-4 py-2 rounded-xl bg-slate-900 text-white text-xs font-semibold">${t('emptyResetBtn')}</button>
          </div>`;
        renderMapMarkers();
        return;
      }

      listContainer.innerHTML = visibleIncidents.map(inc => {
        const isSelected = inc.id === state.selectedIncidentId;
        const st = statusStyles(inc.status);
        const cardBorder = isSelected ? 'incident-card--selected border-[#EB0000] bg-white' : 'border-slate-200 hover:border-slate-300 bg-white';
        const linesHtml = inc.affectedLines.slice(0, 10)
          .map(line => `<span class="line-chip px-2.5 py-1 rounded-md bg-slate-100 border border-slate-200 text-xs font-bold text-slate-800">${escapeHtml(line)}</span>`).join('');
        return `
          <article data-incident-id="${inc.id}" tabindex="0" role="group" aria-current="${isSelected ? 'true' : 'false'}" aria-keyshortcuts="Enter Space" aria-label="${escapeHtml(inc.statusLabel + '. ' + inc.title + '. ' + inc.reason + '. ' + (ACCESSIBILITY_LABELS[state.lang] || ACCESSIBILITY_LABELS.EN)[6])}" onkeydown="if(event.target===event.currentTarget&&(event.key==='Enter'||event.key===' ')){event.preventDefault();selectIncident(${inc.id},true)}" onclick="selectIncident(${inc.id}, true)" class="incident-card rounded-2xl p-4 border transition-all cursor-pointer shadow-sm ${cardBorder}">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="status-badge px-2.5 py-1 rounded-full text-xs font-bold border ${st.badge}">${escapeHtml(inc.statusLabel)}</span>
                <span class="text-xs font-semibold text-slate-500">${escapeHtml(t('cantonLabel'))} ${escapeHtml(inc.canton || 'CH')}</span>
                ${inc.hasReplacementBus ? `<span class="service-badge service-badge--bus px-2.5 py-1 rounded-full text-xs font-bold border">🚌 ${escapeHtml(t('replacementBusBadge'))}</span>` : ''}
              </div>
              <span class="text-xs font-medium text-slate-500">${escapeHtml(inc.updatedAgo)}</span>
            </div>
            <div class="mt-3">
              <h3 class="text-lg font-extrabold text-slate-900 leading-snug">${escapeHtml(inc.title)}</h3>
              <p class="text-sm text-slate-600 mt-1">${escapeHtml(inc.reason)}</p>
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-1.5">${linesHtml}</div>
            <div class="forecast-line mt-3 flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-700 ${inc.forecastWarning ? 'forecast-line--uncertain' : ''}">
              <svg class="w-4 h-4 text-slate-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              <span>${escapeHtml(inc.forecastLabel)}</span>
              ${inc.forecastWarning ? `<span class="forecast-uncertain-label">${escapeHtml(t('forecastUncertain'))}</span>` : ''}
            </div>
            <div class="mt-3 pt-3 border-t border-slate-100 flex flex-wrap items-center justify-between gap-2">
              <div class="flex items-center gap-2 text-[11px] text-slate-500">
                <span>v${inc.currentVersion}</span><span aria-hidden="true">·</span><span class="flex items-center gap-1">${sourceChips(inc.sources)}</span>
              </div>
              <button onclick="event.stopPropagation(); openAssistantDrawer(${inc.id})" class="incident-action min-h-11 px-4 py-2 rounded-xl text-sm font-bold transition flex items-center gap-1.5 ${isSelected ? 'incident-action--primary' : ''}">
                <span>${t('cardActionBtn')}</span><span>→</span>
              </button>
            </div>
          </article>`;
      }).join('');
      renderMapMarkers();
    }

    function currentIncident() {
      return state.incidents.find(i => i.id === state.selectedIncidentId) || (state.detail && state.detail.id === state.selectedIncidentId ? state.detail : null);
    }

    function officialTimetableUrl(inc) {
      const url = new URL(t('sbbUrlBase'));
      const stations = inc && inc.mapGeometry && inc.mapGeometry.stations || [];
      const names = stations.map(station => typeof station === 'string' ? station : station && station.name).filter(Boolean);
      if (names.length >= 2) {
        url.searchParams.set('von', names[0]);
        url.searchParams.set('nach', names[names.length - 1]);
        const when = inc && inc.updatedAt ? new Date(inc.updatedAt) : null;   // same moment the cards show (live: now, dataset: its own day)
        if (when && !isNaN(when)) {
          const f = new Intl.DateTimeFormat('de-CH', { timeZone: 'Europe/Zurich', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
          const p = Object.fromEntries(f.formatToParts(when).map(x => [x.type, x.value]));
          url.searchParams.set('datum', `${p.day}.${p.month}.${p.year}`);
          url.searchParams.set('zeit', `${p.hour}:${p.minute}`);
        }
      }
      return url.toString();
    }

    function updateOfficialLinks(inc) {
      const ttLink = document.getElementById('sbb-timetable-link');
      ttLink.textContent = t('sbbTimetableLink');
      ttLink.href = officialTimetableUrl(inc);
      const trafficLink = document.getElementById('sbb-traffic-link');
      trafficLink.textContent = t('sbbTrafficLink');
      trafficLink.href = t('sbbUrlTraffic');
      const refundLink = document.getElementById('sbb-refund-link');
      refundLink.textContent = t('sbbRefundLink');
      refundLink.href = t('sbbUrlRefund');
    }

    function updateOverlay() {
      const inc = currentIncident();
      updateOfficialLinks(inc);
      const badge = document.getElementById('overlay-badge');
      if (!inc) {
        badge.textContent = '—';
        document.getElementById('overlay-title').textContent = t('emptyTitle');
        document.getElementById('overlay-lines').textContent = '';
        document.getElementById('overlay-forecast').textContent = '';
        return;
      }
      badge.textContent = inc.statusLabel;
      badge.className = `px-2 py-0.5 rounded text-[10px] font-bold uppercase ${statusStyles(inc.status).badge}`;
      document.getElementById('overlay-lines').textContent = inc.affectedLines.slice(0, 8).join(', ');
      document.getElementById('overlay-title').textContent = inc.title;
      document.getElementById('overlay-forecast').textContent = `${inc.forecastLabel} • ${inc.updatedAgo}`;
    }

    function selectIncident(id, focusMap = true) {
      if (id !== state.selectedIncidentId && detourFocusMode) {
        detourFocusMode = false;
        if (detourLayerGroup) detourLayerGroup.clearLayers();
        renderedMapData = null;
      }
      state.selectedIncidentId = id;
      renderDashboard();
      updateOverlay();
      loadIncidentMap(id);
      if (focusMap && map) {
        if (detourLayerGroup) detourLayerGroup.clearLayers();
        fitIncident(id);
      }
    }

    // =========================================================================
    // 5. FILTERS, DATA MODE, LANGUAGE, SIMULATION
    // =========================================================================
    function applyStaticTranslations() {
      document.documentElement.lang = state.lang.toLowerCase();
      applyAccessibilityLabels();
      const set = (id, key) => { const el = document.getElementById(id); if (el) el.textContent = t(key); };
      set('header-tagline', 'tagline'); set('mode-live-label', 'liveMode'); updateDatasetLabel();
      if (window.applyPanelLayout) applyPanelLayout();   // refresh the panel button/handle labels in the new language
      set('sim-update-label', 'simUpdate'); set('toast-badge', 'toastBadge'); set('live-toast-action', 'toastAction');
      set('nav-disruptions', 'serviceNavDisruptions'); set('nav-map', 'serviceNavMap');
      set('kpi-active-title', 'kpiActiveTitle'); set('kpi-active-sub', 'kpiActiveSub'); set('kpi-bus-title', 'kpiBusTitle');
      set('kpi-bus-sub', 'kpiBusSub'); set('kpi-unknown-title', 'kpiUnknownTitle'); set('kpi-unknown-sub', 'kpiUnknownSub');
      set('kpi-resumed-title', 'kpiResumedTitle'); set('kpi-resumed-sub', 'kpiResumedSub');
      document.getElementById('search-input').placeholder = t('searchPlaceholder');
      document.getElementById('search-label').textContent = t('searchPlaceholder');
      set('search-help', 'searchHelp');
      document.getElementById('chat-custom-label').textContent = t('chatPlaceholder');
      document.querySelector('[data-filter="ACTIVE"]').textContent = t('filterActive');
      document.querySelector('[data-filter="RESTRICTED"]').textContent = t('filterRestricted');
      document.querySelector('[data-filter="BUS"]').textContent = t('filterBus');
      document.querySelector('[data-filter="RESUMED"]').textContent = t('filterResumed');
      const visibleCount = document.getElementById('visible-count').textContent;
      const heading = state.dataMode === 'dataset' ? t('listHeadingDataset') : t('listHeading');
      document.getElementById('list-heading').innerHTML = `${escapeHtml(heading)} (<span id="visible-count">${visibleCount}</span>)`;
      set('list-hint', 'listHint'); set('map-heading', 'mapHeading'); set('legend-interrupted', 'legendInterrupted');
      set('legend-restricted', 'legendRestricted'); set('legend-detour', 'legendDetour'); set('legend-bus', 'legendBus'); set('reset-map-btn', 'resetMapBtn');
      set('overlay-btn-text', 'overlayBtnText'); set('map-overlay-show', 'showSelectedIncident');
      document.getElementById('map-overlay-show').setAttribute('aria-label', t('showSelectedIncident'));
      const hideSelected = document.getElementById('map-overlay-hide');
      hideSelected.setAttribute('aria-label', t('hideSelectedIncident'));
      hideSelected.title = t('hideSelectedIncident');
      set('sbb-banner-title', 'sbbBannerTitle'); set('sbb-banner-sub', 'sbbBannerSub');
      updateOfficialLinks(currentIncident());
      set('audit-heading', 'auditHeading');
      document.getElementById('audit-toggle-label').textContent = state.auditExpanded ? t('collapse') : t('expand');
      set('context-heading', 'contextHeading');
      document.getElementById('context-toggle-label').textContent = state.contextExpanded ? t('collapse') : t('expand');
      set('scenarios-heading', 'scenariosHeading');
      set('sc-missed-title', 'scMissedTitle'); set('sc-missed-desc', 'scMissedDesc'); set('sc-detour-title', 'scDetourTitle');
      set('sc-detour-desc', 'scDetourDesc'); set('sc-refund-title', 'scRefundTitle'); set('sc-refund-desc', 'scRefundDesc');
      set('sc-status-title', 'scStatusTitle'); set('sc-status-desc', 'scStatusDesc'); set('chat-heading', 'chatHeading');
      set('inchat-update-btn', 'inChatUpdateBtn');
      document.getElementById('chat-custom-input').placeholder = t('chatPlaceholder');
      set('chat-send-btn', 'chatSendBtn');
      const allDays = document.querySelector('#date-select option[value=""]');
      if (allDays) allDays.textContent = t('allDays');
    }

    function setStatusFilter(filter) {
      state.quickFilter = null;
      state.statusFilter = filter;
      document.querySelectorAll('.filter-chip').forEach(btn => {
        const active = btn.dataset.filter === filter;
        btn.setAttribute('aria-pressed', String(active));
        btn.className = active
          ? 'filter-chip min-h-11 px-3 py-2 rounded-xl text-xs font-semibold bg-slate-900 text-white transition'
          : 'filter-chip min-h-11 px-3 py-2 rounded-xl text-xs font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200 transition';
      });
      renderedMapData = null;
      loadDashboard();
    }

    function setQuickFilter(filter) {
      if (filter === 'UNCERTAIN') {
        state.statusFilter = 'ALL';
        state.quickFilter = state.quickFilter === filter ? null : filter;
        document.querySelectorAll('.filter-chip').forEach(btn => {
          const active = btn.dataset.filter === 'ALL';
          btn.setAttribute('aria-pressed', String(active));
          btn.className = active
            ? 'filter-chip min-h-11 px-3 py-2 rounded-xl text-xs font-semibold bg-slate-900 text-white transition'
            : 'filter-chip min-h-11 px-3 py-2 rounded-xl text-xs font-semibold bg-slate-100 text-slate-700 hover:bg-slate-200 transition';
        });
        renderedMapData = null;
        loadDashboard(true);
        return;
      }
      setStatusFilter(state.statusFilter === filter && !state.quickFilter ? 'ALL' : filter);
    }

    function handleFilterChange() {
      const val = document.getElementById('search-input').value;
      state.searchQuery = val;
      document.getElementById('clear-search-btn').classList.toggle('hidden', val.length === 0);
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadDashboard(), 250);
    }

    function clearSearch() {
      document.getElementById('search-input').value = '';
      handleFilterChange();
    }

    function resetAllFilters() {
      document.getElementById('search-input').value = '';
      state.searchQuery = '';
      document.getElementById('clear-search-btn').classList.add('hidden');
      state.dateFilter = '';
      const ds = document.getElementById('date-select'); if (ds) ds.value = '';
      setStatusFilter('ALL');
    }

    // "Dataset: 01.–03.09.2026" built from the days that really are in the data, instead of a fixed text.
    function datasetRangeText(dates) {
      if (!dates || !dates.length) return '';
      const fmt = d => d.split('-').reverse().join('.');
      const first = dates[0], last = dates[dates.length - 1];
      if (first === last) return fmt(first);
      const [fy, fm, fd] = first.split('-'), [ly, lm, ld] = last.split('-');
      return fy === ly && fm === lm ? `${fd}.–${ld}.${lm}.${ly}` : `${fmt(first)} – ${fmt(last)}`;
    }
    function updateDatasetLabel() {
      const el = document.getElementById('mode-demo-label');
      if (!el) return;
      const r = datasetRangeText(state.datasetDates);
      el.textContent = r ? t('demoModeRange').replace('{range}', r) : t('demoMode');
    }
    async function loadDatasetDates() {
      try { state.datasetDates = (await api(`/incidents?mode=dataset&limit=1&lang=${apiLang()}`)).availableDates || []; updateDatasetLabel(); }
      catch (e) { console.warn('[SBB] dataset dates', e); }
    }

    function renderDateOptions(dates) {
      state.datasetDates = dates;
      updateDatasetLabel();
      const sel = document.getElementById('date-select');
      const current = state.dateFilter;
      sel.innerHTML = `<option value="">${escapeHtml(t('allDays'))}</option>` +
        dates.map(d => `<option value="${d}" ${d === current ? 'selected' : ''}>${d.split('-').reverse().join('.')}</option>`).join('');
    }

    function setDateFilter(value) {
      state.dateFilter = value;
      loadDashboard(true);
    }

    function setDataMode(mode) {
      state.dataMode = mode === 'demo' ? 'dataset' : mode;
      const liveBtn = document.getElementById('mode-live-btn');
      const demoBtn = document.getElementById('mode-demo-btn');
      if (state.dataMode === 'live') {
        liveBtn.className = 'min-h-10 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all bg-white text-slate-900 shadow-sm flex items-center gap-1.5';
        demoBtn.className = 'min-h-10 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all text-slate-600 hover:text-slate-900';
      } else {
        demoBtn.className = 'min-h-10 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all bg-white text-slate-900 shadow-sm';
        liveBtn.className = 'min-h-10 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all text-slate-600 hover:text-slate-900 flex items-center gap-1.5';
      }
      document.getElementById('date-select').classList.toggle('hidden', state.dataMode !== 'dataset');
      applyStaticTranslations();
      resetMapView();
      loadDashboard(true);
    }

    async function switchLang(lang) {
      state.lang = lang;
      document.querySelectorAll('.lang-btn').forEach(btn => {
        btn.className = btn.dataset.lang === lang ? 'lang-btn min-h-10 min-w-10 px-2 py-1 rounded-lg bg-slate-900 text-white' : 'lang-btn min-h-10 min-w-10 px-2 py-1 rounded-lg text-slate-600 hover:text-slate-900';
      });
      applyStaticTranslations();
      await loadDashboard();
      if (state.drawerOpen) await refreshDrawer(true);
    }

    function pickSimulationTarget() {
      const sel = currentIncident();
      if (sel && sel.status !== 'RESUMED') return sel.id;
      const queued = state.incidents.find(i => i.hasPendingUpdates && i.status !== 'RESUMED');
      const open = state.incidents.find(i => i.status !== 'RESUMED');
      return (queued || open || sel || {}).id;
    }

    async function runSimulation(incidentId) {
      if (!incidentId) return null;
      const res = await api(`/incidents/${incidentId}/simulate-update?lang=${apiLang()}`, { method: 'POST' });
      showToast(res.incident, res.applied ? res.changeSummary : t('noUpdates'));
      await loadDashboard();
      return res;
    }

    async function simulateLiveUpdate() {
      try {
        const id = pickSimulationTarget();
        const res = await runSimulation(id);
        if (res) selectIncident(res.incident.id, true);
        if (state.drawerOpen && res && res.incident.id === state.selectedIncidentId) await refreshDrawer(false);
      } catch (e) { console.error('[SBB] simulate', e); }
    }

    function showToast(inc, text) {
      const banner = document.getElementById('live-toast-banner');
      document.getElementById('live-toast-text').textContent = `${inc.title} (v${inc.currentVersion}): ${text}`;
      document.getElementById('live-toast-action').onclick = () => { hideToastBanner(); openAssistantDrawer(inc.id); };
      banner.classList.remove('hidden');
    }

    function hideToastBanner() { document.getElementById('live-toast-banner').classList.add('hidden'); }

    async function pollLive() {
      if (document.hidden) return;
      const before = new Map(state.incidents.map(i => [i.id, i.currentVersion]));
      const data = await loadDashboard();
      if (!data) return;
      const changed = data.incidents.find(i => before.has(i.id) && i.currentVersion > before.get(i.id));
      if (changed) showToast(changed, changed.forecastLabel);
      if (state.drawerOpen && state.sessionId) await syncSession();
    }

    // =========================================================================
    // 6. DECISION ASSISTANT DRAWER (sessions, scenarios, chat)
    // =========================================================================
    function openAssistantForSelected() { if (state.selectedIncidentId) openAssistantDrawer(state.selectedIncidentId); }

    async function openAssistantDrawer(incidentId) {
      const switching = state.selectedIncidentId !== incidentId || !state.drawerOpen;
      selectIncident(incidentId, false);
      document.getElementById('assistant-backdrop').classList.remove('hidden');
      document.getElementById('assistant-drawer').classList.remove('translate-x-full');
      state.drawerOpen = true;
      if (switching) {
        state.sessionId = null;
        state.renderedMessageIds = new Set();
        document.getElementById('chat-feed').innerHTML = `<p class="text-xs text-slate-400">${t('loading')}</p>`;
      }
      await refreshDrawer(true);
    }

    function closeAssistantDrawer() {
      document.getElementById('assistant-drawer').classList.add('translate-x-full');
      document.getElementById('assistant-backdrop').classList.add('hidden');
      state.drawerOpen = false;
    }

    async function loadDetail() {
      state.detail = await api(`/incidents/${state.selectedIncidentId}?lang=${apiLang()}`);
      return state.detail;
    }

    async function refreshDrawer(rerenderChat) {
      try {
        const inc = await loadDetail();
        renderDrawerHeader(inc);
        renderAuditTimeline(inc);
        renderContextBlock(inc);
        await ensureSession(inc, rerenderChat);
      } catch (e) {
        console.error('[SBB] drawer', e);
        document.getElementById('chat-feed').innerHTML = `<p class="text-xs text-red-600">${t('apiError')}</p>`;
      }
    }

    function renderDrawerHeader(inc) {
      const badge = document.getElementById('drawer-status-badge');
      badge.textContent = inc.statusLabel;
      badge.className = `px-2.5 py-0.5 rounded-full text-[11px] font-bold border ${statusStyles(inc.status).badge}`;
      document.getElementById('drawer-incident-id').textContent = `ID #${inc.externalId} · v${inc.currentVersion}`;
      document.getElementById('drawer-updated-tag').textContent = `• ${inc.updatedAgo}`;
      document.getElementById('drawer-sources').innerHTML = sourceChips(inc.sources);
      document.getElementById('drawer-title').textContent = inc.title;
      document.getElementById('drawer-subtitle').textContent =
        `${t('linesLabel')}: ${inc.affectedLines.slice(0, 10).join(', ') || '—'} • ${inc.forecastLabel}`;
      if (!state.selectedRouteChip || !inc.quickRoutes.includes(state.selectedRouteChip)) state.selectedRouteChip = inc.quickRoutes[0] || null;
    }

    function renderAuditTimeline(inc) {
      const container = document.getElementById('audit-timeline-list');
      const items = inc.auditTimeline;
      container.innerHTML = items.map((item, idx) => `
        <div class="relative text-xs">
          <div class="flex items-center gap-2">
            <span class="font-mono font-bold text-slate-900">${escapeHtml(item.time)}</span>
            <span class="px-1.5 py-0.2 rounded bg-slate-200 text-slate-700 text-[10px] font-bold">${escapeHtml(item.label)}</span>
            <span class="text-[10px] text-slate-400">${escapeHtml(item.source)}</span>
            ${idx === items.length - 1 ? `<span class="text-[10px] font-bold text-emerald-700">${t('actualBadge')}</span>` : ''}
          </div>
          <p class="text-slate-600 mt-0.5 leading-snug">${escapeHtml(item.text)}</p>
        </div>`).join('') +
        (inc.hasPendingUpdates ? `<p class="text-[10px] text-amber-700 font-semibold">⏳ ${t('pending')}</p>` : '');
      container.classList.toggle('hidden', !state.auditExpanded);
    }

    function toggleAuditTimeline() {
      state.auditExpanded = !state.auditExpanded;
      document.getElementById('audit-timeline-list').classList.toggle('hidden', !state.auditExpanded);
      document.getElementById('audit-toggle-label').textContent = state.auditExpanded ? t('collapse') : t('expand');
    }

    function renderContextBlock(inc) {
      const body = document.getElementById('context-body');
      const msgs = inc.messages.slice(-5).reverse();
      const ops = inc.operationalSummary;
      const corr = (ops.correlation || []).map(c => `<li>${escapeHtml(c.method)} — <strong>${escapeHtml(c.confidence)}</strong>${c.time_delta_min != null ? ` (Δt ${c.time_delta_min} min${c.distance_km != null ? `, ${c.distance_km} km` : ''})` : ''}</li>`).join('');
      body.innerHTML = `
        <div>
          <p class="font-bold text-slate-800 mb-1">${t('officialMessages')}</p>
          ${msgs.length ? msgs.map(m => `<div class="border-l-2 border-[#EB0000] pl-2 mb-2"><p class="font-semibold text-slate-900">${escapeHtml(m.time)} · ${escapeHtml(m.title)}</p><p class="text-slate-600">${richText(m.body)}</p></div>`).join('') : `<p class="text-slate-400">${t('noMessages')}</p>`}
        </div>
        <div>
          <p class="font-bold text-slate-800 mb-1">${t('opsContext')} (${ops.totalEntries})</p>
          ${ops.latest.map(l => `<p class="text-slate-600 mb-1"><span class="font-mono text-slate-400">${escapeHtml(l.time)}</span> ${escapeHtml(l.text)}</p>`).join('') || '<p class="text-slate-400">—</p>'}
          <p class="text-[10px] text-slate-400 mt-1">${escapeHtml(ops.note)}</p>
        </div>
        ${corr ? `<div><p class="font-bold text-slate-800 mb-1">${t('correlation')}</p><ul class="list-disc ml-4 text-slate-600">${corr}</ul></div>` : ''}`;
      body.classList.toggle('hidden', !state.contextExpanded);
    }

    function toggleContextBlock() {
      state.contextExpanded = !state.contextExpanded;
      document.getElementById('context-body').classList.toggle('hidden', !state.contextExpanded);
      document.getElementById('context-toggle-label').textContent = state.contextExpanded ? t('collapse') : t('expand');
    }

    const sessionKey = id => `sbb_session_${id}`;

    async function ensureSession(inc, rerenderChat) {
      let session = null;
      const stored = (() => { try { return sessionStorage.getItem(sessionKey(inc.id)); } catch (_) { return null; } })();
      if (stored) {
        try { session = await api(`/sessions/${stored}?lang=${apiLang()}`); }
        catch (e) { if (e.status !== 404 && e.status !== 410) throw e; }
      }
      if (!session) {
        session = await api('/sessions', { method: 'POST', body: { incidentId: inc.id, lang: apiLang() } });
        try { sessionStorage.setItem(sessionKey(inc.id), session.sessionId); } catch (_) {}
      }
      state.sessionId = session.sessionId;
      if (rerenderChat) {
        state.renderedMessageIds = new Set();
        renderChatIntro(inc);
      }
      session.messages.forEach(renderMessage);
      if (session.newAlerts && session.newAlerts.length) await onIncidentChanged(false);
    }

    async function syncSession() {
      const session = await api(`/sessions/${state.sessionId}?lang=${apiLang()}`);
      session.messages.forEach(renderMessage);
      if (session.newAlerts && session.newAlerts.length) await onIncidentChanged(false);
    }

    async function onIncidentChanged(alsoSession) {
      const inc = await loadDetail();
      renderDrawerHeader(inc);
      renderAuditTimeline(inc);
      renderContextBlock(inc);
      if (alsoSession) await syncSession();
    }

    function renderChatIntro(inc) {
      const chips = inc.quickRoutes.map(route => `
        <button data-route="${escapeHtml(route)}" onclick="selectRouteInChat(this.dataset.route)" class="route-chip px-3 py-1.5 rounded-xl text-xs font-semibold border transition ${route === state.selectedRouteChip ? 'bg-slate-900 text-white border-slate-900' : 'bg-white text-slate-700 border-slate-200 hover:border-slate-400'}">🚆 ${escapeHtml(route)}</button>`).join('');
      document.getElementById('chat-feed').innerHTML = `
        <div class="bg-slate-50 rounded-2xl p-4 border border-slate-200 space-y-3">
          <div class="flex items-start gap-2.5">
            <div class="w-7 h-7 rounded-lg bg-[#EB0000] text-white flex items-center justify-center text-xs font-extrabold shrink-0">SBB</div>
            <div class="space-y-1.5 text-xs text-slate-700 leading-relaxed">
              <p class="font-bold text-slate-900">${t('quickAssessTitle')}</p>
              <p>${richText(inc.recommendedAction)}</p>
              ${inc.forecastWarning ? `<p class="inline-block px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-800 font-semibold">⚠️ ${escapeHtml(inc.forecastLabel)}</p>` : ''}
            </div>
          </div>
          ${chips ? `<div class="pt-2 border-t border-slate-200/80"><p class="text-[11px] font-bold text-slate-500 mb-2">${t('clarifyDirection')}</p><div class="flex flex-wrap gap-1.5">${chips}</div></div>` : ''}
        </div>`;
    }

    function widgetButtons(widgets, big) {
      const pad = big ? 'px-3 py-2 text-xs' : 'px-3 py-1.5 text-[11px]';
      return widgets.map(w => {
        if (w.type === 'detour_map' && validPoints(w.route).length >= 2) {
          const payload = encodeURIComponent(JSON.stringify({ route: w.route, summary: w.summary }));
          return `<button data-detour="${payload}" onclick="triggerDetourFromChat(this.dataset.detour)" class="${pad} rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold transition shadow-xs">${escapeHtml(w.label)}</button>`;
        }
        if (w.type === 'sbb_link') {
          return `<a href="${encodeURI(w.url)}" target="_blank" rel="noopener noreferrer" class="${pad} rounded-xl ${big ? 'bg-slate-900 hover:bg-slate-800 text-white' : 'bg-white hover:bg-slate-100 text-slate-800 border border-slate-300'} font-bold transition">${escapeHtml(w.label)}</a>`;
        }
        return '';
      }).join('');
    }

    function renderMessage(m) {
      if (state.renderedMessageIds.has(m.id)) return;
      state.renderedMessageIds.add(m.id);
      const chatFeed = document.getElementById('chat-feed');
      const wrapper = buildMessageEl(m);
      wrapper.classList.add('assistant-message');
      chatFeed.appendChild(wrapper);
      scrollChatToBottom();
      const card = (m.widgets || []).find(w => w.type === 'decision_card');
      if (card && card.aiPending) watchMessage(m.id, wrapper, state.sessionId);
    }

    // The rule engine answers instantly; Gemini refines the same card in the background. Poll until it is done.
    async function watchMessage(mid, el, sid) {
      const started = Date.now();
      while (Date.now() - started < 75000) {
        await new Promise(r => setTimeout(r, 1500));
        if (!el.isConnected || state.sessionId !== sid) return;
        try {
          const m = await api(`/sessions/${sid}/messages/${mid}`);
          const card = (m.widgets || []).find(w => w.type === 'decision_card');
          if (card && !card.aiPending) {
            const fresh = buildMessageEl(m);
            fresh.classList.add('ai-refined');
            el.replaceWith(fresh);
            return;
          }
        } catch (e) { console.warn('[SBB] poll message', e); return; }
      }
      const lbl = el.querySelector('.ai-status');
      if (lbl) { lbl.classList.remove('animate-pulse', 'text-amber-600'); lbl.textContent = t('engineDet'); }
    }

    function buildMessageEl(m) {
      const wrapper = document.createElement('div');
      const widgets = m.widgets || [];
      if (m.sender === 'USER') {
        wrapper.className = 'flex justify-end';
        wrapper.innerHTML = `<div class="max-w-[85%] rounded-2xl rounded-tr-xs px-3.5 py-2.5 bg-slate-900 text-white text-xs leading-relaxed shadow-xs">${escapeHtml(m.content)}</div>`;
      } else if (m.sender === 'SYSTEM_ALERT') {
        const lines = m.content.split('\n');
        wrapper.className = 'rounded-2xl p-3.5 bg-amber-50 border border-amber-300 text-xs text-amber-950 space-y-1.5';
        wrapper.innerHTML = `
          <div class="flex items-center gap-1.5 font-extrabold text-amber-900"><span>${escapeHtml(lines[0])} (SYSTEM_ALERT)</span></div>
          ${lines.slice(1, -1).map(l => `<p class="leading-relaxed">${escapeHtml(l)}</p>`).join('')}
          <p class="text-[11px] text-amber-800">${escapeHtml(lines[lines.length - 1])}</p>`;
      } else {
        const card = widgets.find(w => w.type === 'decision_card');
        const warn = widgets.find(w => w.type === 'forecast_warning');
        wrapper.className = 'flex items-start gap-2.5';
        const stepsHtml = card ? card.steps.map((step, i) => `
            <div class="flex items-start gap-2">
              <span class="w-4 h-4 rounded-full bg-red-100 text-[#EB0000] font-bold text-[10px] flex items-center justify-center shrink-0 mt-0.5">${i + 1}</span>
              <p class="text-xs text-slate-700 leading-relaxed">${richText(step)}</p>
            </div>`).join('') : '';
        wrapper.innerHTML = `
          <div class="w-7 h-7 rounded-lg bg-[#EB0000] text-white flex items-center justify-center text-xs font-extrabold shrink-0">SBB</div>
          <div class="flex-1 rounded-2xl rounded-tl-xs p-4 bg-white border border-slate-200 shadow-sm space-y-3">
            <p class="text-xs text-slate-700">${richText(m.content)}</p>
            ${card ? `
              <div class="flex items-center justify-between gap-2 border-b border-slate-100 pb-2">
                <h4 class="text-xs font-extrabold text-slate-900">${escapeHtml(card.title)}</h4>
                <span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-slate-100 text-slate-600">${escapeHtml(card.canton || '')}</span>
              </div>
              ${warn ? `<p class="inline-block px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-800 text-[11px] font-bold">⚠️ ${escapeHtml(warn.text)}</p>` : ''}
              <div class="space-y-2">${stepsHtml}</div>` : ''}
            <div class="pt-2 border-t border-slate-100 flex flex-wrap items-center gap-2">
              ${widgetButtons(widgets, true)}
              ${card ? (card.aiPending
                ? `<span class="ai-status ml-auto text-[10px] text-amber-600 animate-pulse">${t('aiRefining')}</span>`
                : `<span class="ml-auto text-[10px] text-slate-400">${card.engine === 'gemini' ? t('engineGemini') : t('engineDet')}</span>`) : ''}
            </div>
          </div>`;
      }
      return wrapper;
    }

    function showThinking() {
      const el = document.createElement('p');
      el.id = 'thinking-indicator';
      el.className = 'text-xs text-slate-400 animate-pulse';
      el.textContent = t('thinking');
      document.getElementById('chat-feed').appendChild(el);
      scrollChatToBottom();
    }
    function hideThinking() { const el = document.getElementById('thinking-indicator'); if (el) el.remove(); }

    async function withSession(fn) {
      if (state.busy) return;
      state.busy = true;
      showThinking();
      try {
        if (!state.sessionId) await refreshDrawer(true);
        let res;
        try { res = await fn(); }
        catch (e) {
          if (e.status === 404 || e.status === 410) {
            try { sessionStorage.removeItem(sessionKey(state.selectedIncidentId)); } catch (_) {}
            await refreshDrawer(true);
            res = await fn();
          } else throw e;
        }
        hideThinking();
        (res.newAlerts || []).forEach(renderMessage);
        res.messages.forEach(renderMessage);
        if (res.newAlerts && res.newAlerts.length) await onIncidentChanged(false);
      } catch (e) {
        hideThinking();
        console.error('[SBB] assistant', e);
        const err = document.createElement('p');
        err.className = 'text-xs text-red-600';
        err.textContent = t('apiError');
        document.getElementById('chat-feed').appendChild(err);
      } finally {
        state.busy = false;
      }
    }

    function selectRouteInChat(route) {
      state.selectedRouteChip = route;
      document.querySelectorAll('.route-chip').forEach(btn => {
        btn.className = btn.dataset.route === route
          ? 'route-chip px-3 py-1.5 rounded-xl text-xs font-semibold border transition bg-slate-900 text-white border-slate-900'
          : 'route-chip px-3 py-1.5 rounded-xl text-xs font-semibold border transition bg-white text-slate-700 border-slate-200 hover:border-slate-400';
      });
      withSession(() => api(`/sessions/${state.sessionId}/messages`, {
        method: 'POST', body: { content: t('myRoute') + route, selectedRoute: route, lang: apiLang() }
      }));
    }

    function selectScenario(scenarioSlug) {
      document.querySelectorAll('.scenario-card').forEach(card => {
        const on = card.dataset.scenario === scenarioSlug;
        ['border-[#EB0000]', 'bg-red-50/60', 'ring-2', 'ring-[#EB0000]/15'].forEach(c => card.classList.toggle(c, on));
      });
      withSession(() => api(`/sessions/${state.sessionId}/scenario`, {
        method: 'POST', body: { scenarioSlug, selectedRoute: state.selectedRouteChip, lang: apiLang() }
      }));
    }

    function handleCustomChatSubmit(event) {
      event.preventDefault();
      const input = document.getElementById('chat-custom-input');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      withSession(() => api(`/sessions/${state.sessionId}/messages`, {
        method: 'POST', body: { content: text, selectedRoute: state.selectedRouteChip, lang: apiLang() }
      }));
    }

    function triggerDetourFromChat(payload) {
      let data = null;
      try { data = JSON.parse(decodeURIComponent(payload)); } catch (_) {}
      const inc = state.detail;
      if (state.drawerOpen) closeAssistantDrawer();
      showDetourForSelected({ route: data ? data.route : inc && inc.mapGeometry.detourRoute, summary: data ? data.summary : inc && inc.detourSummary });
    }

    async function triggerInChatUpdate() {
      try {
        const res = await runSimulation(state.selectedIncidentId);
        if (res) await onIncidentChanged(true);
      } catch (e) { console.error('[SBB] in-chat update', e); }
    }

    function scrollChatToBottom() {
      const scrollArea = document.getElementById('drawer-scroll-area');
      setTimeout(() => { scrollArea.scrollTop = scrollArea.scrollHeight; }, 50);
    }
