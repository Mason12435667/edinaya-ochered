'use strict';

// Исправление отправки медиа в whatsapp-web.js, применяется при `npm install` и `npm ci` (postinstall).
//
// MediaData может содержать скрытое перечисляемое поле __x_id. Когда встроенный метод sendMessage
// разворачивает эту модель в исходящий объект Msg, __x_id может перезаписать настоящий ключ
// сообщения, и WhatsApp выдаёт ошибку:
// «Data passed to getter must include an id property ... got undefined».
//
// Скрипт можно запускать повторно, он один раз сохраняет копию рядом с Utils.js. Установка
// останавливается при незнакомом устройстве библиотеки, поэтому версия whatsapp-web.js закреплена
// точно в package.json: при обновлении библиотеки проверьте это исправление.

const fs = require('fs');
const path = require('path');

const MARKER = 'QUEUE_WWEBJS_MEDIA_ID_FIX';

function fail(message) {
  console.error(`[MEDIA PATCH] ${message}`);
  process.exit(2);
}

function packageRoot() {
  let pkg;
  try {
    pkg = require.resolve('whatsapp-web.js/package.json', { paths: [__dirname] });
  } catch (error) {
    fail(`whatsapp-web.js is not installed: ${error.message || error}`);
  }
  return path.dirname(pkg);
}

function findMatchingBrace(source, openIndex) {
  let depth = 0;
  let quote = '';
  let escaped = false;
  let lineComment = false;
  let blockComment = false;

  for (let i = openIndex; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1] || '';

    if (lineComment) {
      if (ch === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (ch === '*' && next === '/') {
        blockComment = false;
        i += 1;
      }
      continue;
    }
    if (quote) {
      if (escaped) {
        escaped = false;
        continue;
      }
      if (ch === '\\') {
        escaped = true;
        continue;
      }
      if (ch === quote) quote = '';
      continue;
    }

    if (ch === '/' && next === '/') {
      lineComment = true;
      i += 1;
      continue;
    }
    if (ch === '/' && next === '*') {
      blockComment = true;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch;
      continue;
    }
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

const root = packageRoot();
const target = path.join(root, 'src', 'util', 'Injected', 'Utils.js');
if (!fs.existsSync(target)) fail(`Utils.js not found: ${target}`);

let source = fs.readFileSync(target, 'utf8');
if (source.includes(MARKER)) {
  console.log('[MEDIA PATCH] already applied');
  process.exit(0);
}

const mediaIndex = source.indexOf('...mediaOptions');
if (mediaIndex < 0) fail('mediaOptions spread was not found; refusing to patch an unknown whatsapp-web.js layout');

let objectStart = source.lastIndexOf('const message = {', mediaIndex);
let declaration = 'const message = {';
if (objectStart < 0) {
  objectStart = source.lastIndexOf('let message = {', mediaIndex);
  declaration = 'let message = {';
}
if (objectStart < 0) fail('outgoing message object was not found');

const openBrace = objectStart + declaration.lastIndexOf('{');
const closeBrace = findMatchingBrace(source, openBrace);
if (closeBrace < 0) fail('could not locate the end of the outgoing message object');

const objectText = source.slice(objectStart, closeBrace + 1);
if (!objectText.includes('newMsgKey') || !objectText.includes('mediaOptions')) {
  fail('candidate object does not look like whatsapp-web.js outgoing message construction');
}

let semicolonEnd = closeBrace + 1;
while (/\s/.test(source[semicolonEnd] || '')) semicolonEnd += 1;
if (source[semicolonEnd] === ';') semicolonEnd += 1;

const lineStart = source.lastIndexOf('\n', objectStart) + 1;
const indentMatch = source.slice(lineStart, objectStart).match(/^\s*/);
const indent = indentMatch ? indentMatch[0] : '';
const insertion = `\n${indent}// ${MARKER}: drop MediaData private id before Msg initialization.\n${indent}delete message.__x_id;`;

const backup = `${target}.orig`;
if (!fs.existsSync(backup)) fs.copyFileSync(target, backup);
source = source.slice(0, semicolonEnd) + insertion + source.slice(semicolonEnd);
fs.writeFileSync(target, source, 'utf8');

const verify = fs.readFileSync(target, 'utf8');
if (!verify.includes(MARKER) || !verify.includes('delete message.__x_id;')) {
  fail('verification failed after patching');
}

console.log('[MEDIA PATCH] applied successfully');
