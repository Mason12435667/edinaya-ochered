// Просмотр фото и видео поверх страницы.
window.QueueLightbox = (() => {
  function setupMediaLightbox() {
    let overlay = null;
    let activeCleanup = null;
    function close() {
      if (typeof activeCleanup === "function") {
        try { activeCleanup(); } catch (_) {}
      }
      activeCleanup = null;
      if (overlay) overlay.remove();
      overlay = null;
      document.body.classList.remove("media-lightbox-open");
    }
    function open(src, name) {
      close();
      overlay = document.createElement("div");
      overlay.className = "media-lightbox";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      const frame = document.createElement("div");
      frame.className = "media-lightbox-frame";
      const closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "media-lightbox-close";
      closeButton.textContent = "×";
      closeButton.title = "Закрыть";
      closeButton.addEventListener("click", close);

      const viewport = document.createElement("div");
      viewport.className = "media-lightbox-viewport";
      const image = document.createElement("img");
      image.src = src;
      image.alt = name || "Фото";
      image.draggable = false;
      viewport.append(image);

      const controls = document.createElement("div");
      controls.className = "media-lightbox-controls";
      const zoomOut = document.createElement("button");
      zoomOut.type = "button";
      zoomOut.className = "button secondary";
      zoomOut.textContent = "−";
      zoomOut.title = "Уменьшить";
      const zoomIn = document.createElement("button");
      zoomIn.type = "button";
      zoomIn.className = "button secondary";
      zoomIn.textContent = "+";
      zoomIn.title = "Увеличить";
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "button secondary";
      reset.textContent = "100%";
      reset.title = "Сбросить масштаб и позицию";
      const label = document.createElement("span");
      const hint = document.createElement("span");
      hint.className = "media-lightbox-hint";
      hint.textContent = "Зажмите левую кнопку мыши на фото и тяните в любую сторону";
      const range = document.createElement("input");
      range.type = "range";
      range.min = "40";
      range.max = "300";
      range.step = "10";
      range.value = "100";

      let zoom = 100;
      let panX = 0;
      let panY = 0;
      let drag = null;

      function clampZoom(value) {
        return Math.max(40, Math.min(300, Number(value || 100)));
      }
      function panBounds() {
        const scale = zoom / 100;
        const width = image.offsetWidth * scale;
        const height = image.offsetHeight * scale;
        // Разрешаем тянуть картинку при любом масштабе. Если изображение меньше
        // окна, оно двигается внутри свободного пространства. Если больше, границы
        // соответствуют области, которую реально можно осмотреть.
        return {
          x: Math.max(48, Math.abs(width - viewport.clientWidth) / 2),
          y: Math.max(48, Math.abs(height - viewport.clientHeight) / 2),
        };
      }
      function clampPan() {
        const bounds = panBounds();
        panX = Math.max(-bounds.x, Math.min(bounds.x, panX));
        panY = Math.max(-bounds.y, Math.min(bounds.y, panY));
        viewport.classList.add("is-draggable");
        return true;
      }
      function applyTransform() {
        clampPan();
        image.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${zoom / 100})`;
        label.textContent = `Масштаб ${zoom}%`;
        range.value = String(zoom);
      }
      function updateZoom(value, resetPan = false) {
        const previous = zoom || 100;
        zoom = clampZoom(value);
        if (resetPan) {
          panX = 0;
          panY = 0;
        } else if (previous > 0) {
          const ratio = zoom / previous;
          panX *= ratio;
          panY *= ratio;
        }
        applyTransform();
      }
      function stopDragging(event) {
        if (!drag) return;
        if (event && drag.pointerId !== undefined && event.pointerId !== undefined && drag.pointerId !== event.pointerId) return;
        drag = null;
        viewport.classList.remove("is-dragging");
      }

      range.addEventListener("input", () => updateZoom(range.value));
      zoomOut.addEventListener("click", () => updateZoom(zoom - 10));
      zoomIn.addEventListener("click", () => updateZoom(zoom + 10));
      reset.addEventListener("click", () => updateZoom(100, true));
      image.addEventListener("dblclick", () => updateZoom(100, true));
      viewport.addEventListener("wheel", (event) => {
        event.preventDefault();
        updateZoom(zoom + (event.deltaY < 0 ? 10 : -10));
      }, { passive: false });

      viewport.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !clampPan()) return;
        drag = {
          pointerId: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          panX,
          panY,
        };
        viewport.classList.add("is-dragging");
        try { viewport.setPointerCapture(event.pointerId); } catch (_) {}
        event.preventDefault();
      });
      viewport.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        panX = drag.panX + (event.clientX - drag.x);
        panY = drag.panY + (event.clientY - drag.y);
        applyTransform();
        event.preventDefault();
      });
      viewport.addEventListener("pointerup", stopDragging);
      viewport.addEventListener("pointercancel", stopDragging);
      viewport.addEventListener("lostpointercapture", stopDragging);
      window.addEventListener("resize", applyTransform);
      image.addEventListener("load", () => updateZoom(100, true), { once: true });

      controls.append(zoomOut, zoomIn, reset, label, range, hint);
      const caption = document.createElement("div");
      caption.className = "media-lightbox-caption";
      caption.textContent = name || "Фото";
      frame.append(closeButton, viewport, controls, caption);
      overlay.append(frame);
      overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
      document.body.append(overlay);
      document.body.classList.add("media-lightbox-open");
      if (image.complete) updateZoom(100, true);
      activeCleanup = () => {
        drag = null;
        window.removeEventListener("resize", applyTransform);
      };
    }
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-media-zoom]");
      if (!button) return;
      event.preventDefault();
      open(button.dataset.mediaZoom || "", button.dataset.mediaName || "Фото");
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && overlay) close(); });
  }



  return {setup: setupMediaLightbox};
})();
