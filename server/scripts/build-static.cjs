/**
 * Builds the static (serverless) site into ../../static-site.
 * Bundles the tested engine (../engine/*) + the localStorage client
 * (onyx-client.js) into app.js, and copies the frontend with a favicon.
 * Run: node server/scripts/build-static.cjs
 */
const fs = require('fs');
const path = require('path');
const SRC = path.join(__dirname, '..');                 // server/
const OUT = path.join(__dirname, '..', '..', 'static-site');
const ICON = `<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%230C0A08'/%3E%3Cpath d='M8 3l4 3-4 7-4-7z' fill='%23E3BE86'/%3E%3C/svg%3E" />`;

fs.mkdirSync(OUT, { recursive: true });
const order = ['taxData','taxEngine','extract','scoring','advisory','checklist','planner','verify','assurance','index'];
let engine = '';
for (const m of order) {
  const lines = fs.readFileSync(path.join(SRC, 'engine', m + '.js'), 'utf8').split('\n');
  const out = [];
  for (const line of lines) {
    if (/^\s*'use strict';\s*$/.test(line)) continue;
    if (/require\(/.test(line)) continue;
    if (/^module\.exports/.test(line)) break;
    out.push(line);
  }
  engine += `\n/* ===== engine/${m}.js ===== */\n` + out.join('\n') + '\n';
}
const client = fs.readFileSync(path.join(__dirname, 'onyx-client.js'), 'utf8');
fs.writeFileSync(path.join(OUT, 'app.js'),
`/* ONYX Ledger — static build (engine runs in the browser; data in localStorage). */
(function(){
'use strict';
${engine}
${client}
})();
`);
const pages = ['index.html','app.html','copilot.html','tax-health-score.html','signup.html','login.html','dashboard.html','documents.html','about.html','privacy.html','methodology.html'];
for (const f of pages) {
  let src = fs.readFileSync(path.join(SRC, 'public', f), 'utf8');
  if (!src.includes('rel="icon"')) src = src.replace('</head>', '  ' + ICON + '\n</head>');
  fs.writeFileSync(path.join(OUT, f), src);
}
fs.copyFileSync(path.join(SRC, 'public', 'onyx.css'), path.join(OUT, 'onyx.css'));

// Security & hygiene signals (help legitimacy; served by static hosts like Netlify).
fs.writeFileSync(path.join(OUT, 'robots.txt'), 'User-agent: *\nAllow: /\n');
fs.writeFileSync(path.join(OUT, '_headers'),
  '/*\n' +
  '  X-Content-Type-Options: nosniff\n' +
  '  X-Frame-Options: SAMEORIGIN\n' +
  '  Referrer-Policy: no-referrer\n' +
  '  Permissions-Policy: geolocation=(), microphone=(), camera=()\n');
// Google Search Console site-verification file (safe to keep public).
fs.writeFileSync(path.join(OUT, 'google1fa21b3bb8fd787b.html'), 'google-site-verification: google1fa21b3bb8fd787b.html\n');
fs.mkdirSync(path.join(OUT, '.well-known'), { recursive: true });
fs.writeFileSync(path.join(OUT, '.well-known', 'security.txt'),
  'Contact: mailto:security@onyxledger.ca\n' +
  'Expires: 2027-01-01T00:00:00.000Z\n' +
  'Preferred-Languages: en\n' +
  'Policy: https://onyxledger.ca/privacy.html\n');
console.log('static-site built:', pages.length, 'pages + app.js + onyx.css + robots/_headers/security.txt');
