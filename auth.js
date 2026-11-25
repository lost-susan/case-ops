/*
 * CASE-OPS TEMPORARY client-side auth (demo only). Replace with the real backend.
 *
 * READ THIS: a browser-only check cannot protect data. Anyone can read this file, edit sessionStorage
 * or skip the redirect. It exists so the UI flow (login -> workspace -> sign out) can be built and tested.
 * Real auth must be server-side: verified credentials, httpOnly Secure SameSite cookie, CSRF protection,
 * server-side rate limiting, and parameterized queries for every SQL statement.
 *
 * What this module still does properly, so the contract carries over to the backend:
 *  - No plaintext password in the source: PBKDF2-HMAC-SHA256 (600k iterations, per-user salt), constant-time compare.
 *  - Strict input validation before anything else touches the values (type, length, charset, email shape).
 *  - The same generic failure for "unknown email" and "wrong password"; the hash is always computed.
 *  - Failed-attempt throttling with exponential back-off.
 *  - Expiring, random session token.
 *  - Inputs are only ever compared as strings. There is no query, eval or HTML sink anywhere in this file.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CaseOpsAuth = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  /* Demo account. Generated with: crypto.pbkdf2Sync(password, salt, 600000, 32, 'sha256'). */
  const DEMO_USER = Object.freeze({
    email: 'lawyer@organization.com',
    salt: 'ead04b38ffbef00c3123a38e27868115',
    iterations: 600000,
    hash: '347cfb14a9f54a05a55904458358d4db80974916f8edaebf61f962b0a41709e3',
  });

  const LIMITS = Object.freeze({ emailMax: 254, passwordMax: 128 });
  const SESSION_KEY = 'caseops.session';
  const THROTTLE_KEY = 'caseops.auth.throttle';
  const SESSION_TTL_MS = 8 * 60 * 60 * 1000;
  const FREE_ATTEMPTS = 5;
  const BASE_LOCK_MS = 30 * 1000;
  const MAX_LOCK_MS = 15 * 60 * 1000;

  const EMAIL_RE = /^[a-z0-9._%+-]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$/;
  const CONTROL_RE = /[\u0000-\u001f\u007f]/;

  /* ---------- storage (sessionStorage when usable, in-memory otherwise) ---------- */
  function memoryStorage() {
    const m = new Map();
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => { m.set(k, String(v)); },
      removeItem: (k) => { m.delete(k); },
    };
  }
  let fallback = null;
  function defaultStorage() {
    try {
      if (typeof sessionStorage !== 'undefined') {
        const probe = '__caseops_probe__';
        sessionStorage.setItem(probe, '1');
        sessionStorage.removeItem(probe);
        return sessionStorage;
      }
    } catch (_) { /* blocked storage: fall through */ }
    return fallback || (fallback = memoryStorage());
  }

  /* ---------- validation ---------- */
  const normalizeEmail = (v) => String(v).trim().toLowerCase();

  /** Pure. Never throws. Returns { ok, email, errors:{ email?, password? } }. */
  function validateCredentials(email, password) {
    const errors = {};
    let e = '';
    if (typeof email !== 'string' || !email.trim()) errors.email = 'Enter your email address.';
    else {
      e = normalizeEmail(email);
      if (e.length > LIMITS.emailMax || CONTROL_RE.test(e) || !EMAIL_RE.test(e)) errors.email = 'Enter a valid email address.';
    }
    if (typeof password !== 'string' || password.length === 0) errors.password = 'Enter your password.';
    else if (password.length > LIMITS.passwordMax) errors.password = `Password must be ${LIMITS.passwordMax} characters or fewer.`;
    else if (CONTROL_RE.test(password)) errors.password = 'Password contains unsupported characters.';
    return { ok: !errors.email && !errors.password, email: e, errors };
  }

  /* ---------- crypto ---------- */
  const hexToBytes = (hex) => Uint8Array.from(hex.match(/../g), (b) => parseInt(b, 16));
  const bytesToHex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

  function subtle() {
    const c = globalThis.crypto;
    return c && c.subtle ? c.subtle : null;
  }

  async function hashPassword(password, saltHex, iterations) {
    const s = subtle();
    if (!s) throw new Error('SubtleCrypto unavailable');
    const key = await s.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
    const bits = await s.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: hexToBytes(saltHex), iterations }, key, 256);
    return bytesToHex(new Uint8Array(bits));
  }

  /** Constant-time for equal-length strings; length mismatch is a (non-secret) early false. */
  function safeEqual(a, b) {
    if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  function randomToken() {
    const c = globalThis.crypto;
    if (!c || !c.getRandomValues) throw new Error('Secure random unavailable');
    return bytesToHex(c.getRandomValues(new Uint8Array(32)));
  }

  /* ---------- throttle ---------- */
  function readThrottle(storage) {
    try {
      const o = JSON.parse(storage.getItem(THROTTLE_KEY) || 'null');
      if (o && Number.isInteger(o.fails) && o.fails >= 0 && Number.isFinite(o.lockedUntil)) return { fails: o.fails, lockedUntil: o.lockedUntil };
    } catch (_) { /* corrupt entry: reset */ }
    return { fails: 0, lockedUntil: 0 };
  }
  const writeThrottle = (storage, t) => { try { storage.setItem(THROTTLE_KEY, JSON.stringify(t)); } catch (_) { /* best effort */ } };

  function lockStatus(opts = {}) {
    const storage = opts.storage || defaultStorage();
    const now = (opts.now || Date.now)();
    const t = readThrottle(storage);
    const retryInMs = Math.max(0, t.lockedUntil - now);
    return { locked: retryInMs > 0, retryInMs };
  }

  function recordFailure(storage, now) {
    const t = readThrottle(storage);
    t.fails += 1;
    if (t.fails >= FREE_ATTEMPTS) t.lockedUntil = now + Math.min(MAX_LOCK_MS, BASE_LOCK_MS * 2 ** (t.fails - FREE_ATTEMPTS));
    writeThrottle(storage, t);
    return Math.max(0, t.lockedUntil - now);
  }

  /* ---------- sessions ---------- */
  function createSession(email, storage, now) {
    const session = { token: randomToken(), email, iat: now, exp: now + SESSION_TTL_MS };
    storage.setItem(SESSION_KEY, JSON.stringify(session));
    return session;
  }

  function getSession(opts = {}) {
    const storage = opts.storage || defaultStorage();
    const now = (opts.now || Date.now)();
    try {
      const s = JSON.parse(storage.getItem(SESSION_KEY) || 'null');
      if (s && typeof s.token === 'string' && /^[0-9a-f]{64}$/.test(s.token) && typeof s.email === 'string' &&
          Number.isFinite(s.iat) && Number.isFinite(s.exp) && s.exp > now && s.iat <= now + 60000) return s;
    } catch (_) { /* malformed: treat as signed out */ }
    storage.removeItem(SESSION_KEY);
    return null;
  }

  function signOut(opts = {}) { (opts.storage || defaultStorage()).removeItem(SESSION_KEY); }

  /* ---------- main entry ---------- */
  /**
   * Resolves to:
   *  { ok:true, session }
   *  { ok:false, code:'invalid-input', errors }      format problems, not counted as an attempt
   *  { ok:false, code:'invalid' }                    wrong email or password (deliberately indistinguishable)
   *  { ok:false, code:'locked', retryInMs }
   *  { ok:false, code:'unsupported' }                no SubtleCrypto (needs https or localhost)
   */
  async function authenticate(email, password, opts = {}) {
    const storage = opts.storage || defaultStorage();
    const clock = opts.now || Date.now;

    const lock = lockStatus({ storage, now: clock });
    if (lock.locked) return { ok: false, code: 'locked', retryInMs: lock.retryInMs };

    const v = validateCredentials(email, password);
    if (!v.ok) return { ok: false, code: 'invalid-input', errors: v.errors };
    if (!subtle()) return { ok: false, code: 'unsupported' };

    // Always hash, even for an unknown email, so timing does not reveal which part was wrong.
    const derived = await hashPassword(password, DEMO_USER.salt, DEMO_USER.iterations);
    const emailOk = safeEqual(v.email, DEMO_USER.email);
    const passOk = safeEqual(derived, DEMO_USER.hash);

    if (!(emailOk && passOk)) {
      const retryInMs = recordFailure(storage, clock());
      return retryInMs > 0 ? { ok: false, code: 'locked', retryInMs } : { ok: false, code: 'invalid' };
    }

    writeThrottle(storage, { fails: 0, lockedUntil: 0 });
    return { ok: true, session: createSession(v.email, storage, clock()) };
  }

  /** Browser guard for protected pages. Returns true when a valid session exists, else redirects. */
  function requireSession(loginUrl = './') {
    if (getSession()) return true;
    if (typeof location !== 'undefined') location.replace(loginUrl);
    return false;
  }

  return {
    LIMITS, FREE_ATTEMPTS, SESSION_TTL_MS,
    validateCredentials, normalizeEmail, authenticate, lockStatus,
    getSession, signOut, requireSession, memoryStorage, safeEqual, hashPassword,
  };
});
