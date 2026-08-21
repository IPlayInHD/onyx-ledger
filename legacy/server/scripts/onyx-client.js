/* ===== Local (serverless) client — implements the same ONYX.api interface
   the pages use, but computes everything in-browser with the engine above and
   persists to localStorage. NOTE: client-side accounts are for local/demo use
   only — passwords are obfuscated, not securely hashed. For real multi-user
   auth, deploy the Node API (server/) instead. ===== */

var slipTypes = {};
Object.keys(SLIP_MAP).forEach(function (k) { slipTypes[k] = SLIP_MAP[k].label; });

var LS = {
  get: function (k, d) { try { var v = JSON.parse(localStorage.getItem(k)); return v == null ? d : v; } catch (_) { return d; } },
  set: function (k, v) { localStorage.setItem(k, JSON.stringify(v)); },
};
function usersDB() { return LS.get('onyx_users', {}); }
function saveUsers(u) { LS.set('onyx_users', u); }
function emailIndex() { return LS.get('onyx_email', {}); }
function newId() { return 'u_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }
function publicUser(u) { return { id: u.id, email: u.email, name: u.name, createdAt: u.createdAt }; }
function currentUser() { var t = localStorage.getItem('onyx_token'); if (!t) return null; return usersDB()[t] || null; }

function localApi(path, opts) {
  opts = opts || {};
  var method = opts.method || 'GET';
  var body = opts.body || {};

  if (path === '/meta') return { provinces: PROVINCE_NAMES, slipTypes: slipTypes, years: (typeof AVAILABLE_YEARS !== 'undefined' ? AVAILABLE_YEARS : [2024]), year: 2024, engineVersion: (typeof ENGINE_VERSION !== 'undefined' ? ENGINE_VERSION : '1.0.0') };
  if (path === '/verify') return selfCheck();

  if (path === '/auth/register') {
    var email = (body.email || '').toLowerCase().trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error('Enter a valid email address.');
    if (!body.password || body.password.length < 8) throw new Error('Password must be at least 8 characters.');
    var users = usersDB(), idx = emailIndex();
    if (idx[email]) throw new Error('An account with that email already exists.');
    var u = { id: newId(), email: email, name: body.name || email.split('@')[0], pwd: btoa(body.password),
      createdAt: new Date().toISOString(), profile: { province: 'ON', year: 2024, maritalStatus: 'single' }, documents: [], audit: null };
    users[u.id] = u; idx[email] = u.id; saveUsers(users); LS.set('onyx_email', idx);
    return { token: u.id, user: publicUser(u) };
  }
  if (path === '/auth/login') {
    var em = (body.email || '').toLowerCase().trim(), ix = emailIndex(), us = usersDB();
    var user = ix[em] ? us[ix[em]] : null;
    if (!user || user.pwd !== btoa(body.password || '')) throw new Error('Incorrect email or password.');
    return { token: user.id, user: publicUser(user) };
  }

  var me = currentUser();
  if (!me) throw new Error('Not signed in.');
  var save = function () { var u = usersDB(); u[me.id] = me; saveUsers(u); };

  if (path === '/me') return { user: publicUser(me) };
  if (path === '/profile' && method === 'GET') return { profile: me.profile };
  if (path === '/profile' && method === 'PUT') {
    ['province','year','age','maritalStatus','spouseNetIncome','dependants','isStudent','disability','firstTimeHomeBuyer','ownsHome','rrspRoom','tfsaRoom','employmentType','hasInvestments','hasRentalIncome','hasForeignIncome','hasCrypto']
      .forEach(function (f) { if (f in body) me.profile[f] = body[f]; });
    save(); return { profile: me.profile };
  }
  if (path === '/documents' && method === 'GET') return { documents: me.documents };
  if (path === '/documents' && method === 'POST') {
    var type = (body.type || '').toUpperCase();
    if (!SLIP_MAP[type]) throw new Error('Unknown document type.');
    if (!body.fields && !body.text) throw new Error('Provide either extracted fields or document text.');
    var scan = scanDocuments([{ type: type, fields: body.fields, text: body.text, name: body.name }]);
    var rec = { id: newId(), uploadedAt: new Date().toISOString(), type: type, fields: body.fields, text: body.text, name: body.name, scan: scan.documents[0] };
    me.documents.push(rec); save(); return { document: rec };
  }
  if (path.indexOf('/documents/') === 0 && method === 'DELETE') {
    var did = path.split('/')[2], n = me.documents.length;
    me.documents = me.documents.filter(function (d) { return d.id !== did; }); save();
    return { ok: me.documents.length < n };
  }
  if (path === '/audit' && method === 'POST') {
    var docs = me.documents.map(function (d) { return { type: d.type, fields: d.fields, text: d.text, name: d.name }; });
    var audit = runAudit({ profile: me.profile, documents: docs, year: me.profile.year });
    me.audit = audit; save(); return { audit: audit };
  }
  if (path === '/audit' && method === 'GET') return { audit: me.audit || null };

  if (path === '/simulate' && method === 'POST') {
    var docs = me.documents.map(function (d) { return { type: d.type, fields: d.fields, text: d.text }; });
    var result = simulate({ profile: me.profile, documents: docs, year: me.profile.year, overrides: (body && body.overrides) || {} });
    return { result: result };
  }
  if (path === '/export' && method === 'GET') {
    return { exportedAt: new Date().toISOString(), account: publicUser(me), profile: me.profile, documents: me.documents, audit: me.audit };
  }
  if (path === '/account' && method === 'DELETE') {
    var users = usersDB(), idx = emailIndex();
    delete users[me.id]; delete idx[me.email];
    saveUsers(users); LS.set('onyx_email', idx);
    return { ok: true };
  }

  throw new Error('Unknown route: ' + path);
}

window.ONYX = {
  token: function () { return localStorage.getItem('onyx_token'); },
  user: function () { try { return JSON.parse(localStorage.getItem('onyx_user')); } catch (_) { return null; } },
  setAuth: function (t, u) { localStorage.setItem('onyx_token', t); localStorage.setItem('onyx_user', JSON.stringify(u)); },
  signOut: function () { localStorage.removeItem('onyx_token'); localStorage.removeItem('onyx_user'); location.href = 'login.html'; },
  api: function (path, opts) {
    return new Promise(function (resolve, reject) {
      try { resolve(localApi(path, opts || {})); } catch (e) { reject(e); }
    });
  },
  requireAuth: function () { if (!this.token()) location.href = 'login.html'; },
  money: function (n) { return (n < 0 ? '-$' : '$') + Math.abs(Math.round(n)).toLocaleString('en-CA'); },
  pctStr: function (n) { return (n * 100).toFixed(1) + '%'; },
};
