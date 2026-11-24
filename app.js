/* CASE-OPS app. DOM is built only with createElement/append (text nodes), never from markup strings,
   so file names and extracted evidence text cannot inject HTML or script. */
(() => {
  'use strict';

  const Core = window.CaseOpsCore;
  const D = structuredClone(window.CASE_DATA);
  const TZ = D.tz;
  const MAX_BYTES = 100 * 1024 * 1024;
  const MAX_QUEUE = 50;
  const WAVE = [28, 44, 62, 35, 80, 92, 54, 40, 72, 88, 60, 48, 95, 76, 52, 66, 84, 42, 38, 70, 90, 64, 50, 78, 86, 46, 36, 68, 82, 58, 42, 74, 60, 48, 32, 24];
  const SECTIONS = ['intake', 'timeline', 'conflicts', 'deadlines'];

  /* ---------- helpers ---------- */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const h = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) n.setAttribute(k, v === true ? '' : v);
    for (const kid of kids.flat()) if (kid != null) n.append(kid);
    return n;
  };
  const ms = (name) => h('span', { class: 'ms', 'aria-hidden': 'true' }, name);
  const badge = (tone, text) => h('span', { class: 'badge ' + tone }, text);
  const fmtAt = (iso) => Core.formatInZone(iso, TZ);
  const fmtSec = (s) => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  const STATUS = { conflict: ['err', 'Conflict'], verified: ['ok', 'Verified'], pending: ['warn', 'Pending review'] };
  let seq = 0;
  const uid = () => 'u' + ++seq;

  const state = {
    view: 'all', tl: 'all', q: '', zoom: 1, active: null,
    queue: [], running: false, done: false, lastProof: null,
    files: D.files, events: D.events, proofs: D.proofs, ledger: D.ledger,
  };

  const el = {
    sidebar: $('#sidebar'), topbar: $('#topbar'), main: $('#main'), stream: $('#stream'),
    proof: $('#proof'), scrim: $('#scrim'), menuBtn: $('#menu-btn'), proofClose: $('#proof-close'),
    dlg: $('#upload-dlg'), docLines: $('#doc-lines'), wave: $('#wave'),
  };
  const dockedMQ = matchMedia('(min-width: 1280px)');
  const wideMQ = matchMedia('(min-width: 1024px)');

  /* ---------- proof model (single source of truth for quotes and refs) ---------- */
  const resolveLine = (p, line) => (Number.isInteger(line) && line >= 0 && line < p.lines.length ? line : p.hl);
  const lineText = (ref) => state.proofs[ref.proofId].lines[ref.line].t;
  const refOf = (p, i) => `${p.file} • ${p.kind === 'audio' ? fmtSec(p.lines[i].sec) : p.loc}`;
  const proofText = (id) => (state.proofs[id] ? state.proofs[id].lines.map((l) => l.t).join(' ') : '');
  const has = (q, ...parts) => !q || parts.some((s) => String(s).toLowerCase().includes(q));
  const proofLink = (id, line) => ({ 'data-proof': id, 'data-line': String(resolveLine(state.proofs[id], line)) });

  function deadlines() {
    const today = Core.todayIn(TZ);
    return { today, list: D.deadlines.map((d) => ({ ...d, ...Core.computeDeadline(d.rule, d.trigger.date, today) })) };
  }

  /* ---------- render ---------- */
  function renderChrome() {
    $('#case-title').textContent = D.title;
    $('#case-docket').textContent = D.docket;
    $('#audit-docket').textContent = D.docket;
    document.title = `CASE-OPS — ${D.title} • ${D.docket}`;
    $('#intake-count').textContent = `${state.files.length} indexed files`;
    $('#custody-count').textContent = `${state.files.length} files indexed`;
    $('#nav-badge').replaceChildren(String(D.contradictions.length), h('span', { class: 'sr' }, ' open conflicts'));
    $('#conflict-count').textContent = `${D.contradictions.length} active conflicts`;
  }

  function renderMetrics() {
    const mean = Core.mean(Object.values(state.proofs).map((p) => p.conf).filter((c) => typeof c === 'number'));
    $('#m-conf').textContent = mean == null ? '—' : mean.toFixed(1) + '%';
    $('#m-conflicts').textContent = String(D.contradictions.length);
    const next = deadlines().list.filter((d) => d.left >= 0).sort((a, b) => a.left - b.left)[0];
    $('#m-next').textContent = next ? `${next.left}d left` : '—';
    $('#m-next-sub').textContent = next ? `${next.citation} ${next.title}` : 'No open deadlines';
  }

  function renderFiles() {
    const rows = state.files.filter((f) => has(state.q, f.name, f.status));
    $('#files').replaceChildren(...rows.map((f) => h('li', { class: 'file' },
      h('span', {}, h('span', { class: 'mono accent small' }, f.type), h('span', { class: 'name mono', title: f.name }, f.name)),
      badge(f.tone, f.status))));
    return rows.length;
  }

  function renderTimeline() {
    const rows = state.events
      .filter((e) => (state.tl === 'all' || e.status === state.tl) &&
        has(state.q, e.title, e.actors, e.proofId && refOf(state.proofs[e.proofId], resolveLine(state.proofs[e.proofId], undefined)), proofText(e.proofId)))
      .sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
    $('#timeline').replaceChildren(...(rows.length ? rows.map((e) => {
      const p = state.proofs[e.proofId];
      const [tone, label] = STATUS[e.status] || STATUS.pending;
      return h('li', {},
        h('button', { type: 'button', class: 'ev', ...proofLink(e.proofId) },
          h('span', { class: 'ev-top' },
            h('span', {}, h('span', { class: 'mono accent small' }, fmtAt(e.at)), badge(tone, label)),
            h('span', { class: 'mono src' }, refOf(p, resolveLine(p)))),
          h('span', { class: 'etitle' }, e.title),
          h('span', { class: 'muted small' }, 'Actors: ' + e.actors)));
    }) : [h('li', { class: 'empty' }, 'No timeline events match the active filter.')]));
    return rows.length;
  }

  function renderConflicts() {
    const rows = D.contradictions.filter((c) => has(state.q, c.title, c.summary, lineText(c.a), lineText(c.b)));
    const claim = (label, tone, ref) => {
      const p = state.proofs[ref.proofId];
      return h('button', { type: 'button', class: 'claim', ...proofLink(ref.proofId, ref.line) },
        h('span', { class: 'label ' + tone }, label + ' • click to verify'),
        h('span', { class: 'quote' }, '“' + lineText(ref) + '”'),
        h('span', { class: 'mono ref' }, refOf(p, ref.line)));
    };
    $('#conflicts').replaceChildren(...(rows.length ? rows.map((c) => h('article', { class: 'card conflict' },
      h('header', {}, h('h3', {}, c.title), badge('err', 'Conflict')),
      h('div', { class: 'claims' }, claim('Claim A', 'accent', c.a), claim('Claim B', 'err-text', c.b)),
      h('p', { class: 'discrepancy' }, h('strong', {}, 'Discrepancy: '), c.summary))) :
      [h('p', { class: 'empty' }, 'No contradictions match the search.')]));
    return rows.length;
  }

  function renderDeadlines() {
    const { today, list } = deadlines();
    $('#dl-asof').textContent = `As of ${today} (${TZ})`;
    const tone = { Overdue: 'err', Urgent: 'warn', Active: 'info', Upcoming: 'ok' };
    const txt = { err: 'err-text', warn: 'warn-text', info: 'accent', ok: 'ok-text' };
    $('#deadlines').replaceChildren(...list.map((d) => {
      const t = tone[d.urgency];
      return h('article', { class: 'card dl' },
        h('div', { class: 'dl-body' },
          h('div', { class: 'dl-top' },
            h('span', { class: `mono big ${txt[t]}` }, d.left < 0 ? `${-d.left}d overdue` : `${d.left}d left`),
            badge(t, d.urgency)),
          h('h3', {}, d.title),
          h('p', { class: 'mono accent small' }, d.citation),
          h('p', { class: 'mono muted small' }, d.formula)),
        h('footer', {},
          h('span', { class: 'mono muted small' }, `${d.trigger.label} (${d.trigger.date})`),
          h('button', { type: 'button', class: 'link-btn', ...proofLink(d.trigger.proofId), 'aria-label': `Inspect trigger for ${d.title}` }, 'Inspect trigger')));
    }));
  }

  function renderLedger() {
    const t = { info: 'accent', err: 'err-text', warn: 'warn-text', ok: 'ok-text' };
    $('#ledger').replaceChildren(...state.ledger.map((r) => h('tr', {},
      h('td', { class: 'muted' }, r.ts), h('td', {}, r.action), h('td', { class: t[r.tone] || 'accent' }, r.hash))));
  }

  function renderAll() {
    renderChrome(); renderMetrics(); renderFiles(); renderTimeline(); renderConflicts(); renderDeadlines(); renderLedger(); markActive();
  }

  function markActive() {
    const a = state.active;
    $$('.ev[data-proof], .claim[data-proof]').forEach((b) => {
      if (a && b.dataset.proof === a.id && Number(b.dataset.line) === a.line) b.setAttribute('aria-current', 'true');
      else b.removeAttribute('aria-current');
    });
  }

  /* ---------- navigation / overlays ---------- */
  function setView(v, user = false) {
    state.view = v;
    const show = v === 'all' ? SECTIONS : [v];
    $$('[data-sec]').forEach((s) => { s.hidden = !show.includes(s.dataset.sec); });
    $$('[data-view]').forEach((b) => {
      const on = b.dataset.view === v;
      if (b.classList.contains('nav-btn')) { if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); }
      else b.setAttribute('aria-pressed', String(on));
    });
    if (user) { el.stream.scrollTo({ top: 0 }); window.scrollTo({ top: 0 }); }
  }

  let overlay = null; // 'drawer' | 'sheet' | null
  let returnFocus = null;

  function openOverlay(kind) {
    if (overlay === kind) return;
    if (overlay) closeOverlay(false);
    overlay = kind;
    returnFocus = document.activeElement;
    el.scrim.hidden = false;
    document.documentElement.classList.add('lock');
    if (kind === 'drawer') {
      el.sidebar.classList.add('open');
      el.main.inert = true;
      el.menuBtn.setAttribute('aria-expanded', 'true');
      $('.nav-btn', el.sidebar).focus();
    } else {
      el.proof.classList.add('open');
      el.proof.setAttribute('role', 'dialog');
      el.proof.setAttribute('aria-modal', 'true');
      [el.sidebar, el.topbar, el.stream].forEach((n) => { n.inert = true; });
      el.proofClose.focus();
    }
  }

  function closeOverlay(restore = true) {
    if (!overlay) return;
    el.sidebar.classList.remove('open');
    el.proof.classList.remove('open');
    el.proof.removeAttribute('role');
    el.proof.removeAttribute('aria-modal');
    el.menuBtn.setAttribute('aria-expanded', 'false');
    [el.sidebar, el.topbar, el.stream, el.main].forEach((n) => { n.inert = false; });
    el.scrim.hidden = true;
    document.documentElement.classList.remove('lock');
    overlay = null;
    if (restore && returnFocus && returnFocus.isConnected) returnFocus.focus();
    returnFocus = null;
  }

  /* ---------- source proof canvas ---------- */
  function selectProof(id, line, { open = true } = {}) {
    const p = state.proofs[id];
    if (!p) return;
    const li = resolveLine(p, line);
    state.active = { id, line: li };
    stopAudio();

    const isAudio = p.kind === 'audio';
    $('#proof-file').textContent = p.file;
    $('#proof-insight').textContent = 'Linked: ' + p.insight;
    $('#proof-conf').textContent = p.conf != null ? `${p.confKind} ${p.conf.toFixed(1)}%` : p.confKind;
    $('#proof-icon').textContent = isAudio ? 'graphic_eq' : 'description';
    $('#doc-view').hidden = isAudio;
    $('#audio-view').hidden = !isAudio;
    ['#zoom-in', '#zoom-out', '#zoom-readout'].forEach((s) => { $(s).hidden = isAudio; });

    if (isAudio) {
      $('#proof-meta').textContent = `Cited at ${fmtSec(p.lines[li].sec)} • ${p.channel}`;
      buildAudio(p, li);
    } else {
      const box = p.lines[li] && p.lines[li].bbox;
      $('#proof-meta').textContent = box ? `${p.where} • BBox [${box.join(', ')}]` : p.where;
      $('#doc-exhibit').textContent = p.exhibit;
      $('#doc-page').textContent = p.pageLabel;
      $('#doc-bbox').textContent = box ? `[x1: ${box[0]}, y1: ${box[1]}, x2: ${box[2]}, y2: ${box[3]}]` : 'None (no text extracted)';
      el.docLines.replaceChildren(...p.lines.map((l, i) => (i === li ? h('mark', { class: 'hit' }, l.t) : h('p', {}, l.t))));
    }
    markActive();
    if (open && !dockedMQ.matches) openOverlay('sheet');
    const hit = $('mark.hit', el.docLines);
    if (!isAudio && hit) hit.scrollIntoView({ block: 'nearest' });
  }

  function setZoom(z) {
    state.zoom = Math.min(1.6, Math.max(0.8, Math.round(z * 10) / 10));
    $('#doc-lines').style.setProperty('--z', String(state.zoom));
    $('#zoom-readout').textContent = Math.round(state.zoom * 100) + '%';
    $('#zoom-in').disabled = state.zoom >= 1.6;
    $('#zoom-out').disabled = state.zoom <= 0.8;
  }

  /* ---------- audio (simulated clock; swap for a real <audio> element when media is wired up) ---------- */
  const audio = { timer: null, cur: 0, dur: 0, playing: false, lines: [] };

  function paintPlayBtn() {
    $('#play-icon').textContent = audio.playing ? 'pause' : 'play_arrow';
    $('#play-label').textContent = audio.playing ? 'Pause' : 'Play clip';
  }
  function stopAudio() {
    clearInterval(audio.timer);
    audio.timer = null;
    audio.playing = false;
    paintPlayBtn();
  }
  function buildAudio(p, li) {
    audio.dur = p.dur;
    audio.cur = p.lines[li].sec;
    audio.lines = p.lines;
    el.wave.replaceChildren(...WAVE.map((ht) => { const b = h('i'); b.style.height = ht + '%'; return b; }));
    el.wave.setAttribute('aria-valuemax', String(p.dur));
    $('#transcript').replaceChildren(...p.lines.map((l, i) => h('button', { type: 'button', class: 'tline', 'data-sec': String(l.sec) },
      h('span', { class: 'mono accent small' }, fmtSec(l.sec)),
      h('span', {}, l.t),
      i === li ? badge('info', 'Cited') : null)));
    paintAudio();
  }
  function paintAudio() {
    const idx = Math.min(WAVE.length - 1, Math.floor((audio.cur / audio.dur) * WAVE.length));
    [...el.wave.children].forEach((b, i) => b.classList.toggle('on', i <= idx));
    $('#audio-time').textContent = `${fmtSec(audio.cur)} / ${fmtSec(audio.dur)}`;
    el.wave.setAttribute('aria-valuenow', String(audio.cur));
    el.wave.setAttribute('aria-valuetext', `${fmtSec(audio.cur)} of ${fmtSec(audio.dur)}`);
    let now = -1;
    audio.lines.forEach((l, i) => { if (l.sec <= audio.cur) now = i; });
    $$('.tline').forEach((b, i) => {
      b.classList.toggle('now', i === now);
      if (i === now) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    });
  }
  function seek(sec) {
    audio.cur = Math.min(audio.dur, Math.max(0, Math.round(sec)));
    paintAudio();
  }

  /* ---------- upload ---------- */
  function openUpload() {
    if (!el.dlg.open) el.dlg.showModal();
    renderQueue();
  }

  function addToQueue(fileList) {
    if (state.running) return;
    if (state.done) { state.queue = []; state.done = false; }
    for (const f of fileList) {
      if (state.queue.some((q) => q.file.name === f.name && q.file.size === f.size && q.file.lastModified === f.lastModified)) continue;
      if (state.queue.length >= MAX_QUEUE) { state.queue.push({ file: f, state: 'rejected', reason: `Queue limit (${MAX_QUEUE} files)` }); continue; }
      const v = Core.validateFile(f, MAX_BYTES);
      state.queue.push(v.ok ? { file: f, state: 'ready', type: v.type } : { file: f, state: 'rejected', reason: v.reason });
    }
    openUpload();
  }

  function qStatus(q) {
    switch (q.state) {
      case 'ready': return h('span', { class: 'muted' }, Core.fmtBytes(q.file.size) + ' · Ready');
      case 'rejected': return h('span', { class: 'err-text' }, q.reason);
      case 'running': return h('progress', { max: '100', value: String(q.pct || 0), 'aria-label': 'Processing ' + q.file.name });
      case 'done': return h('span', { class: 'ok-text' }, 'Indexed');
      default: return h('span', { class: 'err-text' }, q.reason || 'Failed');
    }
  }

  function renderQueue() {
    $('#queue').replaceChildren(...state.queue.map((q) => h('li', { class: 'qrow' },
      h('span', { class: 'mono accent' }, q.type || '—'),
      h('span', { class: 'qname mono', title: q.file.name }, q.file.name),
      qStatus(q))));
    const run = $('#run-btn');
    const ready = state.queue.some((q) => q.state === 'ready');
    if (state.done) { run.textContent = 'View in timeline'; run.disabled = false; }
    else { run.textContent = state.running ? 'Processing…' : 'Run ingestion'; run.disabled = state.running || !ready; }
  }

  /* Local ingestion: read + SHA-256 in the browser. OCR/ASR extraction needs a backend; plug it in here
     and return real events/proof lines instead of the "pending" placeholder. */
  async function ingestOne(q) {
    const f = q.file;
    const buf = await f.arrayBuffer();
    q.pct = 50; renderQueue();
    const hash = await Core.sha256Hex(buf);
    q.pct = 100;
    const id = uid();
    const pid = 'p-up-' + id;
    const when = new Date(f.lastModified || Date.now());
    state.proofs[pid] = {
      kind: 'doc', file: f.name, loc: 'file', where: 'File metadata', insight: 'Ingested file: ' + f.name,
      conf: null, confKind: 'Awaiting extraction', exhibit: 'UPLOAD • ' + q.type, pageLabel: 'NO TEXT LAYER', hl: -1,
      lines: [
        { t: 'FILE: ' + f.name },
        { t: `TYPE: ${f.type || 'unknown'} • SIZE: ${Core.fmtBytes(f.size)}` },
        { t: 'MODIFIED: ' + when.toISOString() },
        { t: 'SHA-256: ' + (hash || 'unavailable (needs a secure context: https or localhost)') },
        { t: 'No OCR / transcription backend is connected. No text has been extracted from this file.' },
      ],
    };
    state.events.push({ id: 'ev-' + id, title: 'Ingested: ' + f.name, at: when.toISOString(), actors: 'Not extracted', proofId: pid, status: 'pending' });
    state.files.unshift({ id, name: f.name, type: q.type, status: 'Awaiting extraction', tone: 'warn' });
    state.ledger.push({ ts: Core.formatInZone(new Date().toISOString(), TZ), action: 'Hashed on ingest: ' + f.name, hash: hash ? Core.shortHash(hash) : 'n/a', tone: hash ? 'info' : 'warn' });
    state.lastProof = pid;
  }

  async function runIngestion() {
    if (state.done) {
      el.dlg.close();
      setView('timeline', true);
      if (state.lastProof) selectProof(state.lastProof);
      return;
    }
    state.running = true;
    renderQueue();
    for (const q of state.queue.filter((x) => x.state === 'ready')) {
      q.state = 'running'; q.pct = 5; renderQueue();
      try { await ingestOne(q); q.state = 'done'; }
      catch (err) { console.error('Ingestion failed', err); q.state = 'error'; q.reason = 'Could not read file'; }
      renderQueue();
    }
    state.running = false;
    state.done = state.queue.some((x) => x.state === 'done');
    renderQueue();
    renderAll();
    const ok = state.queue.filter((x) => x.state === 'done').length;
    $('#queue-status').textContent = `${ok} of ${state.queue.length} files ingested.`;
  }

  function wireDrop(node) {
    node.addEventListener('dragenter', (e) => { e.preventDefault(); node.classList.add('drag'); });
    node.addEventListener('dragover', (e) => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'; node.classList.add('drag'); });
    node.addEventListener('dragleave', (e) => { if (!node.contains(e.relatedTarget)) node.classList.remove('drag'); });
    node.addEventListener('drop', (e) => { e.preventDefault(); node.classList.remove('drag'); if (e.dataTransfer) addToQueue(e.dataTransfer.files); });
  }

  /* ---------- events ---------- */
  document.addEventListener('click', (e) => {
    if (!(e.target instanceof Element)) return;
    const t = e.target;
    const view = t.closest('[data-view]');
    if (view) { setView(view.dataset.view, true); if (overlay === 'drawer') closeOverlay(); return; }
    const tl = t.closest('[data-tl]');
    if (tl) {
      state.tl = tl.dataset.tl;
      $$('[data-tl]').forEach((b) => b.setAttribute('aria-pressed', String(b === tl)));
      renderTimeline(); markActive();
      return;
    }
    const pf = t.closest('[data-proof]');
    if (pf) { selectProof(pf.dataset.proof, pf.dataset.line === undefined ? undefined : Number(pf.dataset.line)); return; }
    const tline = t.closest('.tline');
    if (tline) seek(Number(tline.dataset.sec));
  });

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && overlay) { e.preventDefault(); closeOverlay(); } });

  el.menuBtn.addEventListener('click', () => openOverlay('drawer'));
  $('#drawer-close').addEventListener('click', () => closeOverlay());
  el.proofClose.addEventListener('click', () => closeOverlay());
  el.scrim.addEventListener('click', () => closeOverlay());
  dockedMQ.addEventListener('change', () => { if (overlay === 'sheet') closeOverlay(false); });
  wideMQ.addEventListener('change', () => { if (overlay === 'drawer') closeOverlay(false); });

  $('#zoom-in').addEventListener('click', () => setZoom(state.zoom + 0.1));
  $('#zoom-out').addEventListener('click', () => setZoom(state.zoom - 0.1));

  $('#play').addEventListener('click', () => {
    if (audio.playing) { stopAudio(); return; }
    if (!audio.dur) return;
    if (audio.cur >= audio.dur) audio.cur = 0;
    audio.playing = true;
    paintPlayBtn();
    audio.timer = setInterval(() => {
      audio.cur = Math.min(audio.dur, audio.cur + 1);
      paintAudio();
      if (audio.cur >= audio.dur) stopAudio();
    }, 1000);
  });
  el.wave.addEventListener('pointerdown', (e) => {
    const r = el.wave.getBoundingClientRect();
    if (r.width) seek(((e.clientX - r.left) / r.width) * audio.dur);
  });
  el.wave.addEventListener('keydown', (e) => {
    let to = null;
    switch (e.key) {
      case 'ArrowLeft': to = audio.cur - 5; break;
      case 'ArrowRight': to = audio.cur + 5; break;
      case 'PageDown': to = audio.cur - 15; break;
      case 'PageUp': to = audio.cur + 15; break;
      case 'Home': to = 0; break;
      case 'End': to = audio.dur; break;
      default: return;
    }
    e.preventDefault();
    seek(to);
  });

  $('#search-form').addEventListener('submit', (e) => e.preventDefault());
  $('#q').addEventListener('input', (e) => {
    state.q = e.target.value.trim().toLowerCase();
    const n = renderTimeline(); const c = renderConflicts(); const f = renderFiles();
    markActive();
    $('#search-status').textContent = state.q ? `${n} events, ${c} contradictions, ${f} files match.` : '';
  });

  $('#upload-btn').addEventListener('click', openUpload);
  $('#drop').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', (e) => { addToQueue(e.target.files); e.target.value = ''; });
  $('#run-btn').addEventListener('click', runIngestion);
  $('#dlg-close').addEventListener('click', () => el.dlg.close());
  $('#dlg-cancel').addEventListener('click', () => el.dlg.close());
  el.dlg.addEventListener('click', (e) => { if (e.target === el.dlg) el.dlg.close(); });
  el.dlg.addEventListener('close', () => { if (!state.running) { state.queue = []; state.done = false; renderQueue(); } });
  wireDrop($('#drop'));
  wireDrop($('#dlg-drop'));
  // A drop that misses the zones must not navigate the tab to the dropped file.
  ['dragover', 'drop'].forEach((type) => window.addEventListener(type, (e) => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) e.preventDefault();
  }));

  // Deadlines are date-relative: refresh when a long-open tab regains focus.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { renderDeadlines(); renderMetrics(); } });
  window.addEventListener('pagehide', stopAudio);

  /* ---------- boot ---------- */
  renderAll();
  setView('all');
  setZoom(1);
  selectProof('p-ezpass', undefined, { open: false });
})();
