/**
 * Netlify Function wrapper — runs the existing Express API serverlessly.
 * The /api/* redirect in netlify.toml routes all API traffic here.
 */
'use strict';

const serverless = require('serverless-http');
const app = require('../../server.js');

const handler = serverless(app);
const FN_BASE = '/.netlify/functions/api';

exports.handler = async (event, context) => {
  // Normalize the path so Express always sees the original /api/... route,
  // whether Netlify delivers it rewritten or via the function URL.
  if (event.path && event.path.startsWith(FN_BASE)) {
    const rest = event.path.slice(FN_BASE.length);
    event.path = '/api' + (rest && rest !== '/' ? rest : '/');
  }
  return handler(event, context);
};
