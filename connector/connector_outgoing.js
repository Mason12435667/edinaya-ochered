'use strict';
// Исходящие, отправленные с телефона: передача их на сервер и догрузка вложений, которые
// WhatsApp отдал не сразу. Запускается из runFastSync.

module.exports = {
  reconcileRecentOutgoing, retryOutgoingMediaProbe, scheduleOutgoingMediaProbe,
};

const {
  runtime, USER_MESSAGE_TYPES, cachedMessageObject, cleanupOutgoingTracking, client,
  isRememberedForwarded, isRememberedInternalOutgoing, messageAcknowledgements, messageText,
  outgoingMediaProbe, pendingMediaMessages, persistedAcknowledgements, postJson,
  reconciledOutgoingIds, rememberMessageObject, resolveDirectPhoneId, safeWhatsAppBody,
  serializedId,
} = require('./connector_common');
const downloading = require('./connector_media');
const receiving = require('./connector_receive');

let recentOutgoingBusy = false;
let outgoingMediaProbeBusy = false;
// Отдельно отмечаем ручные исходящие, у которых бинарное медиа уже сохранено.
const reconciledOutgoingMediaIds = new Map();

async function reconcileRecentOutgoing() {
  // Резерв для случаев, когда WhatsApp Web не прислал message_create. Проверяем
  // только сообщения, созданные после запуска текущего коннектора, поэтому
  // старую переписку в систему не импортируем. Не допускаем параллельных циклов:
  // накопившиеся evaluate() заметно замедляли WhatsApp Web на слабом сервере.
  if (recentOutgoingBusy) return;
  recentOutgoingBusy = true;
  const now = Date.now();
  try {
    const candidates = await client.pupPage.evaluate((readyAt) => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const Msg = window.require('WAWebCollections').Msg;
        let models = [];
        if (Msg && typeof Msg.getModelsArray === 'function') models = Msg.getModelsArray();
        else if (Msg && Array.isArray(Msg.models)) models = Msg.models;
        const quoteSnapshot = (m) => {
          if (!m) return { quoted_message_key: '', quoted_body: '', quoted_sender: '' };
          const raw = m._data || m;
          const context = raw.contextInfo || raw.context || m.contextInfo || m.context || {};
          const quoted = raw.quotedMsg || raw.quotedMessage || m.quotedMsg || m.quotedMessage || context.quotedMessage || context.quotedMsg || null;
          const quoteId = widText(quoted && quoted.id) || String(
            raw.quotedStanzaID || raw.quotedMessageId || raw.replyToMsgId ||
            m.quotedStanzaID || m.quotedMessageId || m.replyToMsgId ||
            context.stanzaId || context.quotedStanzaID || ''
          ).trim();
          if (!quoted && !quoteId && !context.participant && !raw.quotedParticipant) {
            return { quoted_message_key: '', quoted_body: '', quoted_sender: '' };
          }
          const body = String(
            (quoted && (quoted.body || quoted.caption || quoted.text || quoted.pollName || quoted.eventName)) ||
            (quoted && quoted.msg && (quoted.msg.body || quoted.msg.caption || quoted.msg.text)) || ''
          ).trim();
          const quotedFromMe = Boolean(quoted && ((quoted.id && quoted.id.fromMe) || quoted.fromMe));
          const sender = quotedFromMe
            ? 'Вы'
            : String((quoted && (quoted.notifyName || quoted.pushname || quoted.senderName)) || '').trim() || 'Пользователь';
          return {
            quoted_message_key: quoteId,
            quoted_body: body || (quoteId ? 'Сообщение' : ''),
            quoted_sender: quoteId || quoted ? sender : '',
          };
        };
        const cutoff = Math.max(Number(readyAt || 0), Math.floor(Date.now() / 1000) - 900);
        return (Array.isArray(models) ? models : []).slice(-600).map((m) => ({
          id: widText(m && m.id),
          remote: widText(m && m.id && m.id.remote),
          to: widText(m && m.to),
          from: widText(m && m.from),
          from_me: Boolean(m && m.id && m.id.fromMe),
          type: String((m && m.type) || 'chat'),
          timestamp: Number((m && (m.t || m.timestamp)) || 0),
          body: String((m && (m.body || m.caption || m.pollName || m.eventName)) || ''),
          has_media: Boolean(m && (
            m.hasMedia || m.directPath || m.mediaKey || m.mediaData ||
            m.mimetype || m.clientUrl || m.deprecatedMms3Url || m.filehash || m.encFilehash ||
            Number(m.size || m.mediaSize || 0) > 0 ||
            (m._data && (
              m._data.directPath || m._data.mediaKey || m._data.mediaData ||
              m._data.mimetype || m._data.clientUrl || m._data.deprecatedMms3Url ||
              m._data.filehash || m._data.encFilehash || Number(m._data.size || m._data.mediaSize || 0) > 0
            )) ||
            ['image','video','audio','ptt','document','sticker'].includes(String(m.type || ''))
          )),
          isForwarded: Boolean(m && (m.isForwarded || m.forwarded || (m._data && m._data.isForwarded))),
          forwarded: Boolean(m && (m.forwarded || m.isForwarded)),
          forwardingScore: Number((m && (m.forwardingScore || (m._data && m._data.forwardingScore))) || 0),
          ack: Math.max(0, Math.min(4, Number((m && (m.ack ?? m.__x_ack ?? (m._data && m._data.ack))) || 0))),
          ...quoteSnapshot(m),
        })).filter((m) => m.id && m.from_me && m.timestamp >= cutoff);
      }, runtime.connectorReadyAt);

    let recovered = 0;
    for (const row of (Array.isArray(candidates) ? candidates.slice(-50) : [])) {
      const mid = String(row.id || '').trim();
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      if (!mid) continue;

      // QUEUE_ACK_RECONCILE_3_3_73: message_ack can fire before the local row is
      // committed. In that case the first API update legitimately returns
      // updated=false and the UI can stay on «Отправляется». Read the current
      // ACK from WhatsApp's own message model on every bounded recovery pass
      // and retry persistence until the exact local message exists.
      const modelAck = Math.max(0, Math.min(4, Number(row.ack || 0)));
      const observedAck = Math.max(modelAck, Number(messageAcknowledgements.get(mid) || 0));
      const persistedAck = Number(persistedAcknowledgements.get(mid) || 0);
      if (observedAck > persistedAck) {
        const ackChatId = String(row.remote || row.to || row.from || '').trim();
        if (ackChatId) {
          try {
            const ackResult = await postJson('/api/chat-message-ack', {
              chat_id: ackChatId,
              message_id: mid,
              ack: observedAck,
            });
            if (ackResult && ackResult.updated) {
              persistedAcknowledgements.set(mid, observedAck);
              messageAcknowledgements.set(mid, Math.max(Number(messageAcknowledgements.get(mid) || 0), observedAck));
            }
          } catch (_) {}
        }
      }

      if (reconciledOutgoingIds.has(mid) || isRememberedInternalOutgoing(mid)) continue;
      let rawChatId = String(row.remote || row.to || '').trim();
      if (!rawChatId || rawChatId === 'status@broadcast' || rawChatId.endsWith('@newsletter')) continue;

      let message = null;
      try {
        message = await client.getMessageById(mid);
        if (message) rememberMessageObject(message, rawChatId);
      } catch (_) {}

      if (message && message.fromMe) {
        rawChatId = serializedId(message.to) || serializedId(message.from) || rawChatId;
        if (isRememberedInternalOutgoing(mid)) continue;
        scheduleOutgoingMediaProbe(message, rawChatId);
        if (rawChatId.endsWith('@g.us')) {
          const text = String(messageText(message) || '').trim();
          await receiving.publishLiveMessage(rawChatId, 'Рабочий WhatsApp', message, true, text, false, false).catch(() => {});
        } else {
          const phoneId = rawChatId.endsWith('@c.us') ? rawChatId : await resolveDirectPhoneId(rawChatId);
          const canonicalChatId = phoneId || rawChatId;
          const text = String(messageText(message) || '').trim();
          await receiving.publishLiveMessage(
            canonicalChatId,
            'Рабочий WhatsApp',
            message,
            true,
            text,
            false,
            false
          );
        }
        reconciledOutgoingIds.set(mid, now);
        recovered += 1;
        continue;
      }

      // Даже если whatsapp-web.js не смог собрать объект Message, текстовое
      // сообщение всё равно можно сохранить из внутренней коллекции.
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      if (!rawChatId.endsWith('@g.us')) {
        const phoneId = rawChatId.endsWith('@c.us') ? rawChatId : await resolveDirectPhoneId(rawChatId);
        rawChatId = phoneId || rawChatId;
      }
      // A phone screenshot can appear in Store.Msg a little earlier than its media
      // metadata. Saving/reconciling that empty provisional model loses the image
      // forever, because later sweeps skip the same message id. Leave it pending.
      const provisionalBody = safeWhatsAppBody(row.body);
      if (!row.has_media && !provisionalBody) {
        continue;
      }
      const item = {
        id: mid,
        from_me: true,
        sender: 'Рабочий WhatsApp',
        body: safeWhatsAppBody(row.body) || messageText({ type: row.type, mimetype: row.mimetype }),
        type: String(row.type || 'chat'),
        timestamp: Number(row.timestamp || Math.floor(Date.now() / 1000)),
        ack: Math.max(1, Math.min(4, Number(row.ack || 0))),
        notify: false,
        forwarded: Boolean(
          row.isForwarded ||
          row.forwarded ||
          Number(row.forwardingScore || 0) > 0 ||
          isRememberedForwarded(mid)
        ),
        quoted_message_key: String(row.quoted_message_key || '').trim(),
        quoted_body: String(row.quoted_body || '').trim().slice(0, 1200),
        quoted_sender: String(row.quoted_sender || '').trim().slice(0, 100),
      };
      await postJson('/api/chat-messages-sync', {
        chat_id: rawChatId,
        append: true,
        messages: [item],
      });
      if (row.has_media) {
        console.log(`Исходящее медиа обнаружено резервной синхронизацией: ${mid}; type=${String(row.type || '')}`);
        if (!pendingMediaMessages.has(mid)) {
          pendingMediaMessages.set(mid, {
            chat_id: rawChatId,
            sender: 'Рабочий WhatsApp',
            sender_phone: '',
            sender_id: '',
            from_me: true,
            notify: false,
            forwarded: Boolean(item.forwarded),
            body: String(item.body || messageText({ type: item.type }) || '[Вложение]'),
            type: String(item.type || 'media'),
            timestamp: Number(item.timestamp || Math.floor(Date.now() / 1000)),
            attempts: 0,
            next_at: Date.now() + 800,
          });
        }
      } else {
        reconciledOutgoingIds.set(mid, now);
      }
      recovered += 1;
    }
    if (recovered) console.log(`Резервная синхронизация восстановила исходящих с рабочего WhatsApp: ${recovered}`);
    cleanupOutgoingTracking(now);
  } catch (error) {
    console.warn('Резервная синхронизация исходящих:', error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    recentOutgoingBusy = false;
  }
}


// QUEUE_OUTGOING_MEDIA_PROBE_3_3_63
function scheduleOutgoingMediaProbe(message, rawChatId) {
  const messageId = serializedId(message && message.id);
  const chatId = String(rawChatId || serializedId(message && message.to) || serializedId(message && message.from) || '').trim();
  if (!messageId || !chatId || isRememberedInternalOutgoing(messageId)) return;
  if (reconciledOutgoingMediaIds.has(messageId)) return;

  const previous = outgoingMediaProbe.get(messageId) || {};
  outgoingMediaProbe.set(messageId, {
    chat_id: chatId,
    body: String(messageText(message) || previous.body || '').trim(),
    type: String((message && message.type) || previous.type || 'chat'),
    timestamp: Number((message && message.timestamp) || previous.timestamp || Math.floor(Date.now() / 1000)),
    attempts: Number(previous.attempts || 0),
    next_at: Number(previous.next_at || (Date.now() + 1400)),
  });

  // Keep this queue bounded. It is only for very recent manual outgoing messages.
  if (outgoingMediaProbe.size > 80) {
    const oldest = [...outgoingMediaProbe.entries()]
      .sort((a, b) => Number(a[1]?.timestamp || 0) - Number(b[1]?.timestamp || 0))
      .slice(0, outgoingMediaProbe.size - 80);
    for (const [id] of oldest) outgoingMediaProbe.delete(id);
  }
}

async function outgoingMediaRawState(messageId) {
  const mid = String(messageId || '').trim();
  if (!mid || !client.pupPage) return null;
  try {
    return await Promise.race([
      client.pupPage.evaluate((msgId) => {
        try {
          const Collections = window.require('WAWebCollections');
          const Msg = Collections && Collections.Msg;
          let msg = Msg && typeof Msg.get === 'function' ? Msg.get(msgId) : null;
          if (!msg) return null;
          const data = msg._data || msg;
          return {
            type: String(msg.type || data.type || 'chat'),
            body: String(msg.body || msg.caption || data.body || data.caption || ''),
            has_media: Boolean(
              msg.hasMedia || msg.directPath || msg.mediaKey || msg.mediaData ||
              msg.mimetype || msg.clientUrl || msg.deprecatedMms3Url || msg.filehash || msg.encFilehash ||
              Number(msg.size || msg.mediaSize || 0) > 0 ||
              data.directPath || data.mediaKey || data.mediaData ||
              data.mimetype || data.clientUrl || data.deprecatedMms3Url || data.filehash || data.encFilehash ||
              Number(data.size || data.mediaSize || 0) > 0 ||
              ['image','video','audio','ptt','document','sticker'].includes(String(msg.type || data.type || ''))
            ),
            forwarded: Boolean(
              msg.isForwarded || msg.forwarded || data.isForwarded || data.forwarded ||
              Number(msg.forwardingScore || data.forwardingScore || 0) > 0
            ),
          };
        } catch (_) {
          return null;
        }
      }, mid),
      new Promise((resolve) => setTimeout(() => resolve(null), 1800)),
    ]);
  } catch (_) {
    return null;
  }
}

async function retryOutgoingMediaProbe() {
  if (outgoingMediaProbeBusy || !outgoingMediaProbe.size || runtime.shutdownStarted || !runtime.connectorOperational) return;
  outgoingMediaProbeBusy = true;
  try {
    const now = Date.now();
    const due = [...outgoingMediaProbe.entries()]
      .filter(([, meta]) => Number(meta.next_at || 0) <= now)
      .slice(0, 1);

    for (const [messageId, meta] of due) {
      try {
        if (isRememberedInternalOutgoing(messageId) || reconciledOutgoingMediaIds.has(messageId)) {
          outgoingMediaProbe.delete(messageId);
          continue;
        }

        let rawChatId = String(meta.chat_id || '').trim();
        let message = null;
        try {
          message = cachedMessageObject(messageId) || await Promise.race([
            client.getMessageById(messageId),
            new Promise((resolve) => setTimeout(() => resolve(null), 2600)),
          ]);
          if (message) {
            rawChatId = serializedId(message.to) || serializedId(message.from) || rawChatId;
            rememberMessageObject(message, rawChatId);
          }
        } catch (_) {}

        const rawState = await outgoingMediaRawState(messageId);
        const messageType = String((message && message.type) || (rawState && rawState.type) || meta.type || 'chat');
        const looksLikeMedia = Boolean(
          (message && (
            message.hasMedia || message._data?.directPath || message._data?.mediaKey || message._data?.mediaData ||
            ['image','video','audio','ptt','document','sticker'].includes(String(message.type || ''))
          )) ||
          (rawState && rawState.has_media) ||
          ['image','video','audio','ptt','document','sticker'].includes(messageType)
        );

        if (looksLikeMedia) {
          let destinationChatId = rawChatId;
          if (destinationChatId && !destinationChatId.endsWith('@g.us') && !destinationChatId.endsWith('@c.us')) {
            destinationChatId = await resolveDirectPhoneId(destinationChatId).catch(() => '') || destinationChatId;
          }
          if (!destinationChatId) {
            meta.attempts = Number(meta.attempts || 0) + 1;
            meta.next_at = now + 3500;
            outgoingMediaProbe.set(messageId, meta);
            continue;
          }

          const item = message
            ? receiving.liveMessageItem(message, true, String(messageText(message) || meta.body || rawState?.body || '').trim(), false)
            : {
                id: String(messageId),
                from_me: true,
                sender: 'Рабочий WhatsApp',
                body: String(meta.body || rawState?.body || '[Вложение]'),
                type: messageType,
                timestamp: Number(meta.timestamp || Math.floor(Date.now() / 1000)),
                ack: 1,
                notify: false,
                forwarded: Boolean(meta.forwarded || rawState?.forwarded),
              };
          item.sender = 'Рабочий WhatsApp';

          if (message) await downloading.attachMediaToItem(message, item);
          if (!item.media_base64 && !item.media_too_large) {
            const direct = await downloading.downloadMediaDirectById(messageId);
            downloading.applyMediaResult(item, direct, message || { type: messageType });
          }

          if (item.media_base64 || item.media_too_large) {
            await postJson('/api/chat-messages-sync', {
              chat_id: destinationChatId,
              append: true,
              messages: [item],
            });
            reconciledOutgoingMediaIds.set(messageId, Date.now());
            pendingMediaMessages.delete(messageId);
            outgoingMediaProbe.delete(messageId);
            console.log(`Исходящее медиа с телефона/WhatsApp Web догружено: ${messageId}`);
            continue;
          }

          // Media is confirmed but bytes are not ready yet. Reuse the normal
          // pending-media queue, while keeping this low-frequency probe alive.
          if (!pendingMediaMessages.has(messageId)) {
            pendingMediaMessages.set(messageId, {
              chat_id: destinationChatId,
              sender: 'Рабочий WhatsApp', sender_phone: '', sender_id: '',
              from_me: true, notify: false, forwarded: Boolean(item.forwarded || meta.forwarded || rawState?.forwarded),
              body: String(item.body || meta.body || '[Вложение]'),
              type: messageType,
              timestamp: Number(item.timestamp || meta.timestamp || Math.floor(Date.now() / 1000)),
              attempts: 0, next_at: Date.now() + 1800,
            });
          }
        }

        meta.attempts = Number(meta.attempts || 0) + 1;
        // 1.4s, then roughly 4s, 8s, 14s, 22s. Do not hammer Chromium.
        const delays = [2500, 4000, 6500, 9000, 12000, 15000];
        if (meta.attempts >= 18) {
          outgoingMediaProbe.delete(messageId);
        } else {
          meta.next_at = now + delays[Math.min(meta.attempts - 1, delays.length - 1)];
          outgoingMediaProbe.set(messageId, meta);
        }
      } catch (error) {
        meta.attempts = Number(meta.attempts || 0) + 1;
        if (meta.attempts >= 18) outgoingMediaProbe.delete(messageId);
        else {
          meta.next_at = Date.now() + Math.min(12000, 2500 + meta.attempts * 1800);
          outgoingMediaProbe.set(messageId, meta);
        }
        console.warn('Отложенная проверка исходящего медиа:', error.message || error);
      }
    }
  } finally {
    outgoingMediaProbeBusy = false;
  }
}
