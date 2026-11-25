/* Guard for protected pages. Loaded in <head> after auth.js so signed-out visitors are redirected immediately. */
(() => {
  'use strict';
  const A = window.CaseOpsAuth;
  if (!A || !A.requireSession('./')) return;

  document.addEventListener('DOMContentLoaded', () => {
    const s = A.getSession();
    const who = document.getElementById('user-email');
    if (who && s) who.textContent = s.email;
    const out = document.getElementById('signout-btn');
    if (out) out.addEventListener('click', () => { A.signOut(); location.replace('./'); });
  });

  // An expired session must not keep a long-open tab usable.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !A.getSession()) location.replace('./');
  });
})();
