'use strict';

// A missing return value is not proof that WhatsApp did not send a message.
// Never invoke send twice. The persistent queue owns recovery after a crash.
async function sendOnce({client, recipient, aliases = [], content, options, begin, waitMs = 1800}) {
  const observed = new Map();
  const targets = new Set([recipient, ...aliases]);
  const since = Math.floor(Date.now() / 1000);
  const idOf = m => {
    const id=m?.id || m?._data?.id;
    return typeof id==='string' ? id : String(id?._serialized || id?.$1 || '');
  };
  const capture = m => {
    if (!m?.fromMe || !targets.has(String(m.to?._serialized || m.to || ''))) return;
    if (!['chat','image','video','audio','ptt','document','sticker'].includes(m.type)) return;
    if (Number(m.timestamp || 0) < since || !idOf(m)) return;
    const expected = typeof content === 'string' ? content : String(options.caption || '');
    if (String(m.body || '') !== expected) return;
    if (typeof content !== 'string') {
      if (!m.hasMedia) return;
      const filename = m._data?.filename;
      if (!filename || filename !== content.filename) return;
    }
    observed.set(idOf(m), m);
  };
  let attempted = false;
  let error = null;
  let sent = null;
  client.on('message_create', capture);
  try {
    // Must be committed before entering WhatsApp, not after the side effect.
    if (!await begin()) throw new Error('Не удалось зафиксировать начало отправки. Сообщение не отправлено.');
    attempted = true;
    try { sent = await client.sendMessage(recipient, content, {...options, waitUntilMsgSent: false}); }
    catch (e) { error = e; }
    if (!idOf(sent)) {
      await new Promise(resolve => setTimeout(resolve, waitMs));
      // Ambiguous events (e.g. simultaneous manual sends) are not confirmations.
      sent = observed.size === 1 ? observed.values().next().value : null;
    }
    if (idOf(sent)) return {status: 'sent', message: sent};
    return {status: 'uncertain', error: 'Нет подтверждения WhatsApp. Автоповтор остановлен, проверьте переписку.', detail: String(error?.stack || error || 'empty send result')};
  } catch (e) {
    return {status: attempted ? 'uncertain' : 'failed', error: String(e.message || e)};
  } finally {
    client.removeListener('message_create', capture);
  }
}

// Runs inside the WhatsApp page. Uses the same edit action as wwebjs 1.34.7,
// bypassing optional link-preview modules and fragile result serialization.
async function editInPage(key, content, aliases) {
  try {
    const collections = window.require('WAWebCollections');
    const Msg = collections?.Msg;
    if (!Msg) return {ok: false, reason: 'not_ready'};
    let msg = Msg.get(key);
    if (!msg && typeof Msg.getMessagesById === 'function') {
      try { msg = (await Msg.getMessagesById([key]))?.messages?.[0]; } catch (_) {}
    }
    if (!msg) {
      // PN/LID address changes preserve stanza ID. Never guess by message text.
      const wanted = /^(true|false)_([^_]+)_([^_]+)(?:_(.*))?$/.exec(key);
      const models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : Msg.models || [];
      const matches = wanted ? models.filter(m => {
        const id = m?.id;
        const remote = id?.remote?._serialized || id?.remote;
        return id?.fromMe === true && wanted[1] === 'true' && id.id === wanted[3] && aliases.includes(remote);
      }) : [];
      if (matches.length === 1) msg = matches[0];
    }
    if (!msg) return {ok: false, reason: 'not_found'};
    if (!msg.id?.fromMe) return {ok: false, reason: 'not_own'};
    const value = m => String(m?.type === 'chat' ? (m.body || '') : (m?.caption ?? m?.body ?? ''));
    if (value(msg) === content) return {ok: true, id: msg.id._serialized};
    const capability = window.require('WAWebMsgActionCapability');
    if (!(capability.canEditText?.(msg) || capability.canEditCaption?.(msg))) return {ok: false, reason: 'not_allowed'};
    const action = window.require('WAWebSendMessageEditAction');
    if (typeof action?.sendMessageEdit !== 'function') return {ok: false, reason: 'not_ready'};
    let actionError = null;
    try {
      await action.sendMessageEdit(msg, content, {mentionedJidList: msg.mentionedJidList || []});
    } catch (e) { actionError = String(e?.stack || e?.message || e); }
    const updated = Msg.get(msg.id._serialized) || msg;
    if (value(updated) === content) return {ok: true, id: msg.id._serialized};
    return {ok: false, reason: 'unconfirmed', detail: actionError || 'Edit action returned without updated text'};
  } catch (e) {
    return {ok: false, reason: 'unavailable', detail: String(e?.stack || e?.message || e)};
  }
}

function editError(result) {
  const labels = {
    not_ready: 'WhatsApp ещё загружается. Дождитесь подключения и попробуйте изменить сообщение снова.',
    not_found: 'Исходное сообщение не найдено в WhatsApp. Откройте чат и дождитесь синхронизации.',
    not_own: 'Можно изменять только сообщения рабочего WhatsApp.',
    not_allowed: 'WhatsApp не разрешает изменить это сообщение: срок редактирования истёк или этот тип сообщения не поддерживается.',
    unconfirmed: 'WhatsApp не подтвердил изменение. Проверьте текст в переписке перед повторной попыткой.',
    unavailable: 'Не удалось выполнить изменение в WhatsApp. Перезапустите коннектор и проверьте сообщение.'
  };
  return labels[result?.reason] || labels.unavailable;
}

module.exports = {sendOnce, editInPage, editError};
