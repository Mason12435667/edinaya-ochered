'use strict';
// Проверяет, что все модули, которые подключает коннектор, загружаются. WhatsApp при этом не запускается.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');

function check() {
  const entry = path.join(__dirname, '..', 'connector', 'whatsapp_qr_connector.js');
  const localRequire = createRequire(entry);
  const source = fs.readFileSync(entry, 'utf8');
  const dependencies = new Set([...source.matchAll(/(?<![\w.])require\(['"]([^'"]+)['"]\)/g)].map(m => m[1]));
  for (const dependency of dependencies) localRequire(dependency);
  console.log(`Connector imports OK (${dependencies.size}); no browser or WhatsApp connection started.`);
}

module.exports = {check};
if (require.main === module) check();
