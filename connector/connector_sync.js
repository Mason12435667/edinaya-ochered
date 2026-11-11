'use strict';
// Синхронизация: группы, участники, контакты, присутствие, цитаты и исходящие, отправленные
// с телефона. Работает по таймерам, которые запускает connector_lifecycle.js.

module.exports = {
  publishConnectorState, runFastSync, scheduleOutgoingMediaProbe, syncConnection,
  syncContactDiscovery, syncGroups,
};

const avatarSync = require('./queue_avatars');
const {
  runtime, PERF_CONTACT_COOLDOWN_MS, USER_MESSAGE_TYPES, backgroundWorkAllowed, cachedMessageObject,
  cleanContactDisplayName, cleanupOutgoingTracking, client, contactDisplayById, isRememberedForwarded,
  isRememberedInternalOutgoing, lidToPhone, messageAcknowledgements, messageText, noteBrowserPressure,
  outgoingMediaProbe, ownMentionIds, participantDisplayById, pendingMediaMessages,
  persistedAcknowledgements, postJson, prettyPhoneFromId, reconciledOutgoingIds,
  rememberMessageObject, resolveDirectPhoneId, resolveGroupSenderInfo, safeWhatsAppBody, serializedId,
} = require('./connector_common');
const receiving = require('./connector_receive');
const downloading = require('./connector_media');
const recovering = require('./connector_recovery');

let heartbeatErrorReported = false;
let lastGroupRefreshRequest = '';
let lastRequestedGroupId = '';
let lastGroupParticipantSyncAt = 0;
let lastContactDiscoveryAt = 0;
let lastProfileSyncChatId = '';
let lastProfileSyncAt = 0;
let lastPresenceSyncAt = 0;
let lastPresenceSignature = '';
let presenceSyncBusy = false;
let contactDiscoveryBusy = false;
let recentOutgoingBusy = false;
let participantSyncBusy = false;
let groupIdentityRepairBusy = false;
const groupIdentityRepairAt = new Map();
let profileSyncBusy = false;
const GROUP_SYNC_MIN_INTERVAL_MS = 5 * 60 * 1000;
const ACTIVE_AVATAR_SYNC_COOLDOWN_MS = 10 * 60 * 1000;
const BACKGROUND_MEDIA_INTERVAL_MS = 15000;
const BACKGROUND_OUTGOING_INTERVAL_MS = 10000;
let groupSyncBusy = false;
let lastGroupSyncAt = 0;
let groupSyncDeferredTimer = null;
let lastActiveAvatarSignature = '';
let lastActiveAvatarSyncAt = 0;
let lastMediaRecoveryAt = 0;
let lastOutgoingRecoveryAt = 0;

function syncActiveAvatar(requestedChatId = '', requestedGroupId = '') {
  // Cosmetic avatars are disabled by default. They can be explicitly enabled
  // later without touching the message path.
  if (String(process.env.QUEUE_ENABLE_WHATSAPP_AVATARS || '') !== '1') return;
  if (!backgroundWorkAllowed()) return;
  const ids = [...new Set([requestedChatId, requestedGroupId].map((v) => String(v || '').trim()).filter(Boolean))];
  if (!ids.length) return;
  const signature = ids.join('|');
  const now = Date.now();
  if (signature === lastActiveAvatarSignature && now - lastActiveAvatarSyncAt < ACTIVE_AVATAR_SYNC_COOLDOWN_MS) return;
  lastActiveAvatarSignature = signature;
  lastActiveAvatarSyncAt = now;
  avatarSync.sync(client, postJson, ids).catch((error) => noteBrowserPressure(error, 'avatar'));
}
let outgoingMediaProbeBusy = false;
// Отдельно отмечаем ручные исходящие, у которых бинарное медиа уже сохранено.
const reconciledOutgoingMediaIds = new Map();
// Recently checked saved messages for WhatsApp reply metadata. This lets a
// newly installed patch repair recent replies that were saved by an older build
// without quote information, without importing any unknown chat history.
const quoteProbeAt = new Map();
let quoteRepairBusy = false;

async function repairStoredGroupMessageIdentities(groupId, requestedIds = []) {
  const safeGroupId = String(groupId || '').trim();
  if (!backgroundWorkAllowed()) return;
  if (!safeGroupId.endsWith('@g.us') || groupIdentityRepairBusy || !runtime.connectorOperational) return;
  const now = Date.now();
  if (now - Number(groupIdentityRepairAt.get(safeGroupId) || 0) < 8000) return;
  groupIdentityRepairAt.set(safeGroupId, now);
  groupIdentityRepairBusy = true;
  try {
    const wanted = [...new Set((Array.isArray(requestedIds) ? requestedIds : [])
      .map((value) => String(value || '').trim()).filter(Boolean))].slice(0, 30);
    const wantedSet = new Set(wanted);
    const messagesById = new Map();

    // Сервер сообщает ID только тех сохранённых сообщений, где имя,
    // номер или sender_id ещё не определены. Получаем именно эти сообщения у
    // WhatsApp и никогда не импортируем постороннюю историю.
    if (wanted.length && typeof client.getMessageById === 'function') {
      for (let i = 0; i < wanted.length; i += 6) {
        const found = await Promise.all(wanted.slice(i, i + 6).map(async (id) => {
          try {
            return await Promise.race([
              client.getMessageById(id),
              new Promise((resolve) => setTimeout(() => resolve(null), 1400)),
            ]);
          } catch (_) {
            return null;
          }
        }));
        for (const message of found) {
          const id = serializedId(message && message.id);
          if (id && wantedSet.has(id)) messagesById.set(id, message);
        }
      }
    }

    // Если getMessageById не вернул часть моделей, читаем небольшой хвост
    // выбранной группы. Фильтр wantedSet гарантирует update-only поведение.
    if (wanted.length && messagesById.size < wanted.length) {
      try {
        const chat = await Promise.race([
          client.getChatById(safeGroupId),
          new Promise((resolve) => setTimeout(() => resolve(null), 2200)),
        ]);
        if (chat && typeof chat.fetchMessages === 'function') {
          const recent = await Promise.race([
            chat.fetchMessages({ limit: 120 }),
            new Promise((resolve) => setTimeout(() => resolve([]), 5000)),
          ]);
          for (const message of (Array.isArray(recent) ? recent : [])) {
            const id = serializedId(message && message.id);
            if (id && wantedSet.has(id)) messagesById.set(id, message);
          }
        }
      } catch (_) {}
    }

    // Резерв для новых live-сообщений: используем модели, которые WhatsApp Web
    // уже держит в памяти. Это также помогает сразу после получения сообщения.
    if (!wanted.length) {
      const rows = await Promise.race([
        client.pupPage.evaluate((gid) => {
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
          try {
            if (Msg && typeof Msg.getModelsArray === 'function') models = Msg.getModelsArray() || [];
            else if (Msg && Array.isArray(Msg.models)) models = Msg.models;
          } catch (_) {}
          return (Array.isArray(models) ? models : []).slice(-500).map((m) => ({
            id: widText(m && m.id),
            remote: widText((m && m.id && m.id.remote) || (m && m.from) || (m && m.to)),
            from_me: Boolean(m && m.id && m.id.fromMe),
          })).filter((row) => row.id && row.remote === gid && !row.from_me).slice(-80);
        }, safeGroupId),
        new Promise((resolve) => setTimeout(() => resolve([]), 1500)),
      ]);
      for (const row of (Array.isArray(rows) ? rows : [])) {
        try {
          const message = typeof client.getMessageById === 'function'
            ? await Promise.race([client.getMessageById(String(row.id)), new Promise((resolve) => setTimeout(() => resolve(null), 800))])
            : null;
          if (message) messagesById.set(String(row.id), message);
        } catch (_) {}
      }
    }

    if (!messagesById.size) return;

    const identities = [];
    for (const [mid, message] of messagesById.entries()) {
      if (!message || message.fromMe) continue;
      const identity = await resolveGroupSenderInfo(message, '').catch(() => null);
      if (!identity) continue;
      const phoneId = String(identity.resolved_id || '').endsWith('@c.us')
        ? String(identity.resolved_id)
        : (lidToPhone.get(String(identity.id || '')) || '');
      const phone = String(identity.phone || prettyPhoneFromId(phoneId) || '').trim();
      let sender = String(identity.name || '').trim();
      const folded = sender.toLocaleLowerCase('ru');
      if (!sender || ['участник', 'участник группы', 'пользователь whatsapp', 'direct'].includes(folded)) {
        sender = phone || 'Участник группы';
      }
      identities.push({
        id: mid,
        sender,
        sender_phone: phone,
        sender_id: String(identity.id || identity.resolved_id || phoneId || '').trim(),
      });
    }
    if (identities.length) {
      const result = await postJson('/api/group-message-identities-sync', {
        chat_id: safeGroupId,
        identities,
      });
      if (Number(result && result.messages || 0) > 0) {
        console.log(`Данные авторов сообщений группы обновлены: ${safeGroupId} · ${result.messages}`);
      }
    }
  } catch (error) {
    console.warn(`Не удалось догрузить авторов сообщений группы ${safeGroupId}:`, error.message || error);
  } finally {
    groupIdentityRepairBusy = false;
  }
}

async function syncGroupParticipants(groupId) {
  if (participantSyncBusy || !backgroundWorkAllowed()) return;
  participantSyncBusy = true;
  const safeGroupId = String(groupId || '').trim();
  if (!safeGroupId.endsWith('@g.us')) {
    participantSyncBusy = false;
    return;
  }

  try {
    let rows = await Promise.race([
      client.pupPage.evaluate(async (gid) => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const modelsFrom = (collection) => {
          if (!collection) return [];
          try { if (Array.isArray(collection)) return collection; } catch (_) {}
          try { if (typeof collection.getModelsArray === 'function') return collection.getModelsArray() || []; } catch (_) {}
          try { if (typeof collection.toArray === 'function') return collection.toArray() || []; } catch (_) {}
          try { if (Array.isArray(collection.models)) return collection.models; } catch (_) {}
          try {
            if (collection._models && typeof collection._models === 'object') {
              return Array.isArray(collection._models) ? collection._models : Object.values(collection._models);
            }
          } catch (_) {}
          try {
            if (typeof collection.serialize === 'function') {
              const serialized = collection.serialize();
              if (Array.isArray(serialized)) return serialized;
              if (serialized && typeof serialized === 'object') return Object.values(serialized);
            }
          } catch (_) {}
          try {
            if (typeof collection === 'object') {
              const values = Object.values(collection).filter((v) => v && typeof v === 'object');
              if (values.length) return values;
            }
          } catch (_) {}
          return [];
        };
        const participantRow = (p) => {
          const id = widText(
            (p && p.id) ||
            (p && p.wid) ||
            (p && p.userWid) ||
            (p && p.contact && p.contact.id)
          );
          return {
            id,
            is_admin: Boolean(p && (
              p.isAdmin === true || p.isSuperAdmin === true ||
              (typeof p.isAdmin === 'function' && p.isAdmin()) ||
              (typeof p.isSuperAdmin === 'function' && p.isSuperAdmin())
            )),
          };
        };

        let chat = null;
        // Самый надёжный путь в новых сборках WhatsApp Web: получить реальную
        // модель чата через WWebJS.getChat без сериализации в Node.js.
        try {
          if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
            chat = await window.WWebJS.getChat(gid, { getAsModel: false });
          }
        } catch (_) {}

        const Collections = window.require('WAWebCollections');
        const WidFactory = window.require('WAWebWidFactory');
        const wid = WidFactory.createWid(gid);
        if (!chat) {
          try { chat = Collections.Chat.get(wid) || Collections.Chat.get(gid) || null; } catch (_) {}
        }
        if (!chat && Collections.Chat && typeof Collections.Chat.find === 'function') {
          try { chat = await Collections.Chat.find(wid); } catch (_) {}
        }

        // Просим WhatsApp обновить метаданные группы перед чтением участников.
        try {
          const query = window.require('WAWebGroupQueryJob');
          if (query && typeof query.queryAndUpdateGroupMetadataById === 'function') {
            await Promise.race([
              query.queryAndUpdateGroupMetadataById({ id: gid }),
              new Promise((resolve) => setTimeout(resolve, 2500)),
            ]);
          }
        } catch (_) {}

        try {
          if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
            const refreshed = await window.WWebJS.getChat(gid, { getAsModel: false });
            if (refreshed) chat = refreshed;
          }
        } catch (_) {}
        if (!chat) {
          try { chat = Collections.Chat.get(wid) || Collections.Chat.get(gid) || null; } catch (_) {}
        }
        if (!chat || !chat.groupMetadata) return [];

        const list = modelsFrom(chat.groupMetadata.participants);
        return list.slice(0, 500).map(participantRow).filter((p) => p.id);
      }, safeGroupId),
      new Promise((resolve) => setTimeout(() => resolve([]), 6500)),
    ]);

    rows = Array.isArray(rows) ? rows : [];

    // Последний резерв через публичный объект GroupChat. Он медленнее, но
    // полезен, если внутренняя коллекция изменила форму после обновления Web.
    if (!rows.length) {
      try {
        const chat = await Promise.race([
          client.getChatById(safeGroupId),
          new Promise((resolve) => setTimeout(() => resolve(null), 4500)),
        ]);
        const participants = chat && Array.isArray(chat.participants) ? chat.participants : [];
        rows = participants.map((p) => ({
          id: serializedId(p && p.id),
          is_admin: Boolean(p && (p.isAdmin || p.isSuperAdmin)),
        })).filter((p) => p.id);
      } catch (_) {}
    }

    const sourceRows = rows;
    if (!sourceRows.length) {
      await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: [] });
      console.warn(`Участники группы пока не получены: ${safeGroupId}`);
      return;
    }

    const rawLids = sourceRows.map((p) => String(p.id || '')).filter((id) => id.endsWith('@lid'));
    if (rawLids.length && typeof client.getContactLidAndPhone === 'function') {
      for (let i = 0; i < rawLids.length; i += 60) {
        try {
          const mapped = await Promise.race([
            client.getContactLidAndPhone(rawLids.slice(i, i + 60)),
            new Promise((resolve) => setTimeout(() => resolve([]), 2500)),
          ]);
          for (const row of (Array.isArray(mapped) ? mapped : [])) {
            const lid = serializedId(row && row.lid);
            const pn = serializedId(row && row.pn);
            if (lid && pn.endsWith('@c.us')) lidToPhone.set(lid, pn);
          }
        } catch (_) {}
      }
    }

    const ownId = serializedId(client.info && client.info.wid);
    const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
    const quick = sourceRows.map((p) => {
      const raw = String(p.id || '');
      const phoneId = raw.endsWith('@c.us') ? raw : (lidToPhone.get(raw) || '');
      const me = raw === ownId || Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
      if (me) {
        ownMentionIds.add(raw);
        if (phoneId) ownMentionIds.add(phoneId);
      }
      const phone = prettyPhoneFromId(phoneId);
      const known = participantDisplayById.get(raw) || participantDisplayById.get(phoneId) || contactDisplayById.get(raw) || contactDisplayById.get(phoneId) || '';
      const name = me ? 'Рабочий WhatsApp' : (known || phone || 'Участник');
      participantDisplayById.set(raw, name);
      if (phoneId) participantDisplayById.set(phoneId, name);
      return { mention_id: raw, resolved_id: phoneId || raw, name, phone, is_admin: Boolean(p.is_admin), is_me: me };
    });
    await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: quick });
    // Do not fan out avatar requests for every group participant here.
    // syncConnection() fetches the avatar of the actively opened chat/group.
    console.log(`Участники группы загружены: ${safeGroupId} · ${quick.length}`);

    // Имена догружаем небольшими порциями, чтобы не подвешивать QR-коннектор.
    const enriched = [];
    for (let i = 0; i < sourceRows.length; i += 12) {
      const part = await Promise.all(sourceRows.slice(i, i + 12).map(async (p) => {
        const raw = String(p.id || '');
        if (!raw) return null;
        const phoneId = raw.endsWith('@c.us') ? raw : (lidToPhone.get(raw) || '');
        let c = null;
        try {
          c = await Promise.race([
            client.getContactById(phoneId || raw),
            new Promise((resolve) => setTimeout(() => resolve(null), 1000)),
          ]);
        } catch (_) {}
        const me = Boolean(c && c.isMe) || raw === ownId || Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
        if (me) {
          ownMentionIds.add(raw);
          if (phoneId) ownMentionIds.add(phoneId);
        }
        let phone = prettyPhoneFromId(phoneId);
        if (!phone && c && typeof c.getFormattedNumber === 'function') {
          try {
            phone = String(await Promise.race([
              c.getFormattedNumber(),
              new Promise((resolve) => setTimeout(() => resolve(''), 700)),
            ]) || '').trim();
          } catch (_) {}
        }
        const name = me
          ? 'Рабочий WhatsApp'
          : String((c && (c.pushname || c.name || c.shortName)) || participantDisplayById.get(raw) || participantDisplayById.get(phoneId) || contactDisplayById.get(raw) || contactDisplayById.get(phoneId) || phone || 'Участник');
        participantDisplayById.set(raw, name);
        contactDisplayById.set(raw, name);
        if (phoneId) {
          participantDisplayById.set(phoneId, name);
          contactDisplayById.set(phoneId, name);
        }
        return { mention_id: raw, resolved_id: phoneId || raw, name, phone, is_admin: Boolean(p.is_admin), is_me: me };
      }));
      enriched.push(...part.filter(Boolean));
      await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: enriched });
    }
  } catch (error) {
    console.warn(`Не удалось получить участников группы ${safeGroupId}:`, error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    participantSyncBusy = false;
  }
}

async function syncContactDiscovery() {
  if (contactDiscoveryBusy || !backgroundWorkAllowed()) return;
  const now=Date.now(); if(now-lastContactDiscoveryAt<PERF_CONTACT_COOLDOWN_MS)return; lastContactDiscoveryAt=now; contactDiscoveryBusy=true;
  try {
    const contacts=await Promise.race([client.getContacts(),new Promise(r=>setTimeout(()=>r([]),7000))]);
    const users=(Array.isArray(contacts)?contacts:[]).filter(c=>c&&c.isUser&&!c.isGroup).slice(0,1000);
    const lids=users.map(c=>serializedId(c.id)).filter(id=>id.endsWith('@lid'));
    if(lids.length&&typeof client.getContactLidAndPhone==='function') for(let i=0;i<lids.length;i+=80){try{const rows=await Promise.race([client.getContactLidAndPhone(lids.slice(i,i+80)),new Promise(r=>setTimeout(()=>r([]),3500))]);for(const row of (Array.isArray(rows)?rows:[])){const lid=serializedId(row&&row.lid),pn=serializedId(row&&row.pn);if(lid&&pn.endsWith('@c.us'))lidToPhone.set(lid,pn);}}catch(_){}}
    const payload=users.map(c=>{
      const raw=serializedId(c.id),pn=raw.endsWith('@c.us')?raw:(lidToPhone.get(raw)||'');
      const chatId=pn||raw;
      const displayName=cleanContactDisplayName(c.pushname||c.name||c.shortName||'');
      if(chatId&&displayName) contactDisplayById.set(chatId,displayName);
      if(raw&&displayName) contactDisplayById.set(raw,displayName);
      return {chat_id:chatId,raw_id:raw,phone:prettyPhoneFromId(pn),name:displayName,saved:Boolean(c.isMyContact)};
    });
    await postJson('/api/contact-list-sync',{contacts:payload});
    // Avatars are loaded on demand for the active chat / active group.
    // Bulk-enqueueing up to 1000 contacts caused continuous profile-photo
    // lookups and 20-second retries for privacy/no-photo contacts.
  } catch(error){console.warn('Поиск контактов WhatsApp временно недоступен:',error.message||error);}
  finally { contactDiscoveryBusy=false; }
}

async function syncSelectedContactProfile(chatId) {
  if (profileSyncBusy || !backgroundWorkAllowed()) return;
  const safe=String(chatId||'').trim(); if(!safe||safe.endsWith('@g.us'))return; const now=Date.now(); if(safe===lastProfileSyncChatId&&now-lastProfileSyncAt<30000)return; lastProfileSyncChatId=safe;lastProfileSyncAt=now; profileSyncBusy=true;
  try { const pn=safe.endsWith('@c.us')?safe:await resolveDirectPhoneId(safe); const contact=await Promise.race([client.getContactById(pn||safe),new Promise(r=>setTimeout(()=>r(null),2200))]); if(!contact)return; let pic='',about='',formatted=''; try{pic=await Promise.race([contact.getProfilePicUrl(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} try{about=await Promise.race([contact.getAbout(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} try{formatted=await Promise.race([contact.getFormattedNumber(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} const displayName=cleanContactDisplayName(contact.pushname||contact.name||contact.shortName||''); if(displayName){contactDisplayById.set(safe,displayName);if(pn)contactDisplayById.set(pn,displayName);} await postJson('/api/contact-profile-sync',{chat_id:safe,name:displayName,phone:formatted||prettyPhoneFromId(pn),about:String(about||''),profile_pic_url:String(pic||''),is_business:Boolean(contact.isBusiness)}); } catch(error){console.warn('Профиль WhatsApp не загрузился:',error.message||error);}
  finally { profileSyncBusy=false; }
}


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

async function runFastSync() {
  if (runtime.fastSyncBusy || runtime.shutdownStarted || !runtime.connectorOperational) return;
  if (Date.now() < runtime.startupLiveOnlyUntil) return;
  runtime.fastSyncBusy = true;
  try {
    // Critical lane: missed inbound messages always go first.
    await recovering.reconcileRecentInbound();

    // Optional/background lane.
    if (!backgroundWorkAllowed()) return;
    const now = Date.now();

    if (now - lastOutgoingRecoveryAt >= BACKGROUND_OUTGOING_INTERVAL_MS) {
      lastOutgoingRecoveryAt = now;
      await reconcileRecentOutgoing();
      if (!backgroundWorkAllowed()) return;
    }

    if (now - lastMediaRecoveryAt >= BACKGROUND_MEDIA_INTERVAL_MS) {
      lastMediaRecoveryAt = now;
      await downloading.retryPendingMedia();
      if (!backgroundWorkAllowed()) return;
      await retryOutgoingMediaProbe();
    }
  } finally {
    runtime.fastSyncBusy = false;
  }
}


async function repairStoredMessageQuotes(chatId, messageIds) {
  if (!backgroundWorkAllowed()) return;
  const targetChatId = String(chatId || '').trim();
  const ids = Array.isArray(messageIds) ? messageIds.map((v) => String(v || '').trim()).filter(Boolean).slice(0, 20) : [];
  if (!targetChatId || !ids.length || quoteRepairBusy || typeof client.getMessageById !== 'function') return;
  quoteRepairBusy = true;
  try {
    const now = Date.now();
    for (const messageId of ids) {
      const checkedAt = Number(quoteProbeAt.get(messageId) || 0);
      if (checkedAt && now - checkedAt < 5 * 60 * 1000) continue;
      quoteProbeAt.set(messageId, now);
      let message = null;
      try {
        message = cachedMessageObject(messageId) || await client.getMessageById(messageId);
        if (message) rememberMessageObject(message, targetChatId);
      } catch (_) {}
      if (!message) continue;
      const item = receiving.liveMessageItem(message, Boolean(message.fromMe), String(messageText(message) || '').trim(), false);
      item.sender = '';
      item.sender_phone = '';
      item.sender_id = '';
      item.mentions = [];
      await receiving.attachQuoteToItem(message, item);
      if (!item.quoted_message_key) continue;
      await postJson('/api/chat-messages-sync', {
        chat_id: targetChatId,
        append: true,
        messages: [item],
      }).catch(() => {});
    }
    const cutoff = Date.now() - 30 * 60 * 1000;
    for (const [id, at] of quoteProbeAt) if (Number(at || 0) < cutoff) quoteProbeAt.delete(id);
  } finally {
    quoteRepairBusy = false;
  }
}

async function syncPresence(ids) {
  if (presenceSyncBusy || !backgroundWorkAllowed()) return;
  const cleanIds = [...new Set((Array.isArray(ids) ? ids : []).map((id) => String(id || '').trim()).filter((id) => id && !id.endsWith('@g.us')).slice(0, 16))];
  if (!cleanIds.length) return;
  const signature = cleanIds.join('|');
  const now = Date.now();
  if (signature === lastPresenceSignature && now - lastPresenceSyncAt < 7000) return;
  if (signature !== lastPresenceSignature && now - lastPresenceSyncAt < 1500) return;
  lastPresenceSignature = signature;
  lastPresenceSyncAt = now;
  presenceSyncBusy = true;
  try {
    const items = await Promise.race([
      client.pupPage.evaluate(async (inputIds) => {
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const timeout = async (promise, ms) => Promise.race([promise, sleep(ms).then(() => null)]);
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        let Collections = null;
        let WidFactory = null;
        try { Collections = window.require('WAWebCollections'); } catch (_) {}
        try { WidFactory = window.require('WAWebWidFactory'); } catch (_) {}
        const readOne = async (chatId) => {
          let chat = null;
          try {
            if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
              chat = await timeout(window.WWebJS.getChat(chatId, { getAsModel: false }), 900);
            }
          } catch (_) {}
          if (!chat && Collections && Collections.Chat) {
            try {
              const wid = WidFactory && typeof WidFactory.createWid === 'function' ? WidFactory.createWid(chatId) : chatId;
              chat = Collections.Chat.get(wid) || Collections.Chat.get(chatId) || null;
              if (!chat && typeof Collections.Chat.find === 'function') chat = await timeout(Collections.Chat.find(wid), 900);
            } catch (_) {}
          }
          if (!chat) return { chat_id: chatId, known: false, online: false, state: '' };
          let presence = chat.presence || chat.__x_presence || null;
          let subscribed = false;
          try {
            if (presence && typeof presence.subscribe === 'function') {
              await timeout(Promise.resolve(presence.subscribe()), 1200);
              subscribed = true;
            }
          } catch (_) {}
          if (!subscribed) {
            try {
              const bridge = window.require('WAWebContactPresenceBridge');
              const fn = bridge && (bridge.subscribeUserPresence || bridge.subscribePresence);
              if (typeof fn === 'function') {
                const wid = WidFactory && typeof WidFactory.createWid === 'function' ? WidFactory.createWid(chatId) : chatId;
                await timeout(Promise.resolve(fn(wid)), 1200);
                subscribed = true;
              }
            } catch (_) {}
          }
          if (subscribed) await sleep(180);
          try {
            if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
              const refreshed = await timeout(window.WWebJS.getChat(chatId, { getAsModel: false }), 500);
              if (refreshed) chat = refreshed;
            }
          } catch (_) {}
          presence = chat.presence || chat.__x_presence || presence || null;
          const attrs = (presence && (presence.attributes || presence.__x_attributes)) || {};
          const chatstate = (presence && (presence.chatstate || presence.__x_chatstate)) || attrs.chatstate || {};
          const rawOnline = attrs.isOnline !== undefined ? attrs.isOnline : (presence && presence.isOnline !== undefined ? presence.isOnline : (presence && presence.__x_isOnline));
          const state = String((chatstate && (chatstate.type || chatstate.state || chatstate.chatstate)) || attrs.state || '').toLowerCase();
          const online = rawOnline === true || state === 'composing' || state === 'recording' || state === 'available';
          const known = online || state === 'unavailable';
          return { chat_id: chatId, known, online, state };
        };
        const result = [];
        for (let i = 0; i < inputIds.length; i += 4) {
          const part = await Promise.all(inputIds.slice(i, i + 4).map(readOne));
          result.push(...part);
        }
        return result;
      }, cleanIds),
      new Promise((resolve) => setTimeout(() => resolve([]), 6000)),
    ]);
    const rows = Array.isArray(items) ? items : [];
    if (rows.length) await postJson('/api/presence-sync', { items: rows });
  } catch (_) {
    // Presence is best-effort: privacy settings or WhatsApp Web updates can hide it.
  } finally {
    presenceSyncBusy = false;
  }
}

let connectionSyncBusy = false;
async function syncConnection() {
  if (connectionSyncBusy) return;
  connectionSyncBusy = true;
  try {
    await postJson('/api/chat-list-sync', { connected: true });
    const chatControl = await postJson('/api/chat-control', {});
    const requestedChatId = String((chatControl && chatControl.requested_chat_id) || '');
    for (const id of (chatControl.read_chat_ids || [])) receiving.markChatRead(id).catch(() => {});
    const presenceIds = Array.isArray(chatControl && chatControl.presence_ids) ? chatControl.presence_ids : (requestedChatId ? [requestedChatId] : []);
    syncPresence(presenceIds).catch(() => {});
    if (requestedChatId && !requestedChatId.endsWith('@g.us')) {
      // Only an actively visible chat may acknowledge reading.
      syncSelectedContactProfile(requestedChatId).catch(() => {});
    }
    syncContactDiscovery().catch(() => {});
    const requestedGroupId = String((chatControl && chatControl.requested_group_id) || '');
    syncActiveAvatar(requestedChatId, requestedGroupId);
    const now = Date.now();
    if (requestedGroupId && (requestedGroupId !== lastRequestedGroupId || now - lastGroupParticipantSyncAt > 10 * 60 * 1000)) {
      lastRequestedGroupId = requestedGroupId;
      lastGroupParticipantSyncAt = now;
      syncGroupParticipants(requestedGroupId).catch(() => {});
    }
    if (requestedGroupId) {
      const unresolvedGroupIds = Array.isArray(chatControl && chatControl.unresolved_group_message_ids)
        ? chatControl.unresolved_group_message_ids
        : [];
      repairStoredGroupMessageIdentities(requestedGroupId, unresolvedGroupIds).catch(() => {});
    }
    const quoteProbeChatId = String((chatControl && chatControl.quote_probe_chat_id) || '');
    const quoteProbeMessageIds = Array.isArray(chatControl && chatControl.quote_probe_message_ids)
      ? chatControl.quote_probe_message_ids
      : [];
    repairStoredMessageQuotes(quoteProbeChatId, quoteProbeMessageIds).catch(() => {});
    const refreshState = await postJson('/api/group-refresh-check', {});
    const refreshRequest = String((refreshState && refreshState.request) || '');
    if (refreshRequest && refreshRequest !== lastGroupRefreshRequest) {
      const refreshed = await syncGroups();
      if (refreshed) lastGroupRefreshRequest = refreshRequest;
    }
    heartbeatErrorReported = false;
  } catch (error) {
    if (!heartbeatErrorReported) {
      console.warn('Локальная панель пока недоступна:', error.message || error);
      heartbeatErrorReported = true;
    }
  } finally { connectionSyncBusy = false; }
}

async function publishConnectorState(status, qrDataUrl = '') {
  try {
    await postJson('/api/connector-state', {
      status,
      qr_data_url: qrDataUrl,
    });
  } catch (error) {
    console.warn('Не удалось обновить состояние коннектора в админке:', error.message || error);
  }
}

async function syncGroups() {
  if (runtime.shutdownStarted || !runtime.connectorOperational) return false;
  if (groupSyncBusy) return false;

  const now = Date.now();
  if (!backgroundWorkAllowed()) {
    if (!groupSyncDeferredTimer) {
      const blockedUntil = Math.max(runtime.startupLiveOnlyUntil, runtime.hotInboundUntil, runtime.browserPressureUntil);
      const delay = Math.max(1000, blockedUntil - now + 500);
      groupSyncDeferredTimer = setTimeout(() => {
        groupSyncDeferredTimer = null;
        void syncGroups();
      }, delay);
      if (groupSyncDeferredTimer.unref) groupSyncDeferredTimer.unref();
    }
    return false;
  }
  if (now - lastGroupSyncAt < GROUP_SYNC_MIN_INTERVAL_MS) return false;

  groupSyncBusy = true;
  lastGroupSyncAt = now;
  try {
    await syncGroupsCore();
    return true;
  } finally {
    groupSyncBusy = false;
    lastGroupSyncAt = Date.now();
  }
}

async function syncGroupsCore() {
  const diagnostics = {
    chats_ok: false, chats_total: 0, chats_groups: 0,
    contacts_ok: false, contacts_total: 0, contacts_groups: 0,
    enriched: 0, errors: [],
  };

  try {
    const snapshot = await client.pupPage.evaluate(() => {
      const widText = (value) => {
        if (!value) return '';
        if (typeof value === 'string') return value;
        if (typeof value._serialized === 'string') return value._serialized;
        if (typeof value.$1 === 'string') return value.$1;
        if (value.user && value.server) return `${value.user}@${value.server}`;
        return '';
      };
      const models = (collection) => {
        if (!collection) return [];
        try { if (typeof collection.getModelsArray === 'function') return collection.getModelsArray() || []; } catch (_) {}
        try { if (Array.isArray(collection.models)) return collection.models; } catch (_) {}
        try { if (Array.isArray(collection._models)) return collection._models; } catch (_) {}
        return [];
      };
      const found = new Map();
      const add = (item, source) => {
        if (!item) return;
        const id = widText(item.id || item.wid);
        if (!id.endsWith('@g.us') && item.isGroup !== true) return;
        const name = String(
          item.name || item.subject || item.formattedTitle || item.pushname ||
          item.shortName || (item.contact && (item.contact.name || item.contact.pushname)) || ''
        ).trim();
        const participants = Array.isArray(item.participants)
          ? item.participants
          : (item.groupMetadata && Array.isArray(item.groupMetadata.participants)
            ? item.groupMetadata.participants : []);
        const previous = found.get(id) || { id, name: '', participant_count: 0, sources: [] };
        found.set(id, {
          id,
          name: name || previous.name || 'Группа WhatsApp',
          participant_count: Math.max(Number(previous.participant_count || 0), participants.length || 0),
          sources: [...new Set([...(previous.sources || []), source])],
        });
      };

      let collections = null;
      try { collections = window.require('WAWebCollections'); } catch (_) {}
      const chats = models(collections && collections.Chat);
      const contacts = models(collections && collections.Contact);
      for (const item of chats) add(item, 'chats');
      for (const item of contacts) add(item, 'contacts');

      return {
        groups: [...found.values()],
        chats_total: chats.length,
        contacts_total: contacts.length,
      };
    });

    const original = Array.isArray(snapshot && snapshot.groups) ? snapshot.groups : [];
    const groups = original
      .map((group) => ({
        id: String(group.id || '').trim(),
        name: String(group.name || 'Группа WhatsApp').trim(),
        participant_count: Math.max(0, Number(group.participant_count || 0)),
      }))
      .filter((group) => group.id.endsWith('@g.us'))
      .sort((a, b) => a.name.localeCompare(b.name, 'ru'));

    diagnostics.chats_ok = true;
    diagnostics.contacts_ok = true;
    diagnostics.chats_total = Number(snapshot && snapshot.chats_total || 0);
    diagnostics.contacts_total = Number(snapshot && snapshot.contacts_total || 0);
    diagnostics.chats_groups = original.filter((g) => Array.isArray(g.sources) && g.sources.includes('chats')).length;
    diagnostics.contacts_groups = original.filter((g) => Array.isArray(g.sources) && g.sources.includes('contacts')).length;

    await postJson('/api/group-list-sync', { groups, diagnostics });
    console.log(
      `Список групп обновлён облегчённо: ${groups.length}. ` +
      `Чаты ${diagnostics.chats_groups}/${diagnostics.chats_total}; ` +
      `контакты ${diagnostics.contacts_groups}/${diagnostics.contacts_total}.`
    );
  } catch (error) {
    noteBrowserPressure(error, 'group snapshot');
    console.warn('Облегчённая синхронизация групп временно недоступна:', error.message || error);
  }
}
