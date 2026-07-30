/* ONYX / Onyx Ledger — shared client (API + auth) */
(function () {
  const TOKEN_KEY = 'onyx_token';
  const USER_KEY = 'onyx_user';

  const ONYX = {
    token: () => localStorage.getItem(TOKEN_KEY),
    user: () => { try { return JSON.parse(localStorage.getItem(USER_KEY)); } catch (_) { return null; } },
    setAuth(token, user) { localStorage.setItem(TOKEN_KEY, token); localStorage.setItem(USER_KEY, JSON.stringify(user)); },
    signOut() { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); location.href = 'login.html'; },

    async api(path, { method = 'GET', body, form } = {}) {
      const headers = {};
      const token = ONYX.token();
      if (token) headers.authorization = 'Bearer ' + token;
      let payload;
      if (form) { payload = form; }
      else if (body) { headers['content-type'] = 'application/json'; payload = JSON.stringify(body); }
      const res = await fetch('/api' + path, { method, headers, body: payload });
      if (res.status === 401 && !path.startsWith('/auth')) { ONYX.signOut(); throw new Error('Session expired.'); }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
      return data;
    },

    requireAuth() { if (!ONYX.token()) { location.href = 'login.html'; } },

    money(n) { return (n < 0 ? '-$' : '$') + Math.abs(Math.round(n)).toLocaleString('en-CA'); },
    pctStr(n) { return (n * 100).toFixed(1) + '%'; },
  };

  window.ONYX = ONYX;
})();
