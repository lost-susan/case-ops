/* CASE-OPS core helpers. Pure (no DOM): runs in the browser (window.CaseOpsCore) and in Node (tests). */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CaseOpsCore = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  const DAY = 86400000;

  /* ---------- Dates (calendar math in UTC so DST and the viewer's timezone can't shift a deadline) ---------- */
  function parseYMD(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s));
    if (!m) throw new RangeError('Invalid date: ' + s);
    const t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
    const d = new Date(t);
    if (d.getUTCFullYear() !== +m[1] || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) {
      throw new RangeError('Invalid date: ' + s);
    }
    return t;
  }
  const ymd = (t) => new Date(t).toISOString().slice(0, 10);
  const addDays = (s, n) => ymd(parseYMD(s) + n * DAY);
  const daysBetween = (a, b) => Math.round((parseYMD(b) - parseYMD(a)) / DAY);

  function addMonths(s, n) {
    const d = new Date(parseYMD(s));
    const first = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + n, 1));
    const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
    return ymd(first.getTime() + (Math.min(d.getUTCDate(), last) - 1) * DAY);
  }

  const urgency = (left) => (left < 0 ? 'Overdue' : left <= 7 ? 'Urgent' : left <= 30 ? 'Active' : 'Upcoming');

  /** rule: { days } or { months }. Deterministic: same inputs, same output. */
  function computeDeadline(rule, trigger, today) {
    const hasM = Number.isInteger(rule.months);
    const hasD = Number.isInteger(rule.days);
    if (hasM === hasD) throw new RangeError('Rule needs exactly one of days or months');
    const due = hasM ? addMonths(trigger, rule.months) : addDays(trigger, rule.days);
    const left = daysBetween(today, due);
    const unit = hasM ? rule.months + ' months' : rule.days + ' days';
    return { due, left, urgency: urgency(left), formula: `${trigger} + ${unit} = ${due}` };
  }

  /** Today's calendar date (YYYY-MM-DD) in the court's timezone, not the viewer's. */
  function todayIn(tz, now = new Date()) {
    return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(now);
  }

  /** "2026-03-14 21:18:04 EDT" for an ISO instant, rendered in the given IANA timezone. */
  function formatInZone(iso, tz) {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '—';
    const p = {};
    new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
    }).formatToParts(t).forEach((x) => { p[x.type] = x.value; });
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}`;
  }

  /* ---------- Uploads ---------- */
  const ALLOWED = {
    pdf: ['application/pdf', 'PDF'],
    txt: ['text/', 'TXT'],
    wav: ['audio/', 'WAV'],
    mp3: ['audio/', 'MP3'],
    jpg: ['image/', 'JPG'],
    jpeg: ['image/', 'JPG'],
    png: ['image/', 'PNG'],
  };

  /** Extension must be allowed AND the browser-reported MIME (when present) must agree. */
  function validateFile(f, maxBytes) {
    const ext = String(f.name).includes('.') ? String(f.name).split('.').pop().toLowerCase() : '';
    const rule = Object.hasOwn(ALLOWED, ext) ? ALLOWED[ext] : null;
    if (!rule) return { ok: false, reason: 'Unsupported file type' };
    if (f.type && !f.type.startsWith(rule[0])) return { ok: false, reason: 'Content type does not match extension' };
    if (!f.size) return { ok: false, reason: 'Empty file' };
    if (f.size > maxBytes) return { ok: false, reason: 'Larger than ' + fmtBytes(maxBytes) };
    return { ok: true, type: rule[1] };
  }

  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i];
  }

  /** Hex SHA-256, or null when SubtleCrypto is unavailable (non-secure context such as http://192.168.x.x). */
  async function sha256Hex(buf) {
    const subtle = globalThis.crypto && globalThis.crypto.subtle;
    if (!subtle) return null;
    const d = await subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, '0')).join('');
  }

  const shortHash = (h) => `${h.slice(0, 4)}...${h.slice(-4)}`;
  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);

  return { parseYMD, addDays, addMonths, daysBetween, urgency, computeDeadline, todayIn, formatInZone, validateFile, fmtBytes, sha256Hex, shortHash, mean };
});
