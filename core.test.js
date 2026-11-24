// Run: node --test   (Node 20+, no dependencies)
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const C = require('./core.js');

test('deadline arithmetic is exact and deterministic', () => {
  assert.equal(C.addDays('2026-10-14', 45), '2026-11-28');
  assert.equal(C.addDays('2026-10-14', 53), '2026-12-06');
  assert.equal(C.addDays('2026-10-14', 180), '2027-04-12');
  assert.equal(C.addMonths('2026-10-14', 6), '2027-04-14');
  assert.equal(C.addMonths('2026-08-31', 6), '2027-02-28'); // clamps to month end
  assert.equal(C.addMonths('2027-08-31', 6), '2028-02-29'); // leap year
  assert.equal(C.daysBetween('2026-11-24', '2026-11-28'), 4);
});

test('invalid dates and rules are rejected, not rolled over', () => {
  assert.throws(() => C.parseYMD('2026-02-30'), RangeError);
  assert.throws(() => C.parseYMD('14/10/2026'), RangeError);
  assert.throws(() => C.computeDeadline({}, '2026-10-14', '2026-11-24'), RangeError);
  assert.throws(() => C.computeDeadline({ days: 1, months: 1 }, '2026-10-14', '2026-11-24'), RangeError);
});

test('urgency boundaries', () => {
  const u = (left) => C.urgency(left);
  assert.deepEqual([u(-1), u(0), u(7), u(8), u(30), u(31)], ['Overdue', 'Urgent', 'Urgent', 'Active', 'Active', 'Upcoming']);
  const d = C.computeDeadline({ days: 45 }, '2026-10-14', '2026-11-24');
  assert.deepEqual([d.due, d.left, d.urgency], ['2026-11-28', 4, 'Urgent']);
});

test('timestamps render in the court timezone regardless of viewer timezone', () => {
  assert.equal(C.formatInZone('2026-03-14T21:18:04-04:00', 'America/New_York'), '2026-03-14 21:18:04 EDT');
  assert.equal(C.formatInZone('2026-11-24T10:00:00Z', 'America/New_York'), '2026-11-24 05:00:00 EST');
  assert.equal(C.formatInZone('nope', 'America/New_York'), '—');
});

test('upload validation', () => {
  const ok = (name, type = '', size = 10) => C.validateFile({ name, type, size }, 100).ok;
  assert.ok(ok('a.pdf', 'application/pdf') && ok('B.PNG', 'image/png') && ok('chat.txt', '') && ok('c.wav', 'audio/x-wav'));
  assert.ok(!ok('evil.exe') && !ok('noext') && !ok('constructor') && !ok('page.html', 'text/html'));
  assert.ok(!ok('fake.pdf', 'text/html'), 'MIME must agree with extension');
  assert.ok(!ok('empty.pdf', 'application/pdf', 0));
  assert.ok(!ok('big.pdf', 'application/pdf', 101));
});

test('sha256', async (t) => {
  if (!globalThis.crypto || !globalThis.crypto.subtle) return t.skip('no SubtleCrypto');
  const buf = new TextEncoder().encode('abc');
  assert.equal(await C.sha256Hex(buf), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

test('demo data: every insight resolves to raw proof', () => {
  global.window = {};
  require('./data.js');
  const D = global.window.CASE_DATA;
  const okLine = (r) => D.proofs[r.proofId] && D.proofs[r.proofId].lines[r.line];
  for (const c of D.contradictions) assert.ok(okLine(c.a) && okLine(c.b), c.id);
  for (const e of D.events) {
    assert.ok(D.proofs[e.proofId], e.id);
    assert.ok(['verified', 'conflict', 'pending'].includes(e.status), e.id);
    assert.ok(!Number.isNaN(Date.parse(e.at)), e.id);
  }
  for (const d of D.deadlines) {
    assert.ok(D.proofs[d.trigger.proofId], d.id);
    assert.doesNotThrow(() => C.computeDeadline(d.rule, d.trigger.date, '2026-11-24'), d.id);
  }
  for (const p of Object.values(D.proofs)) {
    assert.ok(p.hl >= 0 && p.hl < p.lines.length);
    if (p.kind === 'audio') assert.ok(p.lines.every((l) => l.sec >= 0 && l.sec <= p.dur));
  }
});

test('shipped files obey the security posture (no HTML-string sinks, no inline script/style)', () => {
  const app = fs.readFileSync('./app.js', 'utf8');
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/.test(app));
  const html = fs.readFileSync('./index.html', 'utf8');
  assert.ok(!/<script(?![^>]*\bsrc=)/i.test(html), 'inline <script>');
  assert.ok(!/\sstyle\s*=/i.test(html), 'inline style attribute');
  assert.ok(!/\son[a-z]+\s*=/i.test(html), 'inline event handler');
  assert.match(html, /Content-Security-Policy/);
});
