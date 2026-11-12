// Поле отправки в чате: вложение из файла, буфера обмена или перетаскиванием, черновики и запрос отправки.
window.QueueChatComposer = (page) => {
  function clearAttachment() {
    if (page.mediaInput) page.mediaInput.value = "";
    if (page.mediaName) page.mediaName.textContent = "";
    page.attachmentRemove.hidden = true;
    page.attachmentBar.hidden = true;
  }

  function showAttachment() {
    const file = page.mediaInput?.files?.[0];
    if (file && (file.size > 512 * 1024 * 1024 || file.size === 0)) {
      page.composeStatus.textContent = "Выберите непустой файл размером до 512 МБ";
      clearAttachment();
      return;
    }
    if (page.mediaName) page.mediaName.textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} МБ` : "";
    page.attachmentRemove.hidden = !file;
    page.attachmentBar.hidden = !file;
  }

  // QUEUE_CLIPBOARD_PASTE_3_3_62: Win+Shift+S / clipboard image paste into the current chat.
  const queueBuildClipboardFileName = (mimeType) => {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const extMap = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"};
    const ext = extMap[mimeType || ""] || "";
    return `clipboard-${stamp}${ext}`;
  };
  const queueSetComposerFile = (file) => {
    if (!file || !page.mediaInput) return false;
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      page.mediaInput.files = transfer.files;
    } catch (_) {
      return false;
    }
    showAttachment();
    return true;
  };
  const queueTakeClipboardFile = (event) => {
    const clipboardItems = Array.from(event.clipboardData?.items || []);
    for (const item of clipboardItems) {
      if (item.kind !== "file") continue;
      const blob = item.getAsFile();
      if (!blob) continue;
      const mimeType = blob.type || item.type || "application/octet-stream";
      const originalName = typeof blob.name === "string" ? blob.name.trim() : "";
      // Win+Shift+S usually appears as generic image.png. Always wrap images
      // into a unique clipboard-* file so this send can be matched reliably.
      if (/^image\//.test(mimeType)) {
        try {
          return new File([blob], queueBuildClipboardFileName(mimeType), { type: mimeType, lastModified: Date.now() });
        } catch (_) {
          return blob;
        }
      }
      if (originalName) return blob;
      try {
        return new File([blob], queueBuildClipboardFileName(mimeType), { type: mimeType, lastModified: Date.now() });
      } catch (_) {
        return blob;
      }
    }
    return null;
  };
  const queueHandleClipboardPaste = (event) => {
    const file = queueTakeClipboardFile(event);
    if (!file) return;
    if (page.composer.dataset.sending === "1") {
      page.composeStatus.textContent = "Дождитесь завершения текущей отправки";
      return;
    }
    if (!page.selectedChatId) {
      page.composeStatus.textContent = "Сначала выберите чат или группу";
      return;
    }
    event.preventDefault();
    const ok = queueSetComposerFile(file);
    page.composeStatus.textContent = ok
      ? (/^image\//.test(file.type || "") ? "Скриншот из буфера прикреплён. Нажмите «Отправить»" : "Файл из буфера прикреплён. Нажмите «Отправить»")
      : "Не удалось прикрепить файл из буфера";
    page.composerTextarea?.focus();
  };

  const queueHasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  const queueSetDroppedFile = (file) => {
    if (!file || !page.mediaInput) return false;
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      page.mediaInput.files = transfer.files;
    } catch (_) {
      return false;
    }
    showAttachment();
    return true;
  };

  const draftKey = () => "queue-draft:" + page.selectedChatId;
  function saveDraft() {
    if (!page.selectedChatId || !page.composerTextarea) return;
    try {
      if (page.composerTextarea.value) localStorage.setItem(draftKey(), page.composerTextarea.value);
      else localStorage.removeItem(draftKey());
    } catch (_) {}
  }
  function restoreDraft() {
    if (!page.composerTextarea) return;
    try { page.composerTextarea.value = localStorage.getItem(draftKey()) || ""; } catch (_) {}
  }

  async function uploadRequest(route, body, signal, raw = false) {
    const response = await fetch(route, {
      method: "POST", signal,
      headers: {"Content-Type": raw ? "application/octet-stream" : "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="queue-csrf"]')?.content || ""},
      body: raw ? body : JSON.stringify(body),
    });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error("Сервер не принял загрузку. Проверьте соединение"); }
    if (!response.ok) throw new Error(result.error || "Ошибка загрузки файла");
    return result;
  }
  const requestKey = () => "queue-send-intent:" + page.selectedChatId;
  function sendIntent(text, file) {
    const signature=JSON.stringify([text,page.replyInput?.value || "",file ? [file.name,file.size,file.lastModified] : null]);
    let prior; try { prior=JSON.parse(localStorage.getItem(requestKey()) || "null"); } catch (_) {}
    if (prior && prior.signature === signature) return prior.id;
    const id=crypto.randomUUID ? crypto.randomUUID() : Date.now()+"-"+Math.random().toString(36).slice(2);
    try { localStorage.setItem(requestKey(),JSON.stringify({signature,id})); } catch (_) {}
    return id;
  }

  return {
    clearAttachment, queueHandleClipboardPaste, queueHasFiles, queueSetComposerFile, requestKey,
    restoreDraft, saveDraft, sendIntent, showAttachment, uploadRequest,
  };
};
