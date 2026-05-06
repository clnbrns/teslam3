// mobile.js — bottom nav, hamburger drawer, pull-to-refresh, skeletons.
// Loaded on every page. Activates the mobile-specific layout when the
// `layout=mobile` cookie is present (set by the server when the user is
// on a /m/* URL or was redirected there from a mobile UA).

(function () {
  const PLACEHOLDER = /^[\s—–\-—]*$/;
  const SKELETON_DEBOUNCE_MS = 400;
  const PTR_THRESHOLD = 70;

  function readCookie(name) {
    return document.cookie.split('; ').reduce((acc, kv) => {
      const [k, v] = kv.split('=');
      return k === name ? decodeURIComponent(v) : acc;
    }, '');
  }

  const isMobileLayout = readCookie('layout') === 'mobile';
  if (isMobileLayout) document.body.classList.add('mobile');

  // ---------- Hamburger bar + drawer (mobile layout only) ----------
  const NAV_ITEMS = [
    { href: '/',          label: 'Dashboard', icon: '<path d="M3 12l9-9 9 9M5 10v10h4v-6h6v6h4V10"/>' },
    { href: '/report',    label: 'Driver Report', icon: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 5-6 8-6s7 2 8 6"/>' },
    { href: '/fsd',       label: 'FSD Usage', icon: '<path d="M3 12h4l2-5 4 10 2-5h6"/>' },
    { href: '/attention', label: 'Attention', icon: '<circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12z"/>' },
    { href: '/charging',  label: 'Charging',  icon: '<path d="M14 3l-4 8h4l-2 10 8-12h-4l3-6h-5z"/>' },
    { href: '/trips',     label: 'Trips',     icon: '<path d="M3 17l4-9 4 5 4-3 6 7"/><circle cx="6" cy="18" r="1.5"/><circle cx="18" cy="18" r="1.5"/>' },
    { href: '/map',       label: 'Map',       icon: '<path d="M9 4l-6 2v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/>' },
    { href: '/roi',       label: 'ROI / TCO', icon: '<path d="M4 20V8M10 20v-8M16 20v-4M22 20V4"/>' },
    { href: '/events',    label: 'Events',    icon: '<path d="M4 6h16M4 12h16M4 18h10"/>' },
  ];
  const DRIVERS = ['Colin', 'Lindsey', 'Carson'];

  function pageTitle() {
    const t = document.title.replace('Goblin M3P · ', '').trim();
    return t || 'Goblin M3P';
  }

  // Translate any "/foo" into "/m/foo" when in mobile layout, so the cookie
  // path stays sticky and refresh keeps you in mobile mode.
  function maybeMobilePath(href) {
    if (!isMobileLayout) return href;
    if (!href || !href.startsWith('/')) return href;
    if (href.startsWith('/m/') || href === '/m') return href;
    if (href.startsWith('/static/') || href.startsWith('/api/')) return href;
    if (href.startsWith('/healthz') || href.startsWith('/.well-known')) return href;
    return '/m' + (href === '/' ? '/' : href);
  }

  function injectMobileBar() {
    if (!isMobileLayout) return;
    if (document.querySelector('.mobile-bar')) return;
    const bar = document.createElement('header');
    bar.className = 'mobile-bar';
    bar.innerHTML = `
      <button class="hamburger" aria-label="Open navigation">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 7h16M4 12h16M4 17h16"/>
        </svg>
      </button>
      <span class="page-title">${pageTitle()}</span>
      <div class="spacer"></div>
      <button class="driver-chip" id="driver-chip" data-driver="" aria-label="Active driver">
        <span class="driver-chip-name">—</span>
      </button>
    `;
    document.body.insertBefore(bar, document.body.firstChild);
    bar.querySelector('.hamburger').addEventListener('click', openDrawer);
  }

  // ---------- Driver chip + popover (works on desktop too) ----------
  async function loadActiveDriver() {
    try {
      const r = await fetch('/api/active-driver');
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  }
  async function setActiveDriver(driver) {
    await fetch('/api/active-driver', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ driver }),
    });
  }
  function injectDriverChip() {
    // For desktop layout, inject chip into the .nav next to the spacer.
    if (!isMobileLayout) {
      const nav = document.querySelector('.nav .spacer');
      if (nav && !document.getElementById('driver-chip')) {
        const chip = document.createElement('button');
        chip.id = 'driver-chip';
        chip.className = 'driver-chip';
        chip.dataset.driver = '';
        chip.setAttribute('aria-label', 'Active driver');
        chip.innerHTML = '<span class="driver-chip-name">—</span>';
        nav.parentElement.insertBefore(chip, nav.nextSibling);
      }
    }
    const chip = document.getElementById('driver-chip');
    if (!chip) return;

    // Build popover once
    let pop = document.getElementById('driver-popover');
    if (!pop) {
      pop = document.createElement('div');
      pop.id = 'driver-popover';
      pop.className = 'driver-popover';
      pop.innerHTML = '<div class="head">Set Active Driver</div><div id="driver-popover-list"></div>';
      document.body.appendChild(pop);
    }
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      pop.classList.toggle('open');
    });
    document.addEventListener('click', (e) => {
      if (!pop.contains(e.target) && e.target !== chip) pop.classList.remove('open');
    });

    function render(state) {
      const driver = state?.driver || '—';
      chip.querySelector('.driver-chip-name').textContent = driver;
      chip.dataset.driver = driver;
      const colors = { Colin: '#4a90d9', Lindsey: '#c9a227', Carson: '#c8232c' };
      const list = document.getElementById('driver-popover-list');
      list.innerHTML = (state?.options || ['Colin','Lindsey','Carson']).map(d => `
        <button class="${d === driver ? 'active' : ''}" data-d="${d}">
          <span class="dot" style="background:${colors[d]||'#888'};box-shadow:0 0 6px ${colors[d]||'#888'}"></span>
          ${d}
        </button>`).join('');
      list.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
        await setActiveDriver(b.dataset.d);
        pop.classList.remove('open');
        const fresh = await loadActiveDriver();
        render(fresh);
      }));
    }
    loadActiveDriver().then(s => s && render(s));
  }

  function injectDrawer() {
    if (!isMobileLayout) return;
    if (document.querySelector('.drawer')) return;

    const path = location.pathname.replace(/^\/m/, '') || '/';
    const itemsHtml = NAV_ITEMS.map(i => `
      <a href="${maybeMobilePath(i.href)}" class="${path === i.href || (i.href !== '/' && path.startsWith(i.href)) ? 'active' : ''}">
        <svg class="ico" viewBox="0 0 24 24">${i.icon}</svg>
        <span>${i.label}</span>
      </a>`).join('');

    const driversHtml = DRIVERS.map(d => `
      <a href="${maybeMobilePath('/report?driver=' + d)}">
        <svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 21c1-3 4-5 7-5s6 2 7 5"/></svg>
        <span>${d}</span>
      </a>`).join('');

    const drawer = document.createElement('aside');
    drawer.className = 'drawer';
    drawer.innerHTML = `
      <div class="drawer-head">
        <span class="brand">Goblin M3P</span>
        <button class="drawer-close" aria-label="Close menu">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M6 6l12 12M18 6l-6 6-6 6"/>
          </svg>
        </button>
      </div>
      <div class="drawer-section">
        ${itemsHtml}
      </div>
      <div class="drawer-section">
        <div class="label">Drivers</div>
        ${driversHtml}
      </div>
      <div class="drawer-footer">
        <span>Tesla Fleet · 2024 Model 3 Performance</span>
        <a href="/?desktop=1">Switch to desktop layout</a>
      </div>
    `;
    document.body.appendChild(drawer);

    const overlay = document.createElement('div');
    overlay.className = 'drawer-overlay';
    document.body.appendChild(overlay);

    drawer.querySelector('.drawer-close').addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeDrawer();
    });
  }

  function openDrawer() {
    document.querySelector('.drawer')?.classList.add('open');
    document.querySelector('.drawer-overlay')?.classList.add('show');
    document.body.style.overflow = 'hidden';
  }
  function closeDrawer() {
    document.querySelector('.drawer')?.classList.remove('open');
    document.querySelector('.drawer-overlay')?.classList.remove('show');
    document.body.style.overflow = '';
  }

  // Rewrite outbound link clicks to stay on /m/* in mobile layout.
  function interceptLinks() {
    if (!isMobileLayout) return;
    document.addEventListener('click', (e) => {
      const a = e.target.closest('a[href]');
      if (!a) return;
      const href = a.getAttribute('href');
      if (!href || href.startsWith('http') || href.startsWith('mailto:')) return;
      const mhref = maybeMobilePath(href);
      if (mhref !== href) {
        e.preventDefault();
        location.href = mhref;
      }
    });
  }

  // ---------- Bottom nav ----------
  function injectBottomNav() {
    if (document.querySelector('.bottom-nav')) return;
    const path = (location.pathname.replace(/^\/m/, '') || '/').replace(/\/$/, '') || '/';
    const items = [
      { href: maybeMobilePath('/'),       label: 'Home',   active: path === '/',
        icon: '<path d="M3 12l9-9 9 9M5 10v10h4v-6h6v6h4V10"/>' },
      { href: maybeMobilePath('/trips'),  label: 'Trips',  active: path.startsWith('/trips'),
        icon: '<path d="M3 17l4-9 4 5 4-3 6 7"/><circle cx="6" cy="18" r="1.5"/><circle cx="18" cy="18" r="1.5"/>' },
      { href: maybeMobilePath('/roi'),    label: 'ROI',    active: path.startsWith('/roi'),
        icon: '<path d="M4 20V8M10 20v-8M16 20v-4M22 20V4"/>' },
      { href: maybeMobilePath('/report'), label: 'Driver', active: path.startsWith('/report'),
        icon: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 5-6 8-6s7 2 8 6"/>' },
    ];
    const html = items.map(i => `
      <a href="${i.href}" class="${i.active ? 'active' : ''}" aria-label="${i.label}">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${i.icon}</svg>
        <span>${i.label}</span>
      </a>`).join('');
    const nav = document.createElement('nav');
    nav.className = 'bottom-nav';
    nav.innerHTML = `<div class="bottom-nav-grid">${html}</div>`;
    document.body.appendChild(nav);
  }

  // ---------- Pull-to-refresh ----------
  function initPullToRefresh() {
    if (!('ontouchstart' in window)) return;
    let startY = 0, pulling = false, indicator = null;

    function ensureIndicator() {
      if (indicator) return indicator;
      indicator = document.createElement('div');
      indicator.className = 'ptr-indicator';
      indicator.innerHTML = '<span class="ptr-arrow"></span><span class="ptr-label">Pull to refresh</span>';
      document.body.appendChild(indicator);
      return indicator;
    }

    document.addEventListener('touchstart', (e) => {
      if (window.scrollY > 0) return;
      startY = e.touches[0].clientY;
      pulling = true;
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
      if (!pulling) return;
      const dy = e.touches[0].clientY - startY;
      if (dy <= 0) { hide(); return; }
      const ind = ensureIndicator();
      ind.classList.add('show');
      ind.querySelector('.ptr-label').textContent =
        dy >= PTR_THRESHOLD ? 'Release to refresh' : 'Pull to refresh';
    }, { passive: true });

    document.addEventListener('touchend', (e) => {
      if (!pulling) return;
      pulling = false;
      const dy = (e.changedTouches[0]?.clientY || 0) - startY;
      if (dy >= PTR_THRESHOLD) trigger();
      else hide();
    });

    function hide() {
      if (indicator) indicator.classList.remove('show', 'spinning');
    }
    function trigger() {
      const ind = ensureIndicator();
      ind.classList.add('show', 'spinning');
      ind.querySelector('.ptr-label').textContent = 'Refreshing…';
      const fn = window.refresh || (() => location.reload());
      Promise.resolve()
        .then(() => fn())
        .catch(() => {})
        .finally(() => setTimeout(hide, 400));
    }
  }

  // ---------- Skeleton loaders ----------
  function initSkeletons() {
    const candidates = Array.from(document.querySelectorAll('[id]'))
      .filter((el) => {
        if (el.children.length > 1) return false;
        if (el.closest('.nav, .bottom-nav, .mobile-bar, .drawer, .page-head h1')) return false;
        return PLACEHOLDER.test((el.textContent || '').trim());
      });
    setTimeout(() => {
      candidates.forEach((el) => {
        if (PLACEHOLDER.test((el.textContent || '').trim())) el.classList.add('sk');
      });
    }, SKELETON_DEBOUNCE_MS);

    new MutationObserver((mutations) => {
      const seen = new Set();
      for (const m of mutations) {
        const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
        if (!target || seen.has(target)) continue;
        seen.add(target);
        if (target.classList && target.classList.contains('sk')) {
          if (!PLACEHOLDER.test((target.textContent || '').trim())) {
            target.classList.remove('sk');
          }
        }
      }
    }).observe(document.body, { childList: true, characterData: true, subtree: true });
  }

  function init() {
    injectMobileBar();
    injectDrawer();
    interceptLinks();
    injectBottomNav();
    injectDriverChip();
    initPullToRefresh();
    initSkeletons();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
