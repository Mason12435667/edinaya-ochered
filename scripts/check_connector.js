'use strict';
// Проверяет, что все модули, которые подключает коннектор, загружаются. WhatsApp при этом не запускается.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');

const CONNECTOR_DIR = path.join(__dirname, '..', 'connector');
const ENTRY = path.join(CONNECTOR_DIR, 'whatsapp_qr_connector.js');

// Части коннектора при загрузке создают клиент WhatsApp, поэтому читаются только их зависимости.
function dependencies() {
  const found = new Set();
  const parts = fs.readdirSync(CONNECTOR_DIR).filter((name) => name === path.basename(ENTRY) || name.startsWith('connector_'));
  for (const part of parts) {
    const source = fs.readFileSync(path.join(CONNECTOR_DIR, part), 'utf8');
    // window.require('WAWeb…') выполняется на странице WhatsApp Web, а не в Node
    for (const match of source.matchAll(/(?<![\w.])require\(\s*['"]([^'"]+)['"]\s*\)/g)) found.add(match[1]);
  }
  return [...found].filter((name) => !name.startsWith('./connector_'));
}

function check() {
  const localRequire = createRequire(ENTRY);
  const names = dependencies();
  for (const name of names) localRequire(name);
  console.log(`Connector imports OK (${names.length}); no browser or WhatsApp connection started.`);
}

module.exports = {check, dependencies, ENTRY};
if (require.main === module) check();
