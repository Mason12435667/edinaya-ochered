'use strict';
// Files above `threshold` are sent as documents (the caller sets
// sendMediaAsDocument). whatsapp-web.js still needs the file as base64, so the
// limit protects the browser from oversized inline media previews.

const fs = require('fs');
const path = require('path');

const threshold = 16 * 1024 * 1024;

async function prepare(client, mediaPath, MessageMedia, info) {
  const data = await fs.promises.readFile(mediaPath, {encoding: 'base64'});
  const name = String((info && info.name) || path.basename(mediaPath));
  const mime = String((info && info.mime) || 'application/octet-stream');
  const content = new MessageMedia(mime, data, name, Number(info && info.size) || undefined);
  return {content, dispose: () => {}};
}

module.exports = {threshold, prepare};
