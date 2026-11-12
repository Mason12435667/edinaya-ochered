'use strict';
// Жизненный цикл: QR-код, вход, готовность WhatsApp Web, запуск таймеров, проверка моста,
// контроль памяти и завершение работы. При подключении файла регистрирует обработчики событий клиента.

module.exports = {
  gracefulShutdown, restartAfterStuckBridge,
};

const fs = require('fs');
const qrcode = require('qrcode-terminal');
const QRCode = require('qrcode');
const {
  runtime, PERF_CONTACT_COOLDOWN_MS, STARTUP_BACKFILL_BATCH, STARTUP_BACKFILL_MODEL_LIMIT,
  backgroundWorkAllowed, client, noteBrowserPressure, outgoingMediaProbe, pendingMediaMessages,
  postJson,
} = require('./connector_common');
const sending = require('./connector_send');
const calling = require('./connector_calls');
const syncing = require('./connector_sync');
const grouping = require('./connector_groups');

let outboundTimer = null;
let heartbeatTimer = null;
let groupSyncTimer = null;
let performanceTimer = null;
let highMemorySamples = 0;
const PERF_RECOVERY_INTERVAL_MS = Math.max(2000, Number(process.env.QUEUE_RECOVERY_INTERVAL_MS || 2000));
const PERF_RAM_RESTART_MB = Math.max(0, Number(process.env.QUEUE_CONNECTOR_RAM_RESTART_MB || 0));
let readyFallbackTimer = null;
let readyFallbackStartedAt = 0;
let fastInboundTimer = null;
const STARTUP_LIVE_ONLY_MS = 12000;

// QUEUE_CONNECTOR_STABILITY_20260930
// WhatsApp Web can reload its execution context while getState() still says
// CONNECTED. In that state the old code kept reporting "connected" although
// window.WWebJS/WAWebCollections were gone, so inbound messages and auto-replies
// could silently stop. Track the actual injected bridge and re-arm recovery.
let bridgeWatchdogTimer = null;
let bridgeWatchdogBusy = false;
let bridgeFailureCount = 0;
let bridgeRecoveryStartedAt = 0;
let authRecoveryBusy = false;
let lastAuthenticatedLogAt = 0;

client.on('qr', async (qr) => {
  console.log('\nОткрой в телефоне: WhatsApp -> Связанные устройства -> Привязка устройства');
  console.log('Отсканируй QR-код ниже:\n');
  qrcode.generate(qr, { small: true });
  try {
    const qrDataUrl = await QRCode.toDataURL(qr, { width: 360, margin: 2 });
    await syncing.publishConnectorState('qr', qrDataUrl);
  } catch (error) {
    console.warn('Не удалось показать QR-код в админке:', error.message || error);
  }
});

async function whatsappBridgeReady() {
  if (runtime.shutdownStarted) return false;
  try {
    if (!client.pupPage || client.pupPage.isClosed()) return false;
    const probe = client.pupPage.evaluate(() => {
      try {
        const collections = window.require('WAWebCollections');
        return Boolean(
          window.WWebJS &&
          typeof window.WWebJS.getChat === 'function' &&
          typeof window.WWebJS.sendMessage === 'function' &&
          collections &&
          collections.Msg
        );
      } catch (_) {
        return false;
      }
    });
    return Boolean(await Promise.race([
      probe,
      new Promise((resolve) => setTimeout(() => resolve(false), 5000)),
    ]));
  } catch (_) {
    return false;
  }
}

async function checkBridgeHealth() {
  if (bridgeWatchdogBusy || runtime.shutdownStarted || !runtime.connectorOperational) return;
  if (Date.now() < runtime.startupLiveOnlyUntil) return;
  bridgeWatchdogBusy = true;
  try {
    const ok = await whatsappBridgeReady();
    if (ok) {
      bridgeFailureCount = 0;
      bridgeRecoveryStartedAt = 0;
      return;
    }

    bridgeFailureCount += 1;
    if (bridgeFailureCount < 2) return;

    runtime.connectorOperational = false;
    bridgeRecoveryStartedAt = Date.now();
    bridgeFailureCount = 0;
    console.warn(
      'WhatsApp Web потерял рабочий bridge при состоянии CONNECTED. ' +
      'Автоответы временно приостановлены; запускаю восстановление без сброса сессии.'
    );
    syncing.publishConnectorState('connecting').catch(() => {});
    startReadyFallback();
  } finally {
    bridgeWatchdogBusy = false;
  }
}

async function restartAfterStuckBridge(reason) {
  if (runtime.shutdownStarted) return;
  runtime.shutdownStarted = true;
  runtime.connectorOperational = false;
  console.error(`${reason}. Перезапускаю процесс коннектора через systemd; сохранённая сессия остаётся на диске.`);
  if (outboundTimer) clearInterval(outboundTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (fastInboundTimer) clearInterval(fastInboundTimer);
  if (groupSyncTimer) clearInterval(groupSyncTimer);
  if (performanceTimer) clearInterval(performanceTimer);
  if (bridgeWatchdogTimer) clearInterval(bridgeWatchdogTimer);
  stopReadyFallback();
  calling.stopIncomingCallPolling();
  try {
    await syncing.publishConnectorState('connecting');
  } catch (_) {}
  try {
    const timeout = new Promise((resolve) => setTimeout(resolve, 8000));
    await Promise.race([client.destroy(), timeout]);
  } catch (_) {}
  process.exit(1);
}

function stopReadyFallback() {
  if (readyFallbackTimer) {
    clearInterval(readyFallbackTimer);
    readyFallbackTimer = null;
  }
}

function connectorServiceRssMb() {
  try {
    const raw = fs.readFileSync('/sys/fs/cgroup/system.slice/edinaya-ochered-whatsapp.service/memory.current', 'utf8').trim();
    const bytes = Number(raw || 0);
    if (Number.isFinite(bytes) && bytes > 0) return bytes / 1024 / 1024;
  } catch (_) {}
  return process.memoryUsage().rss / 1024 / 1024;
}

async function reportPerformance() {
  if (runtime.shutdownStarted) return;
  try {
    const memory = process.memoryUsage();
    const nodeRssMb = memory.rss / 1024 / 1024;
    const rssMb = connectorServiceRssMb();
    await postJson('/api/performance-telemetry', {
      rss_mb: Math.round(rssMb * 10) / 10,
      node_rss_mb: Math.round(nodeRssMb * 10) / 10,
      heap_mb: Math.round(memory.heapUsed / 1024 / 1024 * 10) / 10,
      external_mb: Math.round(memory.external / 1024 / 1024 * 10) / 10,
      pending_media: pendingMediaMessages.size,
      outgoing_probe: outgoingMediaProbe.size,
      fast_sync_busy: runtime.fastSyncBusy,
      pending_media_busy: runtime.pendingMediaBusy,
      contact_cooldown_ms: PERF_CONTACT_COOLDOWN_MS,
      recovery_interval_ms: PERF_RECOVERY_INTERVAL_MS,
      ram_restart_mb: PERF_RAM_RESTART_MB,
    });
    if (PERF_RAM_RESTART_MB > 0 && rssMb >= PERF_RAM_RESTART_MB) highMemorySamples += 1;
    else highMemorySamples = 0;
    // Раньше процесс автоматически завершался после трёх замеров выше RAM-порога.
    // На рабочих системах это выглядело как падение коннектора сразу после
    // входящего/исходящего сообщения, а systemd тут же поднимал его снова.
    // Оставляем телеметрию и предупреждение, но больше не завершаем процесс.
    if (highMemorySamples >= 3 && runtime.connectorOperational && !runtime.outboundBusy && !runtime.fastSyncBusy && !runtime.pendingMediaBusy) {
      console.warn(`Память WhatsApp-коннектора ${rssMb.toFixed(0)} МБ выше порога ${PERF_RAM_RESTART_MB} МБ. Автоперезапуск отключён, коннектор продолжает работу.`);
      highMemorySamples = 0;
    }
  } catch (_) {}
}

async function activateConnector(source = 'ready') {
  if (runtime.connectorOperational) return;
  runtime.connectorOperational = true;
  runtime.connectorReadyAt = Math.floor(Date.now() / 1000);
  runtime.startupInboundRecoveryStartedAt = Date.now();
  runtime.startupLiveOnlyUntil = runtime.startupInboundRecoveryStartedAt + STARTUP_LIVE_ONLY_MS;
  runtime.startupInboundRecoveryUntil = runtime.startupInboundRecoveryStartedAt + 5 * 60 * 1000;
  stopReadyFallback();
  console.log(
    source === 'ready'
      ? '\nWhatsApp QR-коннектор готов.'
      : `\nWhatsApp подключён. Рабочий режим включён по состоянию ${source}, даже если событие ready не пришло.`
  );
  console.log('Заявки создаются только после выбора пункта цифрового меню.');
  console.log('Префикс не требуется.');
  console.log('Новые сообщения групп передаются во вкладку «Группы».');
  console.log('Ответы из карточек заявок отправляются через это подключение.');
  console.log(`Быстрый запуск: live-сообщения и автоответы доступны сразу; фоновая история стартует через ${Math.round(STARTUP_LIVE_ONLY_MS / 1000)} сек.`);
  console.log(`После прогрева проверяю не более 30 минут истории (до ${STARTUP_BACKFILL_MODEL_LIMIT} последних моделей, по ${STARTUP_BACKFILL_BATCH} за проход).`);
  syncing.publishConnectorState('ready').catch((error) => {
    console.warn('Не удалось опубликовать состояние ready:', error.message || error);
  });
  // Сначала поднимаем лёгкие live-процессы, а полный обход групп запускаем
  // чуть позже. Так вкладки получают сохранённые данные и новые сообщения,
  // пока WhatsApp Web спокойно заканчивает инициализацию.
  // Keep only the local connection/control sync immediate. Every WhatsApp-Web
  // heavy task is delayed until the 12-second live-only window is over.
  syncing.syncConnection().catch(() => {});
  setTimeout(() => {
    syncing.runFastSync().catch(() => {});
  }, STARTUP_LIVE_ONLY_MS + 250);
  setTimeout(() => {
    syncing.syncContactDiscovery().catch(() => {});
  }, STARTUP_LIVE_ONLY_MS + 15000);
  setTimeout(() => {
    if (backgroundWorkAllowed()) {
      grouping.syncGroups().catch((error) => {
        noteBrowserPressure(error, 'initial groups');
        console.warn('Первичная синхронизация групп пока недоступна:', error.message || error);
      });
    }
  }, 60000);
  if (!outboundTimer) {
    outboundTimer = setInterval(sending.pollOutbound, 150);
    setTimeout(() => { void sending.pollOutbound(); }, 100);
  }
  if (!heartbeatTimer) {
    heartbeatTimer = setInterval(() => { void syncing.syncConnection(); }, 10000);
  }
  if (!performanceTimer) {
    performanceTimer = setInterval(() => { void reportPerformance(); }, 15000);
    setTimeout(() => { void reportPerformance(); }, 1200);
  }
  if (!fastInboundTimer) {
    fastInboundTimer = setInterval(() => { void syncing.runFastSync(); }, PERF_RECOVERY_INTERVAL_MS);
    setTimeout(() => { void syncing.runFastSync(); }, 250);
    console.log(`Draft Finish v13 активен: «готово/Готово/Дайын» имеет приоритет меню — без debounce/cooldown; Fast Startup сохранён. Резервная проверка ${PERF_RECOVERY_INTERVAL_MS} мс.`);
  }
  if (!groupSyncTimer) {
    groupSyncTimer = setInterval(() => {
      grouping.syncGroups().catch((error) => {
        // A local API/socket failure must never become an unhandled rejection
        // that kills the whole Node connector.
        console.warn('Фоновая синхронизация групп временно недоступна:', error.message || error);
      });
    }, 60 * 60 * 1000);
  }
  if (!bridgeWatchdogTimer) {
    bridgeWatchdogTimer = setInterval(() => { void checkBridgeHealth(); }, 5000);
    setTimeout(() => { void checkBridgeHealth(); }, 3000);
  }
  setTimeout(() => {
    if (!runtime.shutdownStarted && runtime.connectorOperational) calling.startIncomingCallPolling();
  }, STARTUP_LIVE_ONLY_MS + 3000);
}

async function checkReadyFallback() {
  if (runtime.connectorOperational || runtime.shutdownStarted) return;
  try {
    const state = await client.getState();
    const normalized = String(state || '').toUpperCase();
    const elapsed = Math.floor((Date.now() - readyFallbackStartedAt) / 1000);

    if (normalized === 'CONNECTED') {
      const bridgeReady = await whatsappBridgeReady();
      if (bridgeReady) {
        console.warn('WhatsApp сообщает CONNECTED и рабочий bridge доступен. Включаю рабочий режим.');
        bridgeRecoveryStartedAt = 0;
        await activateConnector('CONNECTED+bridge');
        return;
      }
      if (elapsed > 0 && elapsed % 15 < 4) {
        console.warn(`WhatsApp CONNECTED, но bridge ещё восстанавливается (${elapsed} сек.).`);
      }
      if (elapsed >= 90) {
        await restartAfterStuckBridge('WhatsApp остался CONNECTED без рабочего bridge более 90 секунд');
      }
      return;
    }

    if (elapsed > 0 && elapsed % 30 < 6) {
      console.log(`Ожидание готовности WhatsApp: ${normalized || 'UNKNOWN'}, ${elapsed} сек.`);
    }
    if (elapsed >= 120) {
      await restartAfterStuckBridge(`WhatsApp не перешёл в рабочее состояние за ${elapsed} секунд`);
    }
  } catch (error) {
    const elapsed = Math.floor((Date.now() - readyFallbackStartedAt) / 1000);
    if (elapsed > 0 && elapsed % 30 < 6) {
      console.warn('Проверка состояния WhatsApp во время запуска:', error.message || error);
    }
    if (elapsed >= 120) {
      await restartAfterStuckBridge(`Проверка состояния WhatsApp не восстановилась за ${elapsed} секунд`);
    }
  }
}

function startReadyFallback() {
  if (readyFallbackTimer || runtime.shutdownStarted) return;
  readyFallbackStartedAt = Date.now();
  readyFallbackTimer = setInterval(() => { void checkReadyFallback(); }, 3000);
  setTimeout(() => { void checkReadyFallback(); }, 800);
}

client.on('authenticated', () => {
  const now = Date.now();
  if (now - lastAuthenticatedLogAt > 3000) {
    console.log('WhatsApp подтвердил подключение. Проверяю рабочий bridge...');
    lastAuthenticatedLogAt = now;
  }
  if (authRecoveryBusy || runtime.shutdownStarted) return;
  authRecoveryBusy = true;
  void (async () => {
    try {
      if (runtime.connectorOperational) {
        const ok = await whatsappBridgeReady();
        if (ok) return;
        runtime.connectorOperational = false;
        bridgeRecoveryStartedAt = Date.now();
        console.warn('WhatsApp Web перезагрузил контекст. Перевожу коннектор в режим восстановления.');
      }
      await syncing.publishConnectorState('connecting');
      startReadyFallback();
    } finally {
      authRecoveryBusy = false;
    }
  })();
});

client.on('change_state', (state) => {
  const normalized = String(state || '').toUpperCase();
  console.log(`Состояние WhatsApp: ${normalized || 'UNKNOWN'}`);

  // QUEUE_CONNECTOR_STATE_RECOVERY_20260930
  // WhatsApp Web may briefly go OPENING/PAIRING while the Chromium page reloads.
  // During that window getState()/the UI can still look connected even though
  // WWebJS has lost its execution context. Mark the connector non-operational
  // immediately so the UI does not show a stale green state and auto-reply work
  // is not attempted against a detached frame.
  if (normalized === 'OPENING' || normalized === 'PAIRING' || normalized === 'TIMEOUT') {
    if (runtime.connectorOperational) {
      runtime.connectorOperational = false;
      bridgeFailureCount = 0;
      bridgeRecoveryStartedAt = Date.now();
      console.warn(`WhatsApp перешёл в ${normalized}. Рабочий bridge временно недоступен; ожидаю восстановление.`);
      syncing.publishConnectorState('connecting').catch(() => {});
    }
    startReadyFallback();
    return;
  }

  if (normalized === 'CONNECTED') {
    // Never trust CONNECTED alone. checkReadyFallback() verifies that WWebJS,
    // sendMessage and WAWebCollections are actually injected before re-enabling.
    if (!runtime.connectorOperational) {
      startReadyFallback();
      setTimeout(() => { void checkReadyFallback(); }, 250);
    }
    return;
  }

  if (normalized && normalized !== 'CONNECTED' && runtime.connectorOperational) {
    runtime.connectorOperational = false;
    bridgeFailureCount = 0;
    bridgeRecoveryStartedAt = Date.now();
    syncing.publishConnectorState('connecting').catch(() => {});
    startReadyFallback();
  }
});

client.on('ready', () => {
  bridgeFailureCount = 0;
  bridgeRecoveryStartedAt = 0;
  activateConnector('ready');
});

client.on('auth_failure', (message) => {
  console.error('Ошибка авторизации WhatsApp:', message);
  syncing.publishConnectorState('offline');
});

client.on('disconnected', (reason) => {
  runtime.connectorOperational = false;
  bridgeFailureCount = 0;
  bridgeRecoveryStartedAt = 0;
  stopReadyFallback();
  calling.stopIncomingCallPolling();
  console.error('WhatsApp отключил сессию:', reason);
  console.error('Запусти коннектор снова и при необходимости отсканируй новый QR.');
  postJson('/api/chat-list-sync', { connected: false }).catch(() => {});
  syncing.publishConnectorState('offline');
});

async function gracefulShutdown(signal) {
  if (runtime.shutdownStarted) return;
  runtime.shutdownStarted = true;
  runtime.connectorOperational = false;
  console.log(`\nОстанавливаю QR-коннектор (${signal})...`);
  if (outboundTimer) clearInterval(outboundTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (fastInboundTimer) clearInterval(fastInboundTimer);
  if (groupSyncTimer) clearInterval(groupSyncTimer);
  if (performanceTimer) clearInterval(performanceTimer);
  if (bridgeWatchdogTimer) clearInterval(bridgeWatchdogTimer);
  stopReadyFallback();
  calling.stopIncomingCallPolling();

  const timeout = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  try {
    await Promise.race([syncing.publishConnectorState('offline'), timeout(3000)]);
  } catch (error) {
    console.warn('Не удалось сообщить сайту об остановке:', error.message || error);
  }
  try {
    await Promise.race([client.destroy(), timeout(15000)]);
  } catch (error) {
    console.warn('WhatsApp Web завершён принудительно:', error.message || error);
  }
  process.exit(0);
}
