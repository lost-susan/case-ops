/* CASE-OPS login page controller. All output goes through textContent (no HTML strings). */
(() => {
  'use strict';

  const A = window.CaseOpsAuth;
  const APP_URL = 'app.html';
  const $ = (id) => document.getElementById(id);

  const form = $('login-form');
  const email = $('official-email');
  const pass = $('password-input');
  const btn = $('sign-in-btn');
  const label = $('sign-in-label');
  const spinner = $('sign-in-spinner');
  const alertEl = $('form-alert');
  const status = $('login-status');
  const errs = { email: $('email-error'), password: $('password-error') };

  if (!A) { showAlert('Sign-in is unavailable. Reload the page.'); btn.disabled = true; return; }
  if (A.getSession()) { location.replace(APP_URL); return; }

  let busy = false;
  let countdown = null;

  function showAlert(msg) { alertEl.textContent = msg; alertEl.hidden = false; }
  function clearErrors() {
    alertEl.hidden = true; alertEl.textContent = '';
    for (const [k, node] of Object.entries(errs)) { node.hidden = true; node.textContent = ''; }
    email.removeAttribute('aria-invalid'); pass.removeAttribute('aria-invalid');
  }
  function fieldError(input, node, msg) { node.textContent = msg; node.hidden = false; input.setAttribute('aria-invalid', 'true'); }

  function setBusy(on, text) {
    busy = on;
    btn.disabled = on;
    spinner.hidden = !on;
    label.textContent = text || 'Sign In';
    form.setAttribute('aria-busy', String(on));
  }

  /* Lockout: the page shows one static message (so screen readers are not spammed) and counts down on the button. */
  function startLock(ms) {
    clearInterval(countdown);
    const until = Date.now() + ms;
    const secs = Math.ceil(ms / 1000);
    showAlert(`Too many failed attempts. Sign-in is paused for ${secs >= 60 ? Math.ceil(secs / 60) + ' minute' + (secs > 60 ? 's' : '') : secs + ' seconds'}.`);
    const tick = () => {
      const left = Math.ceil((until - Date.now()) / 1000);
      if (left <= 0) { clearInterval(countdown); countdown = null; setBusy(false); alertEl.hidden = true; return; }
      setBusy(false); btn.disabled = true; label.textContent = `Try again in ${left}s`;
    };
    tick();
    countdown = setInterval(tick, 1000);
  }

  $('toggle-password-btn').addEventListener('click', (e) => {
    const show = pass.type === 'password';
    pass.type = show ? 'text' : 'password';
    $('toggle-password-icon').textContent = show ? 'visibility_off' : 'visibility';
    e.currentTarget.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    e.currentTarget.setAttribute('aria-pressed', String(show));
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (busy || countdown) return;
    clearErrors();
    status.hidden = true;

    const v = A.validateCredentials(email.value, pass.value);
    if (!v.ok) {
      if (v.errors.email) fieldError(email, errs.email, v.errors.email);
      if (v.errors.password) fieldError(pass, errs.password, v.errors.password);
      (v.errors.email ? email : pass).focus();
      return;
    }

    setBusy(true, 'Signing in…');
    let r;
    try { r = await A.authenticate(email.value, pass.value); }
    catch (err) { console.error('Sign-in error', err); r = { ok: false, code: 'error' }; }

    if (r.ok) {
      pass.value = '';
      status.textContent = 'Demo session verified • Routing to NY-2026-CR...';
      status.hidden = false;
      setTimeout(() => location.replace(APP_URL), 350);
      return;
    }

    pass.value = '';
    switch (r.code) {
      case 'locked': startLock(r.retryInMs); return;
      case 'invalid-input':
        if (r.errors.email) fieldError(email, errs.email, r.errors.email);
        if (r.errors.password) fieldError(pass, errs.password, r.errors.password);
        break;
      case 'unsupported': showAlert('Secure sign-in needs HTTPS or localhost. Open the site over a secure connection.'); break;
      case 'invalid': showAlert('Invalid email or password.'); break;
      default: showAlert('Something went wrong. Please try again.');
    }
    setBusy(false);
    pass.focus();
  });

  // A lock survives a page refresh.
  const lock = A.lockStatus();
  if (lock.locked) startLock(lock.retryInMs);
})();
