'use strict';
// Группы: список групп, участники с именами и номерами, исправление отправителей в уже
// сохранённых сообщениях группы. Вызывается из syncConnection и connector_lifecycle.js.

module.exports = {
  repairStoredGroupMessageIdentities, syncGroupParticipants, syncGroups,
};

const {
  runtime, backgroundWorkAllowed, client, contactDisplayById, lidToPhone, noteBrowserPressure,
  ownMentionIds, participantDisplayById, postJson, prettyPhoneFromId, resolveGroupSenderInfo,
  serializedId,
} = require('./connector_common');

let participantSyncBusy = false;
let groupIdentityRepairBusy = false;
const groupIdentityRepairAt = new Map();
const GROUP_SYNC_MIN_INTERVAL_MS = 5 * 60 * 1000;
let groupSyncBusy = false;
let lastGroupSyncAt = 0;
let groupSyncDeferredTimer = null;

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
