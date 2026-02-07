// Initialize the beforeinstallprompt listener at the very beginning - do not move this!
let deferredPrompt = null;

window.addEventListener('beforeinstallprompt', (e) => {
    deferredPrompt = e;
    const installContainer = document.getElementById('pwa_install_container');
    if (installContainer) {
        installContainer.style.display = 'block';
    }
    console.log('beforeinstallprompt event captured');
});

document.addEventListener("DOMContentLoaded", function() {
    // Shared navigation/visibility state used by reconnect/reload logic.
    // Goal: reload on real disconnects, but never during user-initiated navigation away.
    let isNavigatingAway = false;
    let isPageHidden = (document.visibilityState !== 'visible');

    // --- Theme (auto/dark/light) ---
    // Uses Bootstrap 5.3 color modes if available (data-bs-theme), with a safe fallback.
    const THEME_PREF_KEY = 'kittyhack_theme_pref_v1'; // 'auto' | 'dark' | 'light'
    const themeToggleButton = document.getElementById('theme_toggle_button');
    const themeColorMeta = document.querySelector('meta[name="theme-color"]');
    const prefersDarkQuery = (window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null);

    function safeGetThemePref() {
        try {
            const raw = localStorage.getItem(THEME_PREF_KEY);
            if (raw === 'dark' || raw === 'light' || raw === 'auto') return raw;
        } catch (e) {
            // ignore storage errors
        }
        return 'auto';
    }

    function getSystemTheme() {
        return (prefersDarkQuery && prefersDarkQuery.matches) ? 'dark' : 'light';
    }

    function setThemeColorMeta(effectiveTheme) {
        if (!themeColorMeta) return;
        // Pick colors that look reasonable in browser UI chrome.
        const color = (effectiveTheme === 'dark') ? '#111827' : '#FFFFFF';
        themeColorMeta.setAttribute('content', color);
    }

    function updateThemeToggleLabel(pref, effective) {
        if (!themeToggleButton) return;
        const prefLabel = (pref === 'auto') ? 'Auto' : (pref === 'dark' ? 'Dark' : 'Light');
        themeToggleButton.textContent = `Theme: ${prefLabel}`;
        themeToggleButton.setAttribute('data-theme-pref', pref);
        themeToggleButton.setAttribute('data-theme-effective', effective);
    }

    function applyTheme(pref) {
        const effective = (pref === 'auto') ? getSystemTheme() : pref;
        document.documentElement.setAttribute('data-bs-theme', effective);
        document.documentElement.setAttribute('data-theme-pref', pref);
        setThemeColorMeta(effective);
        updateThemeToggleLabel(pref, effective);
    }

    function cycleThemePref(currentPref) {
        if (currentPref === 'auto') return 'dark';
        if (currentPref === 'dark') return 'light';
        return 'auto';
    }

    let themePref = safeGetThemePref();
    applyTheme(themePref);

    if (themeToggleButton) {
        themeToggleButton.addEventListener('click', function() {
            themePref = cycleThemePref(themePref);
            try { localStorage.setItem(THEME_PREF_KEY, themePref); } catch (e) {}
            applyTheme(themePref);
        });
    }

    // If user is in Auto mode and OS theme changes, follow it.
    if (prefersDarkQuery) {
        const onPrefersChange = function() {
            if (themePref === 'auto') applyTheme('auto');
        };
        if (typeof prefersDarkQuery.addEventListener === 'function') {
            prefersDarkQuery.addEventListener('change', onPrefersChange);
        } else if (typeof prefersDarkQuery.addListener === 'function') {
            // Safari / older browsers
            prefersDarkQuery.addListener(onPrefersChange);
        }
    }

    // --- Tooltips: don't stick after click ---
    // Bootstrap tooltips default to 'hover focus'. On click, the element gets focus and the
    // tooltip can remain visible until focus changes. Instead of re-initializing tooltips
    // (which can lose their stored title), just hide/blur on click/focus.
    function canUseBootstrapTooltips() {
        return (window.bootstrap && window.bootstrap.Tooltip);
    }

    function ensureBootstrapTooltips(root) {
        if (!canUseBootstrapTooltips()) return;
        const scope = root && root.querySelectorAll ? root : document;
        try {
            // Initialize triggers inside scope
            const triggers = scope.querySelectorAll('[data-bs-toggle="tooltip"]');
            triggers.forEach(function(el) {
                try {
                    // Avoid double-initialization; Bootstrap will reuse if present.
                    window.bootstrap.Tooltip.getOrCreateInstance(el);
                } catch (e) {
                    // ignore per-element failures
                }
            });
            // Also handle the root itself if it is a trigger
            if (root && root.getAttribute && root.getAttribute('data-bs-toggle') === 'tooltip') {
                try { window.bootstrap.Tooltip.getOrCreateInstance(root); } catch (e) {}
            }
        } catch (e) {
            // ignore
        }
    }

    function hideTooltip(el) {
        if (!el || !canUseBootstrapTooltips()) return;
        try {
            const inst = window.bootstrap.Tooltip.getInstance(el);
            if (inst) inst.hide();
        } catch (e) {
            // ignore
        }
    }

    function getTooltipTriggerSpec(el) {
        if (!el) return '';
        try {
            const inst = canUseBootstrapTooltips() ? window.bootstrap.Tooltip.getInstance(el) : null;
            const cfg = inst && inst._config ? inst._config : null;
            if (cfg && typeof cfg.trigger === 'string') return cfg.trigger;
        } catch (e) {
            // ignore
        }
        try {
            const attr = el.getAttribute ? el.getAttribute('data-bs-trigger') : '';
            return (typeof attr === 'string') ? attr : '';
        } catch (e) {
            return '';
        }
    }

    function tooltipAllowsClick(el) {
        const spec = (getTooltipTriggerSpec(el) || '').trim();
        if (!spec) return false; // Bootstrap default is 'hover focus'
        return spec.split(/\s+/).includes('click');
    }

    function hideTooltipSoon(el) {
        // Defer to run after Bootstrap's internal focus/click handlers.
        setTimeout(() => hideTooltip(el), 0);
    }

    function isCoarsePointerDevice() {
        try {
            if (window.matchMedia) {
                // Most phones/tablets
                if (window.matchMedia('(pointer: coarse)').matches) return true;
                // Some browsers report no hover capability instead
                if (window.matchMedia('(hover: none)').matches) return true;
            }
        } catch (e) {
            // ignore
        }
        return false;
    }

    // Track pointer interactions so we can distinguish keyboard focus from click/tap focus.
    let lastPointerDownAt = 0;
    document.addEventListener('pointerdown', function() {
        lastPointerDownAt = Date.now();
    }, true);

    // Hide tooltip immediately when clicking its trigger
    document.addEventListener('click', function(ev) {
        const trigger = ev.target && ev.target.closest ? ev.target.closest('[data-bs-toggle="tooltip"]') : null;
        if (!trigger) return;
        // On mobile/touch devices, Bootstrap tooltips are often click/tap driven.
        // Our desktop-focused auto-hide would immediately dismiss them.
        if (isCoarsePointerDevice()) return;
        // If the tooltip is configured to open on click (common on touch devices), don't auto-hide it.
        if (tooltipAllowsClick(trigger)) return;
        hideTooltipSoon(trigger);
        try {
            if (typeof trigger.blur === 'function') trigger.blur();
        } catch (e) {}
    }, true);

    // Prevent focus from keeping tooltips visible
    document.addEventListener('focusin', function(ev) {
        const trigger = ev.target && ev.target.closest ? ev.target.closest('[data-bs-toggle="tooltip"]') : null;
        if (!trigger) return;
        if (isCoarsePointerDevice()) return;
        // Only auto-hide focus-based tooltips if focus likely came from a pointer interaction.
        if ((Date.now() - lastPointerDownAt) > 700) return;
        if (tooltipAllowsClick(trigger)) return;
        hideTooltipSoon(trigger);
        try {
            if (typeof trigger.blur === 'function') trigger.blur();
        } catch (e) {}
    }, true);

    // Tooltips are not auto-enabled by Bootstrap from data attributes.
    // Shiny often re-renders UI outputs; ensure new tooltip triggers are initialized.
    ensureBootstrapTooltips(document);
    let tooltipInitScheduled = false;
    const tooltipInitObserver = new MutationObserver(function(mutations) {
        if (tooltipInitScheduled) return;
        // Schedule a single re-scan in the next tick.
        tooltipInitScheduled = true;
        setTimeout(function() {
            tooltipInitScheduled = false;
            // Only scan if there is at least one added node.
            try {
                for (let i = 0; i < mutations.length; i++) {
                    const m = mutations[i];
                    if (m && m.addedNodes && m.addedNodes.length) {
                        ensureBootstrapTooltips(document);
                        break;
                    }
                }
            } catch (e) {
                ensureBootstrapTooltips(document);
            }
        }, 0);
    });
    tooltipInitObserver.observe(document.body, { childList: true, subtree: true });

    // --- Event modal: scrubber ---
    // Scrubber is always visible; server updates its value continuously.

    // --- Event modal: fast prev/next with local frame cache ---
    // Problem: the modal image is a Shiny output that can feel laggy when clicking prev/next.
    // Solution: keep a tiny in-memory cache of frames (by index) and swap the <img> src
    // immediately on click while still letting Shiny process the click for authoritative state.
    (function() {
        const CACHE_MAX_PER_EVENT = 64;
        const THROTTLE_MS = 80;

        // Cache keyed by block_id (string) => { srcByIdx: Map<number,string>, tsByIdx: Map<number,string>, lastUsed: number }
        const eventCache = new Map();
        let lastNavAt = 0;

        function nowMs() { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }

        function findModalFromEl(fromEl) {
            if (!fromEl || !fromEl.closest) return document.querySelector('.modal');
            return fromEl.closest('.modal') || document.querySelector('.modal');
        }

        function findPictureRoot(modal) {
            if (!modal || !modal.querySelector) return null;
            return modal.querySelector('#event_modal_picture');
        }

        function findPlayingState(modal) {
            // Prefer server authoritative state set on scrubber wrap
            const wrap = modal && modal.querySelector ? modal.querySelector('#event_scrubber_wrap') : null;
            if (wrap && wrap.getAttribute) {
                return wrap.getAttribute('data-playing') === '1';
            }
            // Fallback: treat disabled prev button as playing
            const prevBtn = modal && modal.querySelector ? modal.querySelector('button[id$="btn_prev"]') : null;
            return !!(prevBtn && prevBtn.disabled);
        }

        function getOrCreateCache(blockId) {
            const key = String(blockId || '');
            let entry = eventCache.get(key);
            if (!entry) {
                entry = { srcByIdx: new Map(), tsByIdx: new Map(), lastUsed: Date.now() };
                eventCache.set(key, entry);
            }
            entry.lastUsed = Date.now();
            return entry;
        }

        function pruneCache(entry) {
            if (!entry || !entry.srcByIdx) return;
            // If larger than cap, delete oldest inserted keys (Map preserves insertion order)
            while (entry.srcByIdx.size > CACHE_MAX_PER_EVENT) {
                const firstKey = entry.srcByIdx.keys().next().value;
                entry.srcByIdx.delete(firstKey);
                entry.tsByIdx.delete(firstKey);
            }
        }

        function parseIntSafe(v, fallback) {
            const n = parseInt(v, 10);
            return Number.isFinite(n) ? n : fallback;
        }

        function captureCurrentFrame(modal) {
            const root = findPictureRoot(modal);
            if (!root) return;

            const blockId = (root.closest && root.closest('[data-block-id]')) ? root.closest('[data-block-id]').getAttribute('data-block-id') : null;
            const cache = getOrCreateCache(blockId);

            const visIdx = parseIntSafe(root.getAttribute('data-vis-idx'), 0);
            const total = parseIntSafe(root.getAttribute('data-total'), 0);
            const nextIdx = total > 0 ? ((visIdx + 1) % total) : null;

            const imgs = root.querySelectorAll('img');
            if (imgs && imgs.length > 0) {
                const preloadImg = root.querySelector('img[aria-hidden="true"]');
                // Visible is the non-aria-hidden image (or last one as fallback)
                const visibleImg = root.querySelector('img:not([aria-hidden="true"])') || imgs[imgs.length - 1];

                if (visibleImg && visibleImg.getAttribute) {
                    const src = visibleImg.getAttribute('src');
                    if (src && typeof src === 'string' && src.startsWith('data:image')) {
                        cache.srcByIdx.set(visIdx, src);
                    }
                }
                if (preloadImg && preloadImg.getAttribute && nextIdx !== null) {
                    const src2 = preloadImg.getAttribute('src');
                    if (src2 && typeof src2 === 'string' && src2.startsWith('data:image')) {
                        cache.srcByIdx.set(nextIdx, src2);
                    }
                }
            }

            const tsEl = modal.querySelector('#event_modal_timestamp');
            if (tsEl && typeof tsEl.textContent === 'string') {
                const ts = tsEl.textContent.trim();
                if (ts) cache.tsByIdx.set(visIdx, ts);
            }

            pruneCache(cache);
        }

        function optimisticRender(modal, targetIdx) {
            const root = findPictureRoot(modal);
            if (!root) return false;

            const total = parseIntSafe(root.getAttribute('data-total'), 0);
            if (!Number.isFinite(targetIdx) || total <= 0) return false;

            const blockId = (root.closest && root.closest('[data-block-id]')) ? root.closest('[data-block-id]').getAttribute('data-block-id') : null;
            const cache = getOrCreateCache(blockId);
            const src = cache.srcByIdx.get(targetIdx);
            if (!src) return false;

            // Swap visible image src immediately
            const visibleImg = root.querySelector('img:not([aria-hidden="true"])');
            if (visibleImg && visibleImg.setAttribute) {
                visibleImg.setAttribute('src', src);
            }

            // Update data-vis-idx and overlays so the user sees instant feedback.
            root.setAttribute('data-vis-idx', String(targetIdx));

            const counterEl = modal.querySelector('#event_modal_counter');
            if (counterEl) counterEl.textContent = String(targetIdx + 1) + '/' + String(total);

            const tsEl = modal.querySelector('#event_modal_timestamp');
            const ts = cache.tsByIdx.get(targetIdx);
            if (tsEl && ts) tsEl.textContent = ts;

            return true;
        }

        // Capture frames whenever the output re-renders
        const pictureObserver = new MutationObserver(function() {
            const modal = document.querySelector('.modal');
            if (modal) captureCurrentFrame(modal);
        });
        pictureObserver.observe(document.body, { childList: true, subtree: true });

        // Optimistic prev/next handlers (capture phase to run ASAP)
        document.addEventListener('click', function(ev) {
            const btnPrev = ev.target && ev.target.closest ? ev.target.closest('button[id$="btn_prev"]') : null;
            const btnNext = ev.target && ev.target.closest ? ev.target.closest('button[id$="btn_next"]') : null;
            const btn = btnPrev || btnNext;
            if (!btn) return;
            if (btn.disabled) return;

            const modal = findModalFromEl(btn);
            if (!modal) return;
            if (findPlayingState(modal)) return;

            const t = nowMs();
            if ((t - lastNavAt) < THROTTLE_MS) return;
            lastNavAt = t;

            // Ensure cache is up-to-date before computing target
            captureCurrentFrame(modal);

            const root = findPictureRoot(modal);
            if (!root) return;
            const visIdx = parseIntSafe(root.getAttribute('data-vis-idx'), 0);
            const total = parseIntSafe(root.getAttribute('data-total'), 0);
            if (total <= 0) return;

            const targetIdx = btnPrev ? ((visIdx - 1 + total) % total) : ((visIdx + 1) % total);
            optimisticRender(modal, targetIdx);
        }, true);
    })();

    // --- Reload debug helpers ---
    // Stores recent reload attempts in sessionStorage so mobile debugging is possible even after a reload.
    const RELOAD_DEBUG_KEY = 'kittyhack_reload_debug_v1';
    function recordReloadAttempt(entry) {
        try {
            const existing = JSON.parse(sessionStorage.getItem(RELOAD_DEBUG_KEY) || '[]');
            existing.push(entry);
            // keep last 25 entries
            const trimmed = existing.slice(-25);
            sessionStorage.setItem(RELOAD_DEBUG_KEY, JSON.stringify(trimmed));
        } catch (e) {
            // ignore storage errors (private mode / quota)
        }
    }

    function dumpLastReloadAttempts() {
        try {
            const existing = JSON.parse(sessionStorage.getItem(RELOAD_DEBUG_KEY) || '[]');
            if (existing.length > 0) {
                const last = existing[existing.length - 1];
                console.warn('[ReloadDebug] Last reload attempt:', last);
                if (last && last.stack) {
                    console.debug('[ReloadDebug] Stack of last reload attempt:\n' + last.stack);
                }
            }
        } catch (e) {
            // ignore
        }
    }

    function attemptReload(reason, opts) {
        const options = opts || {};
        const entry = {
            ts: new Date().toISOString(),
            reason: reason,
            href: window.location.href,
            visibilityState: document.visibilityState,
            isNavigatingAway: isNavigatingAway,
            isPageHidden: isPageHidden,
            userAgent: navigator.userAgent,
            stack: (new Error('reload attempt: ' + reason)).stack
        };
        recordReloadAttempt(entry);
        console.warn('[ReloadDebug] reload attempt:', entry);

        if (!options.allowWhenNavigatingAway && isNavigatingAway) {
            console.warn('[ReloadDebug] suppressed (navigating away)');
            return;
        }
        if (!options.allowWhenHidden && document.visibilityState !== 'visible') {
            console.warn('[ReloadDebug] suppressed (page hidden)');
            return;
        }
        location.reload();
    }

    // Print previous reload attempt on load (helps on Android after the page already reloaded)
    dumpLastReloadAttempts();

    // Track visibility transitions to distinguish real backgrounding from brief UI flicker
    let lastVisibilityState = document.visibilityState;
    let lastHiddenAtMs = (lastVisibilityState === 'hidden') ? Date.now() : null;
    // observe for the presence of the "allowed_to_exit_ranges" element
    let observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (document.getElementById("allowed_to_exit_ranges")) {
                toggleAllowedToExitRanges();
                observer.disconnect(); // Stop observing once found
            }
        });
    });

    observer.observe(document.body, { childList: true, subtree: true });

    function toggleAllowedToExitRanges() {
        let btnAllowedToExit = document.getElementById("btnAllowedToExit");
        let allowedToExitRanges = document.getElementById("allowed_to_exit_ranges");

        if (btnAllowedToExit && allowedToExitRanges) {
            const show = (btnAllowedToExit.value === 'allow' || btnAllowedToExit.value === 'configure_per_cat');
            allowedToExitRanges.style.display = show ? "block" : "none";
            btnAllowedToExit.addEventListener("change", function() {
                const showNow = (btnAllowedToExit.value === 'allow' || btnAllowedToExit.value === 'configure_per_cat');
                allowedToExitRanges.style.display = showNow ? "block" : "none";
            });
        }
    }

    // --- Add functionality to reload on shiny-disconnected-overlay ---
    (function() {
        let reloadInterval = null;
        let reloadedOnce = false;
        let pendingReloadTimeout = null;

        function clearReloadTimers() {
            if (pendingReloadTimeout) {
                clearTimeout(pendingReloadTimeout);
                pendingReloadTimeout = null;
            }
            if (reloadInterval) {
                clearInterval(reloadInterval);
                reloadInterval = null;
            }
        }

        function scheduleReload(reason) {
            // Defer reload slightly so navigation-away events can win the race (Android Firefox).
            if (pendingReloadTimeout || reloadedOnce) return;
            if (isNavigatingAway || isPageHidden) return;
            pendingReloadTimeout = setTimeout(() => {
                pendingReloadTimeout = null;
                if (isNavigatingAway || isPageHidden) return;
                const stillOverlay = document.getElementById("shiny-disconnected-overlay");
                if (!stillOverlay) return;
                reloadedOnce = true;
                attemptReload(reason);
            }, 250);
        }

        // Listen for navigation attempts
        window.addEventListener('beforeunload', function() {
            isNavigatingAway = true;
            // Stop any pending forced reload loops when user navigates away
            clearReloadTimers();
        });

        // Mobile browsers are more reliable with pagehide/unload than beforeunload.
        window.addEventListener('pagehide', function() {
            isNavigatingAway = true;
            clearReloadTimers();
        });
        window.addEventListener('unload', function() {
            isNavigatingAway = true;
            clearReloadTimers();
        });

        // Reset flags when page is shown from bfcache or app switch
        window.addEventListener('pageshow', function() {
            isNavigatingAway = false;
            isPageHidden = (document.visibilityState !== 'visible');
            // Attempt service worker update on HTTPS
            if (navigator.serviceWorker && navigator.serviceWorker.ready) {
                navigator.serviceWorker.ready.then(reg => {
                    try { reg.update(); } catch (e) {}
                });
            }
        });

        document.addEventListener('visibilitychange', function() {
            isPageHidden = (document.visibilityState !== 'visible');
            // When coming back to foreground, allow reconnect/reload attempts again.
            if (!isPageHidden) {
                isNavigatingAway = false;
            }
        });

        function checkForDisconnectOverlay() {
            const overlay = document.getElementById("shiny-disconnected-overlay");

            if (overlay) {
                scheduleReload("Detected disconnection overlay. Reloading...");

                if (!reloadInterval) {
                    reloadInterval = setInterval(() => {
                        const stillOverlay = document.getElementById("shiny-disconnected-overlay");
                        if (isNavigatingAway || isPageHidden) {
                            // User is leaving; stop reload attempts
                            clearReloadTimers();
                            return;
                        }
                        if (stillOverlay) {
                            console.log("Still disconnected. Reloading again...");
                            attemptReload('Still disconnected. Reloading again...');
                        } else {
                            clearReloadTimers();
                            reloadedOnce = false;
                        }
                    }, 3000);
                }
            }
        }

        // Observe DOM changes for disconnection overlay
        const shinyObserver = new MutationObserver(checkForDisconnectOverlay);
        shinyObserver.observe(document.body, { childList: true, subtree: true });

        // Also check immediately in case it's already present
        checkForDisconnectOverlay();

        // --- Service Worker recovery hooks (HTTPS only) ---
        if ('serviceWorker' in navigator) {
            // When controller changes (new SW takes control), force a one-time reload
            let swReloaded = false;
            navigator.serviceWorker.addEventListener('controllerchange', () => {
                if (!swReloaded) {
                    swReloaded = true;
                    // Avoid interrupting user-initiated navigation (Firefox)
                    if (isNavigatingAway) {
                        console.log('Service worker controller changed during navigation; skip reload');
                        return;
                    }
                    console.log('Service worker controller changed, reloading...');
                    attemptReload('Service worker controller changed, reloading...');
                }
            });

            // Emergency kill switch: if the app remains blank for >3s after load, unregister SW once.
            // Helps recover from corrupted caches on mobile.
            window.addEventListener('load', () => {
                setTimeout(async () => {
                    const hasBodyContent = (document.body && document.body.children && document.body.children.length > 0);
                    // Heuristic: blank page or only head-level wrappers
                    if (!hasBodyContent) {
                        try {
                            const regs = await navigator.serviceWorker.getRegistrations();
                            for (const r of regs) {
                                // Only unregister our scope
                                if (r.scope && r.scope.endsWith('/')) {
                                    console.warn('Unregistering service worker due to blank page heuristic');
                                    await r.unregister();
                                }
                            }
                            // Clear caches to avoid stale shell
                            if (window.caches && caches.keys) {
                                const keys = await caches.keys();
                                for (const k of keys) {
                                    await caches.delete(k);
                                }
                            }
                            attemptReload('SW emergency recovery reload', { allowWhenHidden: true, allowWhenNavigatingAway: true });
                        } catch (e) {
                            console.error('SW emergency recovery failed:', e);
                        }
                    }
                }, 3000);
            });
        }
    })();

    // --- Collapse navbar on nav-link click (mobile fix) ---
    document.querySelectorAll('.navbar-collapse .nav-link').forEach(function (el) {
        el.addEventListener('click', function () {
            var navbarCollapse = el.closest('.navbar-collapse');
            if (navbarCollapse && navbarCollapse.classList.contains('show')) {
                navbarCollapse.classList.remove('show');
            }
        });
    });

    // --- Register Service Worker for PWA ---
    // Only register on HTTPS or localhost. Avoid registering on plain HTTP to prevent caching/stale pages.
    const isSecureOrigin =
        window.location.protocol === 'https:' ||
        window.location.hostname === 'localhost' ||
        window.location.hostname === '127.0.0.1';

    if (isSecureOrigin && 'serviceWorker' in navigator) {
        navigator.serviceWorker
            .register('/pwa-service-worker.js', { scope: '/' })
            .then(function(registration) { 
                console.log('Service Worker Registered with scope:', registration.scope);
            })
            .catch(function(error) {
                console.error('Service Worker registration failed:', error);
            });
    } else {
        console.warn('Skipping Service Worker registration: not a secure origin');
    }
    
    // --- PWA Installation functionality ---
    // Create observer to watch for PWA elements appearing in the DOM
    let pwaElementsObserver = new MutationObserver(function() {
        const installContainer = document.getElementById('pwa_install_container');
        if (installContainer) {
            // Once the elements are found, initialize the PWA installation functionality
            initPwaInstallation();
            // Stop observing once we've found the elements
            pwaElementsObserver.disconnect();
        }
    });
    
    // Start observing for PWA elements
    pwaElementsObserver.observe(document.body, { childList: true, subtree: true });
    
    // Separate function to initialize PWA installation
    function initPwaInstallation() {
        
        // Get elements
        const installContainer = document.getElementById('pwa_install_container');
        const installButton = document.getElementById('pwa_install_button');
        const httpsWarning = document.getElementById('pwa_https_warning');
        
        console.log("Install container found:", !!installContainer);
        console.log("Install button found:", !!installButton);
        
        if (installContainer) {
            // installContainer.style.display = 'none';
            
            // Check if we're running on HTTPS
            if (window.location.protocol !== 'https:' && 
                window.location.hostname !== 'localhost' && 
                window.location.hostname !== '127.0.0.1') {
                // Show HTTPS warning
                if (httpsWarning) {
                    httpsWarning.style.display = 'block';
                }
                // Hide install button
                if (installButton) {
                    installButton.style.display = 'none';
                }
                console.warn("PWA installation is only available over HTTPS or localhost");
            }
            
            // Check if app is already installed
            if (window.matchMedia('(display-mode: standalone)').matches || 
                window.navigator.standalone === true) {
                // Show already installed message
                console.log("App appears to be already installed");
                const alreadyInstalledMsg = document.getElementById('pwa_already_installed');
                if (alreadyInstalledMsg) {
                    alreadyInstalledMsg.style.display = 'block';
                }
                if (installButton) {
                    installButton.style.display = 'none';
                }
            }
            
            console.log("Waiting for beforeinstallprompt event...");
        }

        // Attach the click handler ONCE
        if (installButton) {
            installButton.addEventListener('click', async () => {
                if (!deferredPrompt) return;
                deferredPrompt.prompt();
                const { outcome } = await deferredPrompt.userChoice;
                console.log(`User response to install prompt: ${outcome}`);
                deferredPrompt = null;
                if (outcome === 'accepted') {
                    installButton.style.display = 'none';
                    const installedMsg = document.getElementById('pwa_installed_success');
                    if (installedMsg) {
                        installedMsg.style.display = 'block';
                    }
                }
            });
        }
        
        // Listen for the appinstalled event
        window.addEventListener('appinstalled', (evt) => {
            console.log('KITTYHACK was installed as PWA');
            if (installButton) {
                installButton.style.display = 'none';
            }
            const installedMsg = document.getElementById('pwa_installed_success');
            if (installedMsg) {
                installedMsg.style.display = 'block';
            }
        });
    }
    
    // Check immediately in case elements are already present
    if (document.getElementById('pwa_install_container')) {
        initPwaInstallation();
    }

    // Force reconnect when app returns to foreground or network returns
    function tryReconnect(opts) {
        const options = opts || {};
        const source = options.source || 'unknown';
        const allowOverlayReload = (options.allowOverlayReload === true);

        // If Shiny overlay exists, optionally reload.
        // NOTE: The overlay may already be present when returning from background, and
        // the MutationObserver won't fire again; in that case we need an explicit check.
        const overlay = document.getElementById("shiny-disconnected-overlay");
        if (overlay) {
            if (allowOverlayReload && !isNavigatingAway && document.visibilityState === 'visible') {
                attemptReload(`Reconnect(${source}): overlay present`);
            } else {
                console.log(`Reconnect(${source}): overlay present (no reload)`);
            }
            return;
        }
        // Lightweight ping to check reachability
        fetch('/', { cache: 'no-store', method: 'HEAD' })
            .then(() => {
                // If reachable, optionally trigger a Shiny input to reinitialize
                console.log("Reconnect: server reachable");
            })
            .catch(() => {
                console.log("Reconnect: server not reachable, showing offline page...");
                // Prefer offline page to avoid blank screen when SW is present
                if (!isNavigatingAway) {
                    try {
                        window.location.href = '/offline.html';
                    } catch (e) {
                        // Fallback to reload if redirect fails
                        attemptReload('Reconnect: offline redirect failed');
                    }
                }
            });
    }

    document.addEventListener('visibilitychange', function() {
        // Keep shared flags in sync (there are multiple listeners)
        isPageHidden = (document.visibilityState !== 'visible');

        const nowState = document.visibilityState;
        if (nowState === 'hidden') {
            lastVisibilityState = 'hidden';
            lastHiddenAtMs = Date.now();
            return;
        }

        if (nowState === 'visible') {
            const hiddenDurationMs = (lastHiddenAtMs ? (Date.now() - lastHiddenAtMs) : 0);
            lastHiddenAtMs = null;
            lastVisibilityState = 'visible';

            // Only treat this as a real "return to foreground" if we were hidden long enough.
            // This avoids address-bar / UI flicker on Android Firefox triggering reconnect logic.
            if (hiddenDurationMs >= 750) {
                // Only auto-reload when overlay is present if we were hidden long enough that
                // a websocket idle timeout is plausible. This avoids rare bounce-backs.
                const allowOverlayReload = (hiddenDurationMs >= 2500);
                tryReconnect({ source: `visibility/${hiddenDurationMs}ms`, allowOverlayReload });
            } else {
                console.log('Visibility returned quickly; skip reconnect (ms):', hiddenDurationMs);
            }
        }
    });

    window.addEventListener('online', () => tryReconnect({ source: 'online', allowOverlayReload: true }));
});