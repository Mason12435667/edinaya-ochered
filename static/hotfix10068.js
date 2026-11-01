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
  let previewUrls = [];
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

  const previewStrip = (() => {
    const node = document.createElement("div");
    node.className = "queue-attachment-preview";
    node.dataset.attachmentPreview = "";
    node.setAttribute("aria-label", "Прикреплённые файлы");
    node.hidden = true;
    node.style.display = "none";

    // 1.00.6.10: размещаем ленту рядом с уже видимой строкой статуса.
    // В старой разметке вставка возле data-media-name попадала в служебную
    // область grid-компоновки и миниатюры фактически оказывались невидимыми.
    // composeStatus гарантированно находится в нижней видимой части composer.
    if (composeStatus?.parentNode === composer) composeStatus.insertAdjacentElement("beforebegin", node);
    else composer.append(node);
    return node;
  })();

  const modal = (() => {
    const node = document.createElement("div");
    node.className = "queue-preview-modal";
    node.hidden = true;
    node.innerHTML = '<button class="queue-preview-close" type="button" aria-label="Закрыть">×</button><img alt="Предпросмотр вложения">';
    document.body.append(node);
    const close = () => { node.hidden = true; node.querySelector("img")?.removeAttribute("src"); };
    node.addEventListener("click", (event) => { if (event.target === node) close(); });
    node.querySelector(".queue-preview-close")?.addEventListener("click", close);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !node.hidden) close(); });
    node.openImage = (src, alt) => {
      const image = node.querySelector("img");
      if (!image) return;
      image.src = src;
      image.alt = alt || "Предпросмотр вложения";
      node.hidden = false;
    };
    return node;
  })();

  function revokePreviewUrls() {
    for (const url of previewUrls) {
      try { URL.revokeObjectURL(url); } catch (_) {}
    }
    previewUrls = [];
  }

  function renderPending() {
    ensurePreviewMounted();
    revokePreviewUrls();
    previewStrip.replaceChildren();

    if (!pendingFiles.length) {
      previewStrip.hidden = true;
      previewStrip.style.display = "none";
      if (mediaName) {
        mediaName.textContent = "";
        mediaName.removeAttribute("title");
      }
      return;
    }

    previewStrip.hidden = false;
    previewStrip.style.display = "flex";
    const total = pendingFiles.reduce((sum, file) => sum + Number(file.size || 0), 0);
    const noun = pendingFiles.length === 1 ? "файл" : "файлов";
    if (mediaName) {
      mediaName.textContent = `${pendingFiles.length} ${noun} · ${humanSize(total)}`;
      mediaName.title = pendingFiles.map((file, index) => `${index + 1}. ${file.name || "Скриншот"}`).join("\n");
    }

    pendingFiles.forEach((file, index) => {
      const card = document.createElement("div");
      card.className = "queue-attachment-card";
      card.dataset.attachmentIndex = String(index);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "queue-attachment-remove";
      remove.textContent = "×";
      remove.title = "Убрать это вложение";
      remove.setAttribute("aria-label", `Убрать ${file.name || `вложение ${index + 1}`}`);
      remove.addEventListener("click", () => {
        if (sending) return;
        const removed = pendingFiles.splice(index, 1)[0];
        renderPending();
        composeStatus.textContent = pendingFiles.length
          ? `Прикреплено файлов: ${pendingFiles.length}. Можно добавить ещё или отправить.`
          : "Вложения убраны";
        toast(`Убрано: ${removed?.name || "вложение"}`);
      });
      card.append(remove);

      const isImage = String(file.type || "").toLowerCase().startsWith("image/");
      if (isImage) {
        const url = URL.createObjectURL(file);
        previewUrls.push(url);
        const image = document.createElement("img");
        image.className = "queue-attachment-thumb";
        image.src = url;
        image.alt = file.name || `Скриншот ${index + 1}`;
        image.title = "Нажмите, чтобы открыть крупнее";
        image.addEventListener("click", () => modal.openImage?.(url, image.alt));
        card.append(image);
      } else {
        const icon = document.createElement("div");
        icon.className = "queue-attachment-file";
        icon.textContent = "📄";
        card.append(icon);
      }

      const meta = document.createElement("div");
      meta.className = "queue-attachment-meta";
      const name = document.createElement("span");
      name.className = "queue-attachment-name";
      name.textContent = file.name || (isImage ? `Скриншот ${index + 1}` : `Файл ${index + 1}`);
      name.title = name.textContent;
      const size = document.createElement("span");
      size.className = "queue-attachment-size";
      size.textContent = humanSize(file.size);
      meta.append(name, size);
      card.append(meta);
      previewStrip.append(card);
    });

    if (pendingFiles.length > 1) {
      const actions = document.createElement("div");
      actions.className = "queue-attachment-actions";
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "queue-attachment-clear";
      clear.textContent = "Убрать все";
      clear.addEventListener("click", () => {
        if (sending) return;
        pendingFiles = [];
        renderPending();
        composeStatus.textContent = "Все вложения убраны";
      });
      actions.append(clear);
      previewStrip.append(actions);
    }
  }

  function ensurePreviewMounted() {
    if (previewStrip.parentNode !== composer) {
      if (composeStatus?.parentNode === composer) composeStatus.insertAdjacentElement("beforebegin", previewStrip);
      else composer.append(previewStrip);
    }
  }

  const composerObserver = new MutationObserver(() => {
    if (pendingFiles.length) {
      ensurePreviewMounted();
      previewStrip.hidden = false;
      previewStrip.style.display = "flex";
    }
  });
  composerObserver.observe(composer, {childList: true});

  function validFiles(files) {
    return Array.from(files || []).filter((file) => file instanceof File);
  }

  function appendPending(files) {
    const incoming = validFiles(files);
    if (!incoming.length) return 0;
    const remaining = Math.max(0, MAX_BATCH - pendingFiles.length);
    const accepted = incoming.slice(0, remaining);
    pendingFiles.push(...accepted);
    fileInput.value = "";
    renderPending();
    if (incoming.length > accepted.length) toast(`За один раз можно отправить до ${MAX_BATCH} файлов`);
    if (accepted.length) composeStatus.textContent = `Прикреплено файлов: ${pendingFiles.length}. Можно убрать лишнее, добавить ещё или нажать «Отправить».`;
    return accepted.length;
  }

  function clipboardImages(event) {
    const data = event.clipboardData;
    if (!data) return [];
    const fromItems = Array.from(data.items || [])
      .filter((item) => item && item.kind === "file" && String(item.type || "").startsWith("image/"))
      .map((item) => item.getAsFile?.())
      .filter((file) => file instanceof File);
    if (fromItems.length) return fromItems;
    return validFiles(data.files).filter((file) => String(file.type || "").startsWith("image/"));
  }

  // 1.00.6.9 перехватывает и одиночный файл, чтобы у любого вложения был
  // одинаковый предпросмотр и кнопка удаления до отправки.
  fileInput.addEventListener("change", (event) => {
    const files = validFiles(event.target.files);
    if (!files.length) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    appendPending(files);
  }, true);

  composer.addEventListener("drop", (event) => {
    const files = validFiles(event.dataTransfer?.files);
    if (!files.length) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    appendPending(files);
  }, true);

  composer.addEventListener("paste", (event) => {
    const images = clipboardImages(event);
    if (!images.length) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    appendPending(images);
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
    composeStatus.textContent = `Отправка файлов: ${index + 1}/${total} · ${file.name || "Скриншот"}`;
    const response = await fetch(`/chat-media-send-raw?${params.toString()}`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Requested-With": "fetch" },
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok || !payload.queued) throw new Error(String(payload.error || `Ошибка HTTP ${response.status}`));
    return payload;
  }

  composer.addEventListener("submit", async (event) => {
    if (pendingFiles.length < 1 || sending) return;
    event.preventDefault();
    event.stopImmediatePropagation();

    const chatId = String(chatInput?.value || "").trim();
    if (!chatId) { toast("Сначала выберите чат или группу"); return; }

    const batch = pendingFiles.slice();
    const caption = String(textarea?.value || "");
    const replyTo = String(replyInput?.value || "");
    const mentions = String(mentionsInput?.value || "");
    sending = true;
    previewStrip.querySelectorAll("button").forEach((button) => { button.disabled = true; });
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
      previewStrip.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    }
  }, true);

  window.QueueSequentialScreenshots = {
    get count() { return pendingFiles.length; },
    clear() { if (!sending) { pendingFiles = []; renderPending(); } },
    remove(index) {
      const position = Number(index);
      if (sending || !Number.isInteger(position) || position < 0 || position >= pendingFiles.length) return false;
      pendingFiles.splice(position, 1);
      renderPending();
      return true;
    },
  };
})();
