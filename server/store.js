/**
 * Data store with two interchangeable backends (all methods async):
 *   - FileStore  : JSON file on disk — used for local `npm start` / tests.
 *   - BlobStore  : Netlify Blobs     — used when running as a Netlify Function
 *                  (detected via AWS_LAMBDA_FUNCTION_NAME, or STORE=blobs).
 *
 * The server only ever awaits these methods, so swapping the backend (or later
 * moving to Postgres) needs no route changes.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const id = () => crypto.randomUUID();
const useBlobs = !!process.env.AWS_LAMBDA_FUNCTION_NAME || process.env.STORE === 'blobs';

/* ------------------------------------------------------------------ */
/* File backend (local dev / tests)                                    */
/* ------------------------------------------------------------------ */
function FileStore() {
  const DB_PATH = path.join(__dirname, 'data', 'db.json');
  let db;
  try { db = JSON.parse(fs.readFileSync(DB_PATH, 'utf8')); }
  catch (_) { db = { secret: crypto.randomBytes(32).toString('hex'), users: {} }; }
  if (!db.secret) db.secret = crypto.randomBytes(32).toString('hex');

  let timer = null;
  const persist = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
      fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
    }, 50);
  };

  return {
    async getSecret() { return process.env.JWT_SECRET || db.secret; },
    async findUserByEmail(email) { return Object.values(db.users).find((u) => u.email === email.toLowerCase()) || null; },
    async getUser(uid) { return db.users[uid] || null; },
    async createUser({ email, name, passwordHash }) {
      const uid = id();
      db.users[uid] = newUser(uid, email, name, passwordHash);
      persist();
      return db.users[uid];
    },
    async updateProfile(uid, patch) {
      const u = db.users[uid]; if (!u) return null;
      u.profile = Object.assign({}, u.profile, patch); persist(); return u.profile;
    },
    async addDocument(uid, doc) {
      const u = db.users[uid]; if (!u) return null;
      const rec = Object.assign({ id: id(), uploadedAt: new Date().toISOString() }, doc);
      u.documents.push(rec); persist(); return rec;
    },
    async listDocuments(uid) { const u = db.users[uid]; return u ? u.documents : []; },
    async deleteDocument(uid, docId) {
      const u = db.users[uid]; if (!u) return false;
      const n = u.documents.length; u.documents = u.documents.filter((d) => d.id !== docId); persist();
      return u.documents.length < n;
    },
    async saveAudit(uid, audit) { const u = db.users[uid]; if (!u) return null; u.audit = audit; persist(); return audit; },
    async getAudit(uid) { const u = db.users[uid]; return u ? u.audit : null; },
    async deleteUser(uid) { delete db.users[uid]; persist(); return true; },
  };
}

/* ------------------------------------------------------------------ */
/* Netlify Blobs backend (serverless)                                  */
/* ------------------------------------------------------------------ */
function BlobStore() {
  const { getStore } = require('@netlify/blobs');
  const store = getStore('onyx-ledger');
  const K = { user: (uid) => `user:${uid}`, email: (e) => `email:${e.toLowerCase()}`, secret: 'config:secret' };

  const getUser = (uid) => store.get(K.user(uid), { type: 'json' });
  const putUser = (u) => store.setJSON(K.user(u.id), u);

  return {
    async getSecret() {
      if (process.env.JWT_SECRET) return process.env.JWT_SECRET;
      let s = await store.get(K.secret);
      if (!s) { s = crypto.randomBytes(32).toString('hex'); await store.set(K.secret, s); }
      return s;
    },
    async findUserByEmail(email) {
      const uid = await store.get(K.email(email));
      return uid ? getUser(uid) : null;
    },
    async getUser(uid) { return getUser(uid); },
    async createUser({ email, name, passwordHash }) {
      const u = newUser(id(), email, name, passwordHash);
      await putUser(u);
      await store.set(K.email(email), u.id);
      return u;
    },
    async updateProfile(uid, patch) {
      const u = await getUser(uid); if (!u) return null;
      u.profile = Object.assign({}, u.profile, patch); await putUser(u); return u.profile;
    },
    async addDocument(uid, doc) {
      const u = await getUser(uid); if (!u) return null;
      const rec = Object.assign({ id: id(), uploadedAt: new Date().toISOString() }, doc);
      u.documents.push(rec); await putUser(u); return rec;
    },
    async listDocuments(uid) { const u = await getUser(uid); return u ? u.documents : []; },
    async deleteDocument(uid, docId) {
      const u = await getUser(uid); if (!u) return false;
      const n = u.documents.length; u.documents = u.documents.filter((d) => d.id !== docId); await putUser(u);
      return u.documents.length < n;
    },
    async saveAudit(uid, audit) { const u = await getUser(uid); if (!u) return null; u.audit = audit; await putUser(u); return audit; },
    async getAudit(uid) { const u = await getUser(uid); return u ? u.audit : null; },
    async deleteUser(uid) { const u = await getUser(uid); if (u) { await store.delete(K.user(uid)); await store.delete(K.email(u.email)); } return true; },
  };
}

function newUser(uid, email, name, passwordHash) {
  return {
    id: uid, email: email.toLowerCase(), name: name || email.split('@')[0], passwordHash,
    createdAt: new Date().toISOString(),
    profile: { province: 'ON', year: 2024, maritalStatus: 'single' },
    documents: [], audit: null,
  };
}

module.exports = useBlobs ? BlobStore() : FileStore();
