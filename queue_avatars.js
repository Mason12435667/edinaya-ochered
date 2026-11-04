'use strict';
// Profile photos for the chat list: fetched from WhatsApp and sent to the
// application's /api/avatar-sync (at most once per chat per REFRESH_MS).

const REFRESH_MS = 6 * 60 * 60 * 1000;
const MAX_BYTES = 1024 * 1024;
const PER_CALL = 10;
const lastSync = new Map();

async function syncOne(client, postJson, chatId) {
  const url = await client.getProfilePicUrl(chatId).catch(() => '');
  if (!url) {
    await postJson('/api/avatar-sync', {chat_id: chatId, missing: true});
    return;
  }
  const response = await fetch(url);
  if (!response.ok) return;
  const data = Buffer.from(await response.arrayBuffer());
  if (!data.length || data.length > MAX_BYTES) return;
  await postJson('/api/avatar-sync', {chat_id: chatId, image_base64: data.toString('base64')});
}

async function sync(client, postJson, ids) {
  const now = Date.now();
  const due = [...new Set((ids || []).map((id) => String(id || '').trim()).filter(Boolean))]
    .filter((id) => now - (lastSync.get(id) || 0) >= REFRESH_MS)
    .slice(0, PER_CALL);
  for (const chatId of due) {
    lastSync.set(chatId, now);
    try {
      await syncOne(client, postJson, chatId);
    } catch (_) {
      lastSync.delete(chatId); // try again on the next call
    }
  }
  return due.length;
}

module.exports = {sync, REFRESH_MS};
