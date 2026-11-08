'use strict';
// WhatsApp-коннектор, точка входа. Подключает части и запускает WhatsApp Web:
// connector_common.js — общая основа, connector_send.js — отправка, connector_receive.js — приём,
// connector_sync.js — синхронизация, connector_actions.js — удаление и пересылка,
// connector_lifecycle.js — запуск, состояние и завершение.

const {
  runtime, client, inboundSpool, postJsonOnce, postJsonWithRetry,
} = require('./connector_common');
const syncing = require('./connector_sync');
const lifecycle = require('./connector_lifecycle');
require('./connector_send');
require('./connector_receive');
require('./connector_actions');

process.on('SIGINT', () => { void lifecycle.gracefulShutdown('SIGINT'); });
process.on('SIGTERM', () => { void lifecycle.gracefulShutdown('SIGTERM'); });

console.log('Запускаю WhatsApp Web. Восстанавливаю сохранённую сессию...');
syncing.publishConnectorState('connecting').catch(() => {});

// If WhatsApp Web destroys its execution context while initialize() is running,
// do not leave a dead Node process alive because background timers keep the
// event loop open. Exit cleanly with code 1 so systemd Restart=always starts a
// fresh Chromium process with the SAME saved LocalAuth session.
client.initialize().catch((error) => {
  runtime.connectorOperational = false;
  const reason = error && error.message ? error.message : String(error);
  console.error('Не удалось запустить WhatsApp Web:', reason);
  syncing.publishConnectorState('connecting').catch(() => {});
  console.error('Перезапускаю коннектор через systemd через 2 сек. Сессия WhatsApp сохраняется.');
  setTimeout(() => process.exit(1), 2000);
});

// Disk queue survives a connector restart and retains failed jobs for later attempts.
let inboundRecoveryBusy=false;
const inboundRecoveryTimer=setInterval(async()=>{
  if(inboundRecoveryBusy || runtime.shutdownStarted || !runtime.connectorOperational)return;
  inboundRecoveryBusy=true;
  try {
    await inboundSpool.drain(payload=>postJsonWithRetry('/api/whatsapp',payload),async(payload,result)=>{
      if(result.reply && !result.duplicate && !result.silent && !result.manual_contact)
        await postJsonOnce('/api/inbound-recovery-reply',{external_id:payload.external_id,chat_id:payload.chat_id,reply:result.reply});
    });
  } catch(error){console.error('Очередь входящих требует проверки:',error.message||error);}
  finally{inboundRecoveryBusy=false;}
},3000);
inboundRecoveryTimer.unref();
