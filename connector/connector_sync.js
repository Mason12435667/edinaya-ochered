'use strict';
// Синхронизация: состояние коннектора, контакты, присутствие и цитаты. Работает по таймерам,
// которые запускает connector_lifecycle.js; группы ведёт connector_groups.js, исходящие с
// телефона — connector_outgoing.js.

module.exports = {
  publishConnectorState, runFastSync, syncConnection, syncContactDiscovery,
};

const avatarSync = require('./queue_avatars');
const {
  runtime, PERF_CONTACT_COOLDOWN_MS, backgroundWorkAllowed, cachedMessageObject,
  cleanContactDisplayName, client, contactDisplayById, lidToPhone, messageText,
  noteBrowserPressure, postJson, prettyPhoneFromId, rememberMessageObject, resolveDirectPhoneId,
  serializedId,
} = require('./connector_common');
const receiving = require('./connector_receive');
const downloading = require('./connector_media');
const recovering = require('./connector_recovery');
const grouping = require('./connector_groups');
const outgoing = require('./connector_outgoing');

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
let profileSyncBusy = false;
const ACTIVE_AVATAR_SYNC_COOLDOWN_MS = 10 * 60 * 1000;
const BACKGROUND_MEDIA_INTERVAL_MS = 15000;
const BACKGROUND_OUTGOING_INTERVAL_MS = 10000;
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
// Recently checked saved messages for WhatsApp reply metadata. This lets a
// newly installed patch repair recent replies that were saved by an older build
// without quote information, without importing any unknown chat history.
const quoteProbeAt = new Map();
let quoteRepairBusy = false;

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
      await outgoing.reconcileRecentOutgoing();
      if (!backgroundWorkAllowed()) return;
    }

    if (now - lastMediaRecoveryAt >= BACKGROUND_MEDIA_INTERVAL_MS) {
      lastMediaRecoveryAt = now;
      await downloading.retryPendingMedia();
      if (!backgroundWorkAllowed()) return;
      await outgoing.retryOutgoingMediaProbe();
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
      grouping.syncGroupParticipants(requestedGroupId).catch(() => {});
    }
    if (requestedGroupId) {
      const unresolvedGroupIds = Array.isArray(chatControl && chatControl.unresolved_group_message_ids)
        ? chatControl.unresolved_group_message_ids
        : [];
      grouping.repairStoredGroupMessageIdentities(requestedGroupId, unresolvedGroupIds).catch(() => {});
    }
    const quoteProbeChatId = String((chatControl && chatControl.quote_probe_chat_id) || '');
    const quoteProbeMessageIds = Array.isArray(chatControl && chatControl.quote_probe_message_ids)
      ? chatControl.quote_probe_message_ids
      : [];
    repairStoredMessageQuotes(quoteProbeChatId, quoteProbeMessageIds).catch(() => {});
    const refreshState = await postJson('/api/group-refresh-check', {});
    const refreshRequest = String((refreshState && refreshState.request) || '');
    if (refreshRequest && refreshRequest !== lastGroupRefreshRequest) {
      const refreshed = await grouping.syncGroups();
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

