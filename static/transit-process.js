(() => {
  'use strict';

  const init = () => {
    const nav = document.querySelector('.tp-side-nav .tp-qnav-list');
    const sections = [...document.querySelectorAll('.tp-section[data-tp-section]')];
    if (!nav || !sections.length) return;

    const links = [...nav.querySelectorAll('.tp-qnav-row[href^="#tp-"]')];
    if (!links.length) return;

    const linkById = new Map();
    links.forEach((link) => {
      const href = link.getAttribute('href') || '';
      if (href.startsWith('#')) linkById.set(href.slice(1), link);
    });

    let currentId = '';
    let rafId = 0;

    const setActive = (id, ensureVisible = false) => {
      if (!id || id === currentId) return;
      currentId = id;
      links.forEach((link) => {
        const active = link === linkById.get(id);
        link.classList.toggle('is-active', active);
        if (active) {
          link.setAttribute('aria-current', 'location');
          if (ensureVisible) {
            try { link.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (_) {}
          }
        } else {
          link.removeAttribute('aria-current');
        }
      });
    };

    const visibleSections = () => sections.filter((section) => !section.hidden && linkById.has(section.id));

    const resolveCurrent = () => {
      const items = visibleSections();
      if (!items.length) return '';

      const marker = Math.max(120, Math.min(window.innerHeight * 0.30, 240));
      let best = null;
      let bestScore = Number.POSITIVE_INFINITY;

      for (const section of items) {
        const rect = section.getBoundingClientRect();
        if (rect.bottom < 90 || rect.top > window.innerHeight - 40) continue;

        let score;
        if (rect.top <= marker && rect.bottom >= marker) {
          score = Math.abs(rect.top - marker) * 0.25;
        } else {
          score = Math.min(Math.abs(rect.top - marker), Math.abs(rect.bottom - marker));
        }

        if (score < bestScore) {
          bestScore = score;
          best = section;
        }
      }

      if (!best) {
        best = items
          .map((section) => ({ section, distance: Math.abs(section.getBoundingClientRect().top - marker) }))
          .sort((a, b) => a.distance - b.distance)[0]?.section || null;
      }
      return best ? best.id : '';
    };

    const refresh = (ensureVisible = false) => {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        rafId = 0;
        const id = resolveCurrent();
        if (id) setActive(id, ensureVisible);
      });
    };

    links.forEach((link) => {
      link.addEventListener('click', () => {
        const href = link.getAttribute('href') || '';
        if (href.startsWith('#')) setActive(href.slice(1), true);
      });
    });

    window.addEventListener('scroll', () => refresh(true), { passive: true });
    window.addEventListener('resize', () => refresh(false), { passive: true });

    const search = document.querySelector('[data-tp-search]');
    if (search) search.addEventListener('input', () => setTimeout(() => refresh(true), 0));

    if ('MutationObserver' in window) {
      const observer = new MutationObserver(() => refresh(true));
      sections.forEach((section) => observer.observe(section, { attributes: true, attributeFilter: ['hidden'] }));
    }

    const hashId = location.hash.startsWith('#tp-') ? location.hash.slice(1) : '';
    if (hashId && linkById.has(hashId)) setActive(hashId, true);
    else refresh(true);
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
