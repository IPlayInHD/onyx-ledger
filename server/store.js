/**
 * Minimal JSON-file data store (users, profiles, documents, audits).
 * Zero native dependencies so it runs anywhere. For production, swap this
 * module for Postgres/Prisma — the rest of the server only uses these methods.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DB_PATH = path.join(__dirname, 'data', 'db.json');

function load() {
  try {
    return JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
  } catch (_) {
    return { secret: crypto.randomBytes(32).toString('hex'), users: {} };
  }
}

let db = load();
if (!db.secret) db.secret = crypto.randomBytes(32).toString('hex');

let saveTimer = null;
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
    fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
  }, 50);
}

const id = () => crypto.randomUUID();

module.exports = {
  secret: () => db.secret,

  findUserByEmail(email) {
    return Object.values(db.users).find((u) => u.email === email.toLowerCase());
  },
  getUser(uid) {
    return db.users[uid] || null;
  },
  createUser({ email, name, passwordHash }) {
    const uid = id();
    db.users[uid] = {
      id: uid,
      email: email.toLowerCase(),
      name: name || email.split('@')[0],
      passwordHash,
      createdAt: new Date().toISOString(),
      profile: { province: 'ON', year: 2024, maritalStatus: 'single' },
      documents: [],
      audit: null,
    };
    persist();
    return db.users[uid];
  },

  updateProfile(uid, patch) {
    const u = db.users[uid];
    if (!u) return null;
    u.profile = Object.assign({}, u.profile, patch);
    persist();
    return u.profile;
  },

  addDocument(uid, doc) {
    const u = db.users[uid];
    if (!u) return null;
    const record = Object.assign({ id: id(), uploadedAt: new Date().toISOString() }, doc);
    u.documents.push(record);
    persist();
    return record;
  },
  listDocuments(uid) {
    const u = db.users[uid];
    return u ? u.documents : [];
  },
  deleteDocument(uid, docId) {
    const u = db.users[uid];
    if (!u) return false;
    const before = u.documents.length;
    u.documents = u.documents.filter((d) => d.id !== docId);
    persist();
    return u.documents.length < before;
  },

  saveAudit(uid, audit) {
    const u = db.users[uid];
    if (!u) return null;
    u.audit = audit;
    persist();
    return audit;
  },
  getAudit(uid) {
    const u = db.users[uid];
    return u ? u.audit : null;
  },
};
