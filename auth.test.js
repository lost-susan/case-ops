// Run: node --test   (Node 20+). Covers validation, injection payloads, throttling and sessions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const A = require('./auth.js');

const EMAIL = 'lawyer@organization.com';
const PASS = 'demo-access-key';
const mem = () => A.memoryStorage();
const clock = (t0 = 1_000_000) => { let t = t0; const f = () => t; f.adv = (ms) => { t += ms; }; return f; };

const SQLI = [
  "' OR '1'='1", "' OR 1=1--", "admin'--", "\" OR \"\"=\"", "'; DROP TABLE users;--",
  "' UNION SELECT NULL,NULL--", "1; WAITFOR DELAY '0:0:5'--", "' OR SLEEP(5)--", "\\' OR 1=1 -- ",
  "%27%20OR%20%271%27%3D%271", "ʼ OR 1=1", "') OR ('1'='1", "admin' /*", "' OR 'x'='x' #",
  EMAIL + "'--", EMAIL + "' OR '1'='1", EMAIL + "\u0000", EMAIL + "\n' OR 1=1",
];
const XSS = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "javascript:alert(1)", '${7*7}', '{{7*7}}'];

test('correct demo credentials sign in (email is trimmed and case-insensitive)', async () => {
  const storage = mem();
  const r = await A.authenticate('  LAWYER@Organization.com ', PASS, { storage });
  assert.equal(r.ok, true);
  assert.match(r.session.token, /^[0-9a-f]{64}$/);
  assert.equal(A.getSession({ storage }).email, EMAIL);
});

test('SQL injection payloads never authenticate, as email, as password, or both', async () => {
  for (const p of SQLI) {
    for (const [e, pw] of [[p, PASS], [EMAIL, p], [p, p]]) {
      const storage = mem();
      const r = await A.authenticate(e, pw, { storage });
      assert.equal(r.ok, false, `bypass with ${JSON.stringify([e, pw])}`);
      assert.ok(['invalid', 'invalid-input'].includes(r.code), r.code);
      assert.equal(A.getSession({ storage }), null);
    }
  }
});

test('XSS / template payloads never authenticate and never throw', async () => {
  for (const p of XSS) {
    for (const [e, pw] of [[p, PASS], [EMAIL, p]]) {
      const r = await A.authenticate(e, pw, { storage: mem() });
      assert.equal(r.ok, false);
    }
  }
});

test('hostile input types and sizes are rejected without throwing', async () => {
  for (const [e, pw] of [[null, null], [undefined, PASS], [{}, PASS], [[EMAIL], PASS], [EMAIL, 123], [EMAIL, ['x']],
    ['a'.repeat(300) + '@x.com', PASS], [EMAIL, 'p'.repeat(129)], [EMAIL, 'bad\u0000pass'], ['', ''], ['   ', PASS]]) {
    const r = await A.authenticate(e, pw, { storage: mem() });
    assert.equal(r.ok, false);
    assert.equal(r.code, 'invalid-input');
  }
});

test('injection-shaped emails are rejected by format before any hashing', () => {
  for (const p of SQLI.filter((x) => x.includes('@') || x.includes("'"))) {
    assert.equal(A.validateCredentials(p, PASS).ok, false, p);
  }
  assert.equal(A.validateCredentials(EMAIL, PASS).ok, true);
});

test('wrong email and wrong password are indistinguishable', async () => {
  const a = await A.authenticate('other@organization.com', PASS, { storage: mem() });
  const b = await A.authenticate(EMAIL, 'wrong-password', { storage: mem() });
  assert.deepEqual(a, b);
  assert.equal(a.code, 'invalid');
});

test('lockout after 5 failures, persists, expires, and resets on success', async () => {
  const storage = mem(); const now = clock();
  for (let i = 0; i < A.FREE_ATTEMPTS - 1; i++) assert.equal((await A.authenticate(EMAIL, 'nope', { storage, now })).code, 'invalid');
  const fifth = await A.authenticate(EMAIL, 'nope', { storage, now });
  assert.equal(fifth.code, 'locked');
  assert.equal(fifth.retryInMs, 30000);
  // correct password is refused while locked, with no hashing
  assert.equal((await A.authenticate(EMAIL, PASS, { storage, now })).code, 'locked');
  now.adv(30001);
  assert.equal(A.lockStatus({ storage, now }).locked, false);
  // next failure doubles the lock
  assert.equal((await A.authenticate(EMAIL, 'nope', { storage, now })).retryInMs, 60000);
  now.adv(60001);
  assert.equal((await A.authenticate(EMAIL, PASS, { storage, now })).ok, true);
  assert.equal(A.lockStatus({ storage, now }).locked, false);
});

test('corrupt throttle or session storage is treated as empty', async () => {
  const storage = mem();
  storage.setItem('caseops.auth.throttle', '{"fails":"x"}');
  storage.setItem('caseops.session', '{"token":"short","email":1}');
  assert.equal(A.lockStatus({ storage }).locked, false);
  assert.equal(A.getSession({ storage }), null);
  storage.setItem('caseops.session', 'not json');
  assert.equal(A.getSession({ storage }), null);
});

test('sessions expire and sign-out clears them', async () => {
  const storage = mem(); const now = clock();
  await A.authenticate(EMAIL, PASS, { storage, now });
  assert.ok(A.getSession({ storage, now }));
  now.adv(A.SESSION_TTL_MS + 1);
  assert.equal(A.getSession({ storage, now }), null);
  await A.authenticate(EMAIL, PASS, { storage, now });
  A.signOut({ storage });
  assert.equal(A.getSession({ storage, now }), null);
});

test('no plaintext password in shipped source', () => {
  for (const f of ['auth.js', 'login.js', 'session.js', 'app.js', 'data.js']) {
    assert.ok(!fs.readFileSync('./' + f, 'utf8').includes(PASS), f);
  }
});

test('safeEqual', () => {
  assert.ok(A.safeEqual('abc', 'abc'));
  assert.ok(!A.safeEqual('abc', 'abd') && !A.safeEqual('abc', 'ab') && !A.safeEqual('a', 1));
});
