// Единая обёртка над fetch: CSRF-токен для своего сайта и независимые наблюдатели ответов.
// Загружается раньше остальных скриптов. Каждый наблюдатель получает свою копию ответа.
(() => {
  "use strict";
  if (window.QueueHTTP) return;
  const nativeFetch = window.fetch.bind(window);
  const observers = new Set();
  const token = () => document.querySelector('meta[name="queue-csrf"]')?.content || "";

  function publish(kind, details) {
    for (const observer of observers) {
      if (typeof observer[kind] !== "function") continue;
      try {
        if (observer.match && !observer.match(details)) continue;
        const event = kind === "response"
          ? {...details, response: details.response.clone()}
          : details;
        Promise.resolve(observer[kind](event)).catch(() => {});
      } catch (_) {
        // Presentation failures must not change a completed network operation.
      }
    }
  }

  window.QueueHTTP = Object.freeze({
    subscribe(observer) {
      observers.add(observer);
      return () => observers.delete(observer);
    },
  });

  window.fetch = async (input, init = {}) => {
    const request = input instanceof Request ? input : null;
    const method = String(init.method || request?.method || "GET").toUpperCase();
    const url = new URL(request ? request.url : String(input), location.href);
    if (method !== "GET" && method !== "HEAD" && url.origin === location.origin) {
      const headers = new Headers(init.headers || request?.headers || undefined);
      if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token());
      init = {...init, headers};
    }
    let response;
    try {
      response = await nativeFetch(input, init);
    } catch (error) {
      publish("error", {url, method, error});
      throw error;
    }
    publish("response", {url, method, response});
    return response;
  };
})();
