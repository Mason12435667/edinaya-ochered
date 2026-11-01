(() => {
  "use strict";
  const page = document.querySelector("[data-conversation-page]");
  if (!page) return;
  const composer = page.querySelector("[data-chat-composer]");
  if (!composer) return;
  const fileInput = composer.querySelector("[data-media-input]");
  if (!fileInput) return;

  const MAX_BATCH = 20;
  const mediaName = composer.querySelector("[data-media-name]");
  const textarea = composer.querySelector('textarea[name="message"]');
  const chatInput = composer.querySelector("[data-chat-id]");
  const replyInput = composer.querySelector("[data-reply-to]");
  const mentionsInput = composer.querySelector("[data-mentions-input]");
  const sendButton = composer.querySelector('button[type="submit"]');
  const composeStatus = composer.querySelector(".compose-status") || (() => {
    const node = document.createElement("div");
    node.className = "compose-status";
    composer.append(node);
    return node;
  })();

  let pendingFiles = [];
  let sending = false;
  fileInput.multiple = true;

  const toast = (message) => window.QueueUI?.toast?.(message) || console.info(message);
  const humanSize = (bytes) => {
    let value = Number(bytes) || 0;
    const units = ["Б", "КБ", "МБ", "ГБ"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${index ? value.toFixed(value >= 10 ? 1 : 2) : Math.round(value)} ${units[index]}`;
  };

  function renderPending() {
    if (!mediaName) return;
    if (!pendingFiles.length) {
      mediaName.textContent = "";
      mediaName.removeAttribute("title");
      return;
    }
    const total = pendingFiles.reduce((sum, file) => sum + Number(file.size || 0), 0);
    mediaName.textContent = `${pendingFiles.length} файлов · ${humanSize(total)}`;
    mediaName.title = pendingFiles.map((file) => file.name).join("\n");
  }

  function setPending(files) {
    pendingFiles = Array.from(files || []).filter((file) => file instanceof File).slice(0, MAX_BATCH);
    fileInput.value = "";
    renderPending();
    if (pendingFiles.length) {
      composeStatus.textContent = `Выбрано файлов: ${pendingFiles.length}. Нажмите «Отправить».`;
    }
  }

  fileInput.addEventListener("change", (event) => {
    const files = Array.from(event.target.files || []);
    if (files.length <= 1) {
      pendingFiles = [];
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    if (files.length > MAX_BATCH) toast(`За один раз можно отправить до ${MAX_BATCH} файлов`);
    setPending(files);
  }, true);

  composer.addEventListener("drop", (event) => {
    const files = Array.from(event.dataTransfer?.files || []);
    if (files.length <= 1) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (files.length > MAX_BATCH) toast(`За один раз можно отправить до ${MAX_BATCH} файлов`);
    setPending(files);
  }, true);

  composer.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.files || []).filter((file) => file.type.startsWith("image/"));
    if (files.length <= 1) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    setPending(files);
  }, true);

  async function queueOne(file, index, total, caption, replyTo, mentions) {
    const chatId = String(chatInput?.value || "").trim();
    const params = new URLSearchParams({
      chat_id: chatId,
      message: index === 0 ? caption : "",
      reply_to: index === 0 ? replyTo : "",
      mimetype: String(file.type || "application/octet-stream"),
      filename: String(file.name || `attachment-${index + 1}`),
      mentions: index === 0 ? mentions : "",
    });
    composeStatus.textContent = `Отправка файлов: ${index + 1}/${total} · ${file.name}`;
    const response = await fetch(`/chat-media-send-raw?${params.toString()}`, {
      method: "POST",
      body: file,
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Requested-With": "fetch",
      },
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok || !payload.queued) {
      throw new Error(String(payload.error || `Ошибка HTTP ${response.status}`));
    }
    return payload;
  }

  composer.addEventListener("submit", async (event) => {
    if (pendingFiles.length <= 1 || sending) return;
    event.preventDefault();
    event.stopImmediatePropagation();

    const chatId = String(chatInput?.value || "").trim();
    if (!chatId) {
      toast("Сначала выберите чат или группу");
      return;
    }

    const batch = pendingFiles.slice();
    const caption = String(textarea?.value || "");
    const replyTo = String(replyInput?.value || "");
    const mentions = String(mentionsInput?.value || "");
    sending = true;
    if (sendButton) sendButton.disabled = true;
    fileInput.disabled = true;
    let sent = 0;
    try {
      for (let index = 0; index < batch.length; index += 1) {
        await queueOne(batch[index], index, batch.length, caption, replyTo, mentions);
        sent += 1;
      }
      pendingFiles = [];
      if (textarea) {
        textarea.value = "";
        textarea.dispatchEvent(new Event("input", {bubbles: true}));
      }
      if (mentionsInput) mentionsInput.value = "";
      const cancelReply = composer.querySelector("[data-reply-cancel]");
      if (cancelReply && !cancelReply.closest("[hidden]")) cancelReply.click();
      else if (replyInput) replyInput.value = "";
      renderPending();
      composeStatus.textContent = `Файлы поставлены в очередь: ${sent}`;
      toast(`Отправка ${sent} файлов поставлена в очередь`);
      window.setTimeout(() => {
        if (composeStatus.textContent.includes("поставлены в очередь")) composeStatus.textContent = "";
      }, 2500);
    } catch (error) {
      pendingFiles = batch.slice(sent);
      renderPending();
      if (sent > 0 && textarea) textarea.value = "";
      composeStatus.textContent = `Отправлено ${sent}/${batch.length}. Ошибка: ${String(error?.message || error)}`;
      toast(composeStatus.textContent);
    } finally {
      sending = false;
      if (sendButton) sendButton.disabled = false;
      fileInput.disabled = false;
    }
  }, true);
})();
