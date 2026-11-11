'use strict';
// Медиа: скачивает вложения сообщений из WhatsApp Web и догружает те, что не удалось получить сразу.

module.exports = {
  applyMediaResult, attachMediaToItem, downloadMediaDirectById, retryPendingMedia,
};

const {
  runtime, backgroundWorkAllowed, client, isRememberedInternalOutgoing, locallyRecoveredMediaIds,
  messageText, normalizeMessageIdObject, noteBrowserPressure, outgoingMediaProbe,
  pendingMediaMessages, postJson, reconciledOutgoingIds, serializedId,
} = require('./connector_common');
const receiving = require('./connector_receive');

let directMediaEvaluation = null;
// The official WWebJS media downloader can also outlive a Promise.race timeout.
// Bound its actual in-flight promises rather than just waiting callers.
let officialMediaDownloads = 0;
const directMediaReasonLoggedAt = new Map();
function reportDirectMediaFailure(reason) {
  const label = String(reason || 'unknown').replace(/[\r\n]/g, ' ').slice(0, 110);
  const now = Date.now();
  if (now - Number(directMediaReasonLoggedAt.get(label) || 0) < 60000) return;
  directMediaReasonLoggedAt.set(label, now);
  if (directMediaReasonLoggedAt.size > 100) for (const [key, at] of directMediaReasonLoggedAt) {
    if (now - at > 3600000) directMediaReasonLoggedAt.delete(key);
  }
  console.warn('[media116] direct recovery:', label);
}
async function downloadMediaDirectById(messageId, timeoutMs = 8000) {
  const mid = String(messageId || '').trim();
  if (!mid || directMediaEvaluation || runtime.shutdownStarted || !client.pupPage) return null;
  let evaluation = null;
  try {
    evaluation = client.pupPage.evaluate(async (msgId) => {
        let phase = 'collection';
        try {
          let Msg = null;
          try { Msg = window.require('WAWebCollections')?.Msg || null; } catch (_) {}
          if (!Msg) Msg = (window.Store && (window.Store.Msg || window.Store.Messages)) || null;
          if (!Msg) return {__media_error:'collection-unavailable'};
          const idCandidates = [String(msgId)];
          // For LID messages the browser model may require the structured Wid
          // object. Rebuild it from the stable serialized form as well.
          // false_<chat@g.us>_<stanza>_<sender@lid> is a four-part key.
          // Old three-part parser failed on all group messages with participants.
          const parsed = String(msgId).match(/^(true|false)_([^_]+)_([^_]+)(?:_(.+))?$/);
          if (parsed) {
            idCandidates.push({
              fromMe: parsed[1] === 'true', remote: parsed[2], id: parsed[3],
              participant: parsed[4] || undefined,
              $1: String(msgId), _serialized: String(msgId),
            });
            idCandidates.push(parsed[3]);
          }
          let msg = null;
          for (const candidate of idCandidates) {
            try { msg = typeof Msg.get === 'function' ? Msg.get(candidate) : null; } catch (_) {}
            if (msg) break;
          }
          if (!msg && typeof Msg.getMessagesById === 'function') {
            for (const candidate of idCandidates) {
              try {
                msg = (await Msg.getMessagesById([candidate]))?.messages?.[0] || null;
              } catch (_) {}
              if (msg) break;
            }
          }
          if (!msg) {
            let models = [];
            try {
              models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() :
                (Array.isArray(Msg.models) ? Msg.models : (Array.isArray(Msg._models) ? Msg._models : []));
            } catch (_) {}
            const wanted = String(msgId);
            msg = (Array.isArray(models) ? models : []).find((candidate) => {
              const id = candidate && candidate.id;
              return Boolean(id && (id._serialized === wanted || id.$1 === wanted ||
                (id.fromMe !== undefined && id.remote && id.id &&
                 `${Boolean(id.fromMe)}_${id.remote}_${id.id}` === wanted)));
            }) || null;
          }
          if (!msg) return {__media_error:'message-model-not-in-browser-cache'};

          // WhatsApp часто создаёт событие раньше, чем файл полностью готов.
          // Просим Web-клиент принудительно подготовить медиа, как это делает
          // whatsapp-web.js внутри Message.downloadMedia().
          if (msg.mediaData && msg.mediaData.mediaStage !== 'RESOLVED' && typeof msg.downloadMedia === 'function') {
            try {
              await Promise.race([
                msg.downloadMedia({ downloadEvenIfExpensive: true, rmrReason: 1 }),
                new Promise((resolve) => setTimeout(resolve, 3500)),
              ]);
            } catch (_) {}
          }
          // Даже при REUPLOADING не выходим сразу: directPath/mediaKey уже могут
          // быть доступны и файл можно забрать напрямую. Если ещё рано, очередь
          // догрузки повторит попытку через несколько секунд.

          // Official WhatsApp Web download helper may be present on newer builds.
          // It returns an actual media payload, not the low-resolution body thumbnail.
          if (window.WWebJS && typeof window.WWebJS.downloadMedia === 'function') {
            try {
              const original = await window.WWebJS.downloadMedia(msg);
              if (original && typeof original.data === 'string' && original.data.length > 100) {
                return {
                  data: original.data, mimetype: original.mimetype || msg.mimetype || '',
                  filename: original.filename || msg.filename || '', filesize: original.filesize || msg.size || 0,
                };
              }
              if (typeof original === 'string' && original.length > 100) {
                return {data:original, mimetype:msg.mimetype || '',filename:msg.filename || '',filesize:msg.size || 0};
              }
            } catch (_) {}
          }

          // На новых сборках библиотеки уже может быть готовый resolveMediaBlob.
          if (window.WWebJS && typeof window.WWebJS.resolveMediaBlob === 'function') {
            try {
              const resolved = await window.WWebJS.resolveMediaBlob(msgId);
              if (resolved && resolved.blob) {
                const data = await window.WWebJS.arrayBufferToBase64Async(await resolved.blob.arrayBuffer());
                return {
                  data,
                  mimetype: resolved.mimetype || msg.mimetype || '',
                  filename: resolved.filename || msg.filename || '',
                  filesize: resolved.filesize || msg.size || 0,
                };
              }
            } catch (_) {}
          }

          const directPath = msg.directPath || (msg.mediaData && msg.mediaData.directPath) || '';
          const mediaKey = msg.mediaKey || (msg.mediaData && msg.mediaData.mediaKey);
          if (!directPath || !mediaKey) return {__media_error:'original-url-or-key-not-ready'};
          const mockQpl = {
            addAnnotations() { return this; },
            addPoint() { return this; },
            end() { return this; },
          };
          phase = 'download-manager';
          let managerModule = null;
          try { managerModule = window.require('WAWebDownloadManager'); } catch (_) {}
          if (!managerModule) managerModule = window.Store && window.Store.DownloadManager;
          const manager = managerModule && (managerModule.downloadManager || managerModule);
          if (!manager || typeof manager.downloadAndMaybeDecrypt !== 'function')
            return {__media_error:'download-manager-unavailable'};
          const declaredMime = String(msg.mimetype || (msg.mediaData && msg.mediaData.mimetype) || '').trim().toLowerCase();
          const mediaType = String(msg.type || '').toLowerCase();
          const fallbackMimes = mediaType === 'ptt'
            ? ['audio/ogg; codecs=opus', 'audio/ogg']
            : mediaType === 'audio'
              ? ['audio/ogg; codecs=opus', 'audio/ogg', 'audio/mpeg', 'audio/mp4']
              : mediaType === 'image'
                ? ['image/jpeg', 'image/png', 'image/webp']
                : mediaType === 'video'
                  ? ['video/mp4', 'video/webm']
                  : mediaType === 'sticker' ? ['image/webp', 'image/png'] : [];
          const mimeCandidates = declaredMime && declaredMime !== 'application/octet-stream'
            ? [declaredMime, ...fallbackMimes]
            : fallbackMimes.length ? fallbackMimes : ['application/octet-stream'];
          let decryptedMedia = null;
          let lastMimeError = null;
          for (const candidateMime of [...new Set(mimeCandidates)]) {
            try {
              decryptedMedia = await manager.downloadAndMaybeDecrypt({
                directPath,
                encFilehash: msg.encFilehash || (msg.mediaData && msg.mediaData.encFilehash),
                filehash: msg.filehash || (msg.mediaData && msg.mediaData.filehash),
                mediaKey,
                mediaKeyTimestamp: msg.mediaKeyTimestamp || (msg.mediaData && msg.mediaData.mediaKeyTimestamp),
                type: msg.type,
                mimetype: candidateMime,
                signal: new AbortController().signal,
                downloadQpl: mockQpl,
              });
              if (decryptedMedia) break;
            } catch (error) {
              lastMimeError = error;
              if (!/unexpected mimetype/i.test(String(error && (error.message || error.name) || ''))) throw error;
            }
          }
          if (!decryptedMedia && lastMimeError) throw lastMimeError;
          if (!decryptedMedia) return {__media_error:'decrypt-returned-empty'};
          const data = await window.WWebJS.arrayBufferToBase64Async(decryptedMedia);
          return {
            data,
            mimetype: msg.mimetype || (msg.mediaData && msg.mediaData.mimetype) || '',
            filename: msg.filename || (msg.mediaData && msg.mediaData.filename) || '',
            filesize: msg.size || (msg.mediaData && msg.mediaData.size) || 0,
          };
        } catch (error) {
          // Do not print message IDs or decrypted bytes to service logs.
          return {__media_error:phase + ':' + String(error && (error.message || error.name) || 'failure').slice(0, 70)};
        }
      }, mid);
    directMediaEvaluation = evaluation;
    // An outstanding Chromium evaluation remains guarded after timeout until
    // it actually settles; subsequent media retries use the official API only.
    evaluation.then(() => {
      if (directMediaEvaluation === evaluation) directMediaEvaluation = null;
    }, () => {
      if (directMediaEvaluation === evaluation) directMediaEvaluation = null;
    });
    const result = await Promise.race([
      evaluation,
      new Promise((resolve) => setTimeout(() => resolve({__media_error:'direct-evaluation-timeout'}), timeoutMs)),
    ]);
    if (result && result.__media_error) {
      reportDirectMediaFailure(result.__media_error);
      return null;
    }
    return result || null;
  } catch (error) {
    reportDirectMediaFailure('puppeteer:' + String(error && (error.message || error.name) || 'failure'));
    return null;
  }
}



function applyMediaResult(item, media, message) {
  if (!media || !media.data) return false;
  const bytes = Buffer.byteLength(String(media.data), 'base64');
  item.media_name = String(media.filename || `WhatsApp ${(message && message.type) || 'media'}`).slice(0, 180);
  item.media_mime = String(media.mimetype || '').slice(0, 120);
  if (!bytes) return false;
  if (bytes > 512 * 1024 * 1024) {
    item.media_too_large = true;
    console.warn(`Вложение WhatsApp слишком большое для панели: ${Math.round(bytes / 1024 / 1024)} МБ`);
    return true;
  }
  item.media_base64 = String(media.data);
  item.media_pending = false;
  return true;
}

async function attachMediaToItem(message, item) {
  if (!message) return item;
  const normalizedMessageId = normalizeMessageIdObject(message);
  const raw = message._data || {};
  const mediaExpected = Boolean(
    message.hasMedia || raw.directPath || raw.mediaKey || raw.mediaData ||
    ['image', 'video', 'audio', 'ptt', 'document', 'sticker'].includes(String(message.type || ''))
  );
  if (!mediaExpected) return item;
  const mid = normalizedMessageId || serializedId(message && message.id);
  if (mid) {
    const recoveredUntil = Number(locallyRecoveredMediaIds.get(String(mid)) || 0);
    if (recoveredUntil > Date.now()) {
      item.media_pending = false;
      item.media_name = String(item.media_name || `WhatsApp ${message.type || 'media'}`).slice(0, 180);
      return item;
    }
    if (recoveredUntil) locallyRecoveredMediaIds.delete(String(mid));
  }
  let lastError = null;
  let officialTimedOut = false;
  if (typeof message.downloadMedia === 'function') {
    if (officialMediaDownloads >= 2) {
      officialTimedOut = true;
    } else {
      officialMediaDownloads += 1;
      const official = Promise.resolve().then(() => message.downloadMedia());
      const release = () => { officialMediaDownloads = Math.max(0, officialMediaDownloads - 1); };
      official.then(release, release);
      try {
        const timeoutMarker = {__media_timeout:true};
        const media = await Promise.race([
          official,
          new Promise(resolve => setTimeout(() => resolve(timeoutMarker), 6500)),
        ]);
        if (media === timeoutMarker) {
          officialTimedOut = true;
          reportDirectMediaFailure('official-evaluation-timeout');
        } else if (applyMediaResult(item, media, message)) {
          return item;
        }
      } catch (error) {
        lastError = error;
        reportDirectMediaFailure('official:' + String(error && (error.message || error.name) || 'failure'));
      }
    }
  }
  // A direct download is serialized and attempted once per cycle. If the official
  // Chromium call is still running, defer to the retry queue instead of piling on.
  if (!officialTimedOut && mid) {
    const direct = await downloadMediaDirectById(mid);
    if (applyMediaResult(item, direct, message)) return item;
  }
  item.media_name = String(item.media_name || `WhatsApp ${message.type || 'media'}`).slice(0, 180);
  item.media_pending = true;
  console.warn(
    `Вложение WhatsApp поставлено на догрузку: ${mid || message.type || 'media'}`,
    lastError && (lastError.message || lastError) || ''
  );
  return item;
}

async function retryPendingMedia() {
  if (runtime.pendingMediaBusy || !pendingMediaMessages.size || !backgroundWorkAllowed()) return;
  runtime.pendingMediaBusy = true;
  try {
    const now = Date.now();
    const entries = [...pendingMediaMessages.entries()]
      .filter(([, meta]) => Number(meta.next_at || 0) <= now)
      .slice(0, 1);

    for (const [messageId, meta] of entries) {
      try {
        const locallyRecoveredUntil = Number(locallyRecoveredMediaIds.get(String(messageId)) || 0);
        if (locallyRecoveredUntil > Date.now()) {
          pendingMediaMessages.delete(messageId);
          outgoingMediaProbe.delete(messageId);
          continue;
        }
        if (locallyRecoveredUntil) locallyRecoveredMediaIds.delete(String(messageId));
        if (isRememberedInternalOutgoing(messageId)) {
          pendingMediaMessages.delete(messageId);
          outgoingMediaProbe.delete(messageId);
          continue;
        }

        let message = null;
        try {
          message = await Promise.race([
            client.getMessageById(messageId),
            new Promise((resolve) => setTimeout(() => resolve(null), 1800)),
          ]);
        } catch (_) {}

        const item = message
          ? receiving.liveMessageItem(message, Boolean(meta.from_me), messageText(message), Boolean(meta.notify))
          : {
              id: String(messageId),
              from_me: Boolean(meta.from_me),
              sender: String(meta.sender || ''),
              body: String(meta.body || '[Вложение]'),
              type: String(meta.type || 'media'),
              timestamp: Number(meta.timestamp || Math.floor(Date.now() / 1000)),
              ack: 0,
              notify: Boolean(meta.notify),
              forwarded: Boolean(meta.forwarded),
            };
        item.forwarded = Boolean(item.forwarded || meta.forwarded);
        item.sender = String(meta.sender || item.sender || '');
        item.sender_phone = String(meta.sender_phone || item.sender_phone || '');
        item.sender_id = String(meta.sender_id || item.sender_id || '');

        if (message) {
          await attachMediaToItem(message, item);
        } else {
          const direct = await downloadMediaDirectById(messageId);
          applyMediaResult(item, direct, { type: meta.type || 'media' });
        }

        if (item.media_base64) {
          await postJson('/api/chat-messages-sync', {
            chat_id: String(meta.chat_id || ''),
            append: true,
            messages: [item],
          });
          if (item.media_pending) throw new Error('Передача вложения в панель не завершена');
          pendingMediaMessages.delete(messageId);
          if (meta.from_me) reconciledOutgoingIds.set(messageId, Date.now());
          console.log(`Вложение WhatsApp догружено и сохранено: ${messageId}`);
          continue;
        }

        meta.attempts = Number(meta.attempts || 0) + 1;
        meta.next_at = now + Math.min(90000, 6000 + meta.attempts * 9000);
        if (meta.attempts >= 12) {
          pendingMediaMessages.delete(messageId);
          console.warn(`Не удалось скачать вложение после ${meta.attempts} попыток: ${messageId}`);
        } else {
          pendingMediaMessages.set(messageId, meta);
        }
      } catch (error) {
        meta.attempts = Number(meta.attempts || 0) + 1;
        meta.next_at = Date.now() + Math.min(90000, 6000 + meta.attempts * 9000);
        noteBrowserPressure(error, 'media recovery');
        if (meta.attempts >= 12) {
          pendingMediaMessages.delete(messageId);
          console.warn('Не удалось догрузить вложение WhatsApp:', error.message || error);
        } else {
          pendingMediaMessages.set(messageId, meta);
        }
      }
    }
  } finally {
    runtime.pendingMediaBusy = false;
  }
}

