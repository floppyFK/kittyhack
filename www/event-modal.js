// Kittyhack - Event modal helpers
// Keeps the Python server code clean by hosting modal-specific JS here.
// This file is served via app.py static_assets mapping ("/" -> www/).

(function () {
    "use strict";

    // Mark Firefox mobile so CSS can avoid opacity/compositing glitches.
    try {
        var ua = (typeof navigator !== 'undefined' && navigator.userAgent) ? navigator.userAgent : '';
        var isFirefox = /Firefox\//i.test(ua);
        var isMobile = /Mobile|Android|iPhone|iPad|iPod/i.test(ua);
        if (isFirefox && isMobile) {
            document.documentElement.classList.add('kh-firefox-mobile');
        }
    } catch (e) {}

    // ---------------------------------------------------------------------
    // Optional performance optimization:
    // If the server provides a per-event bundle URL (tar.gz under /thumb/...),
    // fetch & unpack it once and then replace /thumb/<id>.jpg with blob: URLs.
    // This reduces the number of HTTP requests during playback/scrubbing,
    // while keeping the existing server-driven controls intact.
    // ---------------------------------------------------------------------

    // Global cache: blockId -> { urlsByPid: Map<number,string>, lastUsedMs: number }
    window.__khEventBundles = window.__khEventBundles || new Map();
    // Remember last shown frame per event block so DOM swaps don't flash black.
    window.__khEventModalLastShownByBlock = window.__khEventModalLastShownByBlock || new Map();
    // Track bundle load state by URL so we can avoid JPG fallback while it loads.
    window.__khEventBundleStateByUrl = window.__khEventBundleStateByUrl || new Map();

    function nowMs() {
        return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    }

    function parseIntSafe(v, fallback) {
        var n = parseInt(v, 10);
        return (typeof n === 'number' && isFinite(n)) ? n : fallback;
    }

    function parsePidFromThumbSrc(src) {
        if (!src || typeof src !== 'string') return null;
        // Accept absolute or relative URLs.
        // Examples: /thumb/123.jpg , https://host/thumb/123.jpg
        var m = src.match(/\/thumb\/(\d+)\.jpg(?:\?|#|$)/);
        if (!m) return null;
        var pid = parseIntSafe(m[1], null);
        return (pid === null || !isFinite(pid)) ? null : pid;
    }

    function canDecompressGzip() {
        try {
            return (typeof DecompressionStream !== 'undefined');
        } catch (e) {
            return false;
        }
    }

    function tarIsAllZero(headerBytes) {
        for (var i = 0; i < headerBytes.length; i++) {
            if (headerBytes[i] !== 0) return false;
        }
        return true;
    }

    function decodeNullTerminatedAscii(u8, start, end) {
        var out = '';
        for (var i = start; i < end; i++) {
            var c = u8[i];
            if (!c) break;
            out += String.fromCharCode(c);
        }
        return out;
    }

    function parseTar(arrayBuffer, onFile) {
        // Minimal tar parser for ustar-like archives.
        var u8 = new Uint8Array(arrayBuffer);
        var offset = 0;
        while (offset + 512 <= u8.length) {
            var header = u8.subarray(offset, offset + 512);
            if (tarIsAllZero(header)) break;

            var name = decodeNullTerminatedAscii(u8, offset + 0, offset + 100);
            var sizeOct = decodeNullTerminatedAscii(u8, offset + 124, offset + 136).trim();
            var size = 0;
            if (sizeOct) {
                try { size = parseInt(sizeOct, 8) || 0; } catch (e) { size = 0; }
            }
            var fileStart = offset + 512;
            var fileEnd = fileStart + size;
            if (fileEnd > u8.length) break;

            try {
                onFile(name, u8.subarray(fileStart, fileEnd));
            } catch (e) {
                // ignore per-file parse errors
            }

            // Advance to next 512-byte boundary
            var padded = Math.ceil(size / 512) * 512;
            offset = fileStart + padded;
        }
    }

    async function fetchAndCacheBundle(blockId, bundleUrl) {
        // IMPORTANT: Use bundleUrl as cache key (stable across re-renders).
        // Some Shiny ids can change across renders; the bundle URL is the true identity.
        var key = String(bundleUrl || '');
        if (!key) return;

        try {
            var st0 = window.__khEventBundleStateByUrl.get(key);
            if (!st0) {
                window.__khEventBundleStateByUrl.set(key, { status: 'loading', startedAtMs: Date.now(), lastTryAtMs: Date.now() });
            } else {
                st0.lastTryAtMs = Date.now();
                if (!st0.status) st0.status = 'loading';
                window.__khEventBundleStateByUrl.set(key, st0);
            }
        } catch (e) {}

        var existing = window.__khEventBundles.get(key);
        if (existing && existing.urlsByPid && existing.urlsByPid.size > 0) {
            existing.lastUsedMs = Date.now();
            try {
                var stOk = window.__khEventBundleStateByUrl.get(key) || {};
                stOk.status = 'ready';
                window.__khEventBundleStateByUrl.set(key, stOk);
            } catch (e) {}
            return;
        }

        // Prevent duplicate parallel fetches per block
        if (window.__khEventBundles.get(key) && window.__khEventBundles.get(key).__inFlight) return;
        window.__khEventBundles.set(key, { urlsByPid: new Map(), lastUsedMs: Date.now(), __inFlight: true });

        try {
            var resp = await fetch(bundleUrl, { cache: 'force-cache' });
            if (!resp || !resp.ok) return;

            var buf;
            // Support both plain tar and tar.gz. Prefer plain tar (works on HTTP without special APIs).
            var isGz = /\.gz(?:\?|#|$)/.test(bundleUrl);
            if (isGz) {
                if (!canDecompressGzip() || !resp.body) return;
                var ds = new DecompressionStream('gzip');
                var decompressedStream = resp.body.pipeThrough(ds);
                buf = await new Response(decompressedStream).arrayBuffer();
            } else {
                buf = await resp.arrayBuffer();
            }

            var urlsByPid = new Map();
            parseTar(buf, function (name, bytes) {
                if (!name || typeof name !== 'string') return;
                // Expect entries like "123.jpg"
                var m = name.match(/(^|\/)(\d+)\.jpg$/);
                if (!m) return;
                var pid = parseIntSafe(m[2], null);
                if (pid === null) return;

                try {
                    var blob = new Blob([bytes], { type: 'image/jpeg' });
                    var url = URL.createObjectURL(blob);
                    urlsByPid.set(pid, url);
                } catch (e) {
                    // ignore
                }
            });

            var entry = window.__khEventBundles.get(key) || { urlsByPid: new Map(), lastUsedMs: Date.now() };
            // Revoke any previous URLs (shouldn't exist normally)
            try {
                if (entry.urlsByPid && entry.urlsByPid.size) {
                    entry.urlsByPid.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) {} });
                }
            } catch (e) {}

            entry.urlsByPid = urlsByPid;
            entry.lastUsedMs = Date.now();
            entry.__inFlight = false;
            window.__khEventBundles.set(key, entry);

            try {
                var st1 = window.__khEventBundleStateByUrl.get(key) || {};
                st1.status = (urlsByPid && urlsByPid.size > 0) ? 'ready' : 'error';
                if (!st1.startedAtMs) st1.startedAtMs = Date.now();
                st1.lastTryAtMs = Date.now();
                window.__khEventBundleStateByUrl.set(key, st1);
            } catch (e) {}

            // Prune old bundles (keep last 3)
            try {
                if (window.__khEventBundles.size > 3) {
                    var items = Array.from(window.__khEventBundles.entries());
                    items.sort(function (a, b) {
                        return (a[1].lastUsedMs || 0) - (b[1].lastUsedMs || 0);
                    });
                    while (items.length > 3) {
                        var evict = items.shift();
                        if (!evict) break;
                        var evKey = evict[0];
                        var evVal = evict[1];
                        try {
                            if (evVal && evVal.urlsByPid) {
                                evVal.urlsByPid.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) {} });
                            }
                        } catch (e) {}
                        window.__khEventBundles.delete(evKey);
                    }
                }
            } catch (e) {}
        } catch (e) {
            // ignore
            try {
                var stE = window.__khEventBundleStateByUrl.get(key) || {};
                stE.status = 'error';
                if (!stE.startedAtMs) stE.startedAtMs = Date.now();
                stE.lastTryAtMs = Date.now();
                window.__khEventBundleStateByUrl.set(key, stE);
            } catch (e) {}
        } finally {
            try {
                var ent = window.__khEventBundles.get(key);
                if (ent) ent.__inFlight = false;
            } catch (e) {}
        }
    }

    function maybeSwapImgToBlob(modal, imgEl) {
        try {
            if (!modal || !imgEl || !imgEl.getAttribute) return;

            var root = modal.querySelector ? modal.querySelector('#event_modal_root') : null;
            var bundleUrl = root && root.getAttribute ? (root.getAttribute('data-bundle-url') || '') : '';
            var key = bundleUrl ? String(bundleUrl) : '';
            if (!key) {
                // Backwards compatibility fallback (older server versions)
                var blockId = root && root.getAttribute ? root.getAttribute('data-block-id') : null;
                if (!blockId) return;
                key = String(blockId);
            }

            var entry = window.__khEventBundles.get(String(key));
            if (!entry || !entry.urlsByPid) return;

            var pid = parseIntSafe(imgEl.getAttribute('data-pid'), null);
            if (pid === null) {
                pid = parsePidFromThumbSrc(imgEl.getAttribute('src') || imgEl.src || '');
            }
            if (pid === null) return;

            var blobUrl = entry.urlsByPid.get(pid);
            if (!blobUrl) return;

            var currentSrc = imgEl.getAttribute('src') || '';
            if (currentSrc === blobUrl) return;

            // Swap source to the cached blob.
            imgEl.setAttribute('src', blobUrl);
        } catch (e) {
            // ignore
        }
    }

    function lookupBlobUrl(modal, imgEl) {
        try {
            if (!modal || !imgEl || !imgEl.getAttribute) return '';

            var root = modal.querySelector ? modal.querySelector('#event_modal_root') : null;
            var bundleUrl = root && root.getAttribute ? (root.getAttribute('data-bundle-url') || '') : '';
            var key = bundleUrl ? String(bundleUrl) : '';
            if (!key) {
                var blockId = root && root.getAttribute ? root.getAttribute('data-block-id') : null;
                if (!blockId) return '';
                key = String(blockId);
            }

            var entry = window.__khEventBundles.get(String(key));
            if (!entry || !entry.urlsByPid) return '';

            var pid = parseIntSafe(imgEl.getAttribute('data-pid'), null);
            if (pid === null) return '';

            return entry.urlsByPid.get(pid) || '';
        } catch (e) {
            return '';
        }
    }

    function desiredImgSrc(modal, imgEl) {
        // Return the src we *want* to use without setting it.
        try {
            if (!imgEl || !imgEl.getAttribute) return '';

            // If a bundle is advertised but not ready yet, avoid falling back to /thumb/*.jpg.
            try {
                var root = modal && modal.querySelector ? modal.querySelector('#event_modal_root') : null;
                var bundleUrl = root && root.getAttribute ? (root.getAttribute('data-bundle-url') || '') : '';
                if (bundleUrl) {
                    var st = window.__khEventBundleStateByUrl.get(String(bundleUrl));
                    var startedAt = st && st.startedAtMs ? st.startedAtMs : 0;
                    var status = st && st.status ? st.status : '';
                    var age = startedAt ? (Date.now() - startedAt) : 0;
                    if (status !== 'ready' && age > 0 && age < 2500) {
                        return '';
                    }
                }
            } catch (e) {}

            var blob = lookupBlobUrl(modal, imgEl);
            if (blob) return blob;

            return imgEl.getAttribute('data-src') || '';
        } catch (e) {
            return '';
        }
    }

    function ensureImgSrc(modal, imgEl) {
        // Set src from cached blob (if ready) else from data-src.
        try {
            if (!imgEl || !imgEl.getAttribute || !imgEl.setAttribute) return '';
            if (modal) maybeSwapImgToBlob(modal, imgEl);

            var src = imgEl.getAttribute('src') || '';
            if (src) return src;

            // If a bundle is advertised but not ready yet, avoid falling back to /thumb/*.jpg
            // (that would cause a burst of JPG GETs and defeats the optimization).
            try {
                var root = modal && modal.querySelector ? modal.querySelector('#event_modal_root') : null;
                var bundleUrl = root && root.getAttribute ? (root.getAttribute('data-bundle-url') || '') : '';
                if (bundleUrl) {
                    var st = window.__khEventBundleStateByUrl.get(String(bundleUrl));
                    var startedAt = st && st.startedAtMs ? st.startedAtMs : 0;
                    var status = st && st.status ? st.status : '';
                    var age = startedAt ? (Date.now() - startedAt) : 0;
                    // Wait up to 2500ms for the TAR to land; afterwards allow JPG fallback.
                    if (status !== 'ready' && age > 0 && age < 2500) {
                        return '';
                    }
                }
            } catch (e) {}

            var ds = imgEl.getAttribute('data-src') || '';
            if (ds) {
                imgEl.setAttribute('src', ds);
                return ds;
            }
        } catch (e) {}
        return (imgEl && imgEl.getAttribute) ? (imgEl.getAttribute('src') || '') : '';
    }

    function q(sel, root) {
        try {
            return (root || document).querySelector(sel);
        } catch (e) {
            return null;
        }
    }

    function getEventBlockId() {
        try {
            var rootEl = document.getElementById('event_modal_root');
            var blockId = rootEl && rootEl.getAttribute ? rootEl.getAttribute('data-block-id') : null;
            return blockId ? String(blockId) : '';
        } catch (e) {
            return '';
        }
    }

    function setWrapBg(wrap, src) {
        try {
            if (!wrap) return;
            if (!src) return;
            // Prefer a real <img> backdrop (more reliable than CSS background-image on Firefox mobile).
            var bg = null;
            // Prefer placing the backdrop inside the persistent JS layer if present.
            var host = null;
            try { host = wrap.querySelector('#event_modal_js_layer') || wrap; } catch (e) { host = wrap; }
            try { bg = host.querySelector('img[data-role="bg"]'); } catch (e) { bg = null; }
            if (!bg) {
                try {
                    bg = document.createElement('img');
                    bg.setAttribute('data-role', 'bg');
                    bg.setAttribute('aria-hidden', 'true');
                    bg.className = 'event-modal-picture-bg';
                    // Help mobile browsers prioritize decode.
                    try { bg.decoding = 'async'; } catch (e) {}
                    try { bg.loading = 'eager'; } catch (e) {}
                    host.insertBefore(bg, host.firstChild);
                } catch (e) {
                    bg = null;
                }
            }
            if (bg) {
                var cur = bg.getAttribute('src') || '';
                if (cur !== src) bg.setAttribute('src', src);
            }
            // Keep CSS var as a fallback (older CSS versions).
            wrap.style.setProperty('--kh-event-bg', 'url("' + src + '")');
        } catch (e) {}
    }

    function getPersistentPreloader() {
        // Persistent Image() preloader for clean, controlled swaps.
        try {
            if (!window.__khEventModalPreloader) {
                var im = new Image();
                try { im.decoding = 'async'; } catch (e) {}
                window.__khEventModalPreloader = { img: im, target: '', pid: null };
            }
            return window.__khEventModalPreloader;
        } catch (e) {
            return null;
        }
    }

    function getPrefetchPool() {
        try {
            if (!window.__khEventModalPrefetchPool) {
                var im1 = new Image();
                var im2 = new Image();
                try { im1.decoding = 'async'; } catch (e) {}
                try { im2.decoding = 'async'; } catch (e) {}
                window.__khEventModalPrefetchPool = {
                    imgs: [im1, im2],
                    idx: 0,
                    seenAtMsBySrc: new Map(),
                };
            }
            return window.__khEventModalPrefetchPool;
        } catch (e) {
            return null;
        }
    }

    function prefetchSrc(src) {
        try {
            if (!src) return;
            var pool = getPrefetchPool();
            if (!pool || !pool.imgs || pool.imgs.length === 0) return;

            var now = Date.now();
            var last = pool.seenAtMsBySrc.get(src) || 0;
            // Avoid hammering the same URL on rapid button repeats.
            if (now - last < 2500) return;
            pool.seenAtMsBySrc.set(src, now);

            // Prevent unbounded growth.
            if (pool.seenAtMsBySrc.size > 200) {
                pool.seenAtMsBySrc.clear();
            }

            var im = pool.imgs[pool.idx % pool.imgs.length];
            pool.idx = (pool.idx + 1) % pool.imgs.length;
            try { im.src = src; } catch (e) {}
        } catch (e) {}
    }

    function ensureJsImageLayer(wrap) {
        try {
            if (!wrap) return null;
            var layer = document.getElementById('event_modal_js_layer');
            if (!layer) {
                layer = document.createElement('div');
                layer.id = 'event_modal_js_layer';
                layer.className = 'event-modal-js-layer';
                wrap.insertBefore(layer, wrap.firstChild);
            }

            // Backdrop is managed by setWrapBg (uses this layer).
            // Use a double-buffered visible image pair to avoid rare Chrome mobile
            // black flashes during rapid src swaps (old frame stays visible until
            // the new frame has painted).
            var a = null;
            var b = null;
            try { a = layer.querySelector('img[data-role="visible"][data-slot="a"]'); } catch (e) { a = null; }
            try { b = layer.querySelector('img[data-role="visible"][data-slot="b"]'); } catch (e) { b = null; }

            function mk(slot) {
                var el = document.createElement('img');
                el.setAttribute('data-role', 'visible');
                el.setAttribute('data-slot', slot);
                // Start hidden; we unhide the active one.
                el.setAttribute('aria-hidden', 'true');
                // Help mobile browsers prioritize decode.
                try { el.decoding = 'async'; } catch (e) {}
                try { el.loading = 'eager'; } catch (e) {}
                // Ensure deterministic stacking when both are temporarily visible.
                try { el.style.zIndex = (slot === 'a') ? '2' : '1'; } catch (e) {}
                layer.appendChild(el);
                return el;
            }

            if (!a) a = mk('a');
            if (!b) b = mk('b');

            // Default active slot.
            var activeSlot = '';
            try { activeSlot = wrap.getAttribute('data-active-slot') || ''; } catch (e) { activeSlot = ''; }
            if (activeSlot !== 'a' && activeSlot !== 'b') {
                activeSlot = 'a';
                try { wrap.setAttribute('data-active-slot', activeSlot); } catch (e) {}
            }

            // IMPORTANT: attachOnce() can run many times per second (MutationObserver + poll).
            // If we force visibility while a swap is mid-flight, we can momentarily show the old
            // frame again (jitter). During a buffered swap, leave the DOM as-is.
            var swapInProgress = '';
            try { swapInProgress = wrap.getAttribute('data-swap-in-progress') || ''; } catch (e) { swapInProgress = ''; }
            if (!swapInProgress) {
                // Ensure only the active one is visible.
                try {
                    if (activeSlot === 'a') {
                        a.removeAttribute('aria-hidden');
                        b.setAttribute('aria-hidden', 'true');
                        a.style.zIndex = '2';
                        b.style.zIndex = '1';
                    } else {
                        b.removeAttribute('aria-hidden');
                        a.setAttribute('aria-hidden', 'true');
                        b.style.zIndex = '2';
                        a.style.zIndex = '1';
                    }
                } catch (e) {}
            }

            function getActive() {
                var slot = '';
                try { slot = wrap.getAttribute('data-active-slot') || 'a'; } catch (e) { slot = 'a'; }
                return (slot === 'b') ? b : a;
            }

            function getInactive() {
                var slot = '';
                try { slot = wrap.getAttribute('data-active-slot') || 'a'; } catch (e) { slot = 'a'; }
                return (slot === 'b') ? a : b;
            }

            function setActiveSlot(slot) {
                if (slot !== 'a' && slot !== 'b') return;
                try { wrap.setAttribute('data-active-slot', slot); } catch (e) {}
            }

            return { layer: layer, getActive: getActive, getInactive: getInactive, setActiveSlot: setActiveSlot, a: a, b: b };
        } catch (e) {
            return null;
        }
    }

    function lookupBlobUrlByPid(modal, pid) {
        try {
            if (!modal) return '';
            var root = modal.querySelector ? modal.querySelector('#event_modal_root') : null;
            var bundleUrl = root && root.getAttribute ? (root.getAttribute('data-bundle-url') || '') : '';
            var key = bundleUrl ? String(bundleUrl) : '';
            if (!key) {
                var blockId = root && root.getAttribute ? root.getAttribute('data-block-id') : null;
                if (!blockId) return '';
                key = String(blockId);
            }

            var entry = window.__khEventBundles.get(String(key));
            if (!entry || !entry.urlsByPid) return '';
            return entry.urlsByPid.get(pid) || '';
        } catch (e) {
            return '';
        }
    }

    function desiredSrcForPid(modal, pid, fallbackUrl) {
        try {
            if (!pid) return '';

            var blob = lookupBlobUrlByPid(modal, pid);
            if (blob) return blob;
            return fallbackUrl || '';
        } catch (e) {
            return '';
        }
    }

    function safeClick(el) {
        try {
            if (el && typeof el.click === 'function') el.click();
        } catch (e) {
            // ignore
        }
    }

    function initModalStaticPulseObserver() {
        // Guard against multiple observers in case init() is called more than once.
        if (window.__khModalStaticObserver) return;
        window.__khModalStaticObserver = true;

        function setupPulseObserver() {
            var modal = document.querySelector('.modal');
            var btn = document.querySelector('button[id$="modal_pulse"]');
            if (!modal || !btn) {
                setTimeout(setupPulseObserver, 100);
                return;
            }
            try {
                var observer = new MutationObserver(function (mutations) {
                    mutations.forEach(function (mutation) {
                        if (
                            mutation.type === "attributes" &&
                            mutation.attributeName === "class" &&
                            mutation.target.classList.contains("modal-static") &&
                            (!mutation.oldValue || !mutation.oldValue.includes("modal-static"))
                        ) {
                            safeClick(btn);
                            try { console.log('Shiny modal_pulse button clicked'); } catch (e) {}
                        }
                    });
                });
                observer.observe(modal, { attributes: true, attributeOldValue: true });
            } catch (e) {
                // ignore
            }
        }
        setupPulseObserver();
    }

    function initEventImageObserver() {
        // This observer is designed for Shiny re-renders that replace DOM nodes.
        // It:
        // - prevents modal height collapse (wrapper has CSS aspect-ratio + min-height)
        // - keeps previous frame visible as wrapper background while next loads
        // - advances slideshow ONLY after the current frame is confirmed loaded in the browser

        // Guard against multiple poll loops in case init() is called more than once.
        if (window.__khEventImagePoller) return;
        window.__khEventImagePoller = true;

        function attachOnce() {
            // Note: the modal is created/destroyed dynamically by Shiny.
            // Do NOT stop polling permanently if it's not present yet.

            var wrap = document.getElementById('event_modal_picture_wrap');
            var pulseBtn = q('button[id$="img_loaded_pulse"]');
            var modal = document.querySelector('.modal');

            var blockIdKey = getEventBlockId();
            if (wrap && blockIdKey) {
                // If wrap was recreated by Shiny, restore last shown frame immediately.
                var remembered = window.__khEventModalLastShownByBlock.get(blockIdKey) || '';
                if (remembered) setWrapBg(wrap, remembered);
            }

            // Kick off bundle fetch if available (non-blocking).
            try {
                var rootEl = document.getElementById('event_modal_root');
                var blockId = rootEl && rootEl.getAttribute ? rootEl.getAttribute('data-block-id') : null;
                var bundleUrl = rootEl && rootEl.getAttribute ? (rootEl.getAttribute('data-bundle-url') || '') : '';
                if (blockId && bundleUrl) {
                    // Start download in background; swap as soon as URLs are present.
                    fetchAndCacheBundle(blockId, bundleUrl);
                }
            } catch (e) {}

            if (!wrap || !pulseBtn) return;

            var layerInfo = ensureJsImageLayer(wrap);
            if (!layerInfo || !layerInfo.getActive || !layerInfo.getInactive) return;
            var img = layerInfo.getActive();
            var imgAlt = layerInfo.getInactive();
            if (!img || !imgAlt) return;

            // Read Shiny-rendered indicator (no <img> tags in reactive output).
            var ind = document.getElementById('event_modal_indicator');
            if (!ind || !ind.getAttribute) return;

            var pid = parseIntSafe(ind.getAttribute('data-visible-pid'), null);
            var fallbackUrl = ind.getAttribute('data-visible-src') || '';
            var prevPid = parseIntSafe(ind.getAttribute('data-prev-pid'), null);
            var prevFallbackUrl = ind.getAttribute('data-prev-src') || '';
            var nextPid = parseIntSafe(ind.getAttribute('data-next-pid'), null);
            var nextFallbackUrl = ind.getAttribute('data-next-src') || '';
            var playingNow = (ind.getAttribute('data-playing') === '1');
            if (pid === null) return;

            // If playback just resumed, kick it once so the server advances to the next frame.
            try {
                var lastPlayAttr = wrap.getAttribute('data-playing-last') || '';
                var nowPlayAttr = playingNow ? '1' : '0';
                if (lastPlayAttr !== nowPlayAttr) {
                    wrap.setAttribute('data-playing-last', nowPlayAttr);
                    if (playingNow) {
                        // Clear any pending pulse-throttle state.
                        try { wrap.removeAttribute('data-pulse-timeout'); } catch (e) {}
                        try { wrap.setAttribute('data-last-pulse-ms', String(Date.now())); } catch (e) {}
                        safeClick(pulseBtn);
                    }
                }
            } catch (e) {}

            // Mark that the dedicated event-modal observer is active (so global app.js doesn't interfere).
            try {
                wrap.setAttribute('data-event-modal-observer', '1');
            } catch (e) {}

            // Determine desired src (blob if ready, else /thumb/<id>.jpg if allowed).
            var src = desiredSrcForPid(modal, pid, fallbackUrl) || '';
            if (!src) return;

            // Prefetch adjacent frames so rapid prev/next feels instant.
            try {
                if (prevPid !== null) {
                    var psrc = desiredSrcForPid(modal, prevPid, prevFallbackUrl) || '';
                    if (psrc && psrc !== src) prefetchSrc(psrc);
                }
                if (nextPid !== null) {
                    var nsrc = desiredSrcForPid(modal, nextPid, nextFallbackUrl) || '';
                    if (nsrc && nsrc !== src) prefetchSrc(nsrc);
                }
            } catch (e) {}

            var currentSrc = wrap.getAttribute('data-current-src') || '';
            var shownSrc = wrap.getAttribute('data-shown-src') || '';

            if (!shownSrc && blockIdKey) {
                shownSrc = window.__khEventModalLastShownByBlock.get(blockIdKey) || '';
            }

            function syncShownMeta(finalSrc) {
                try {
                    if (!finalSrc) return;
                    wrap.classList.add('has-image');
                    wrap.setAttribute('data-shown-src', finalSrc);
                    if (blockIdKey) window.__khEventModalLastShownByBlock.set(blockIdKey, finalSrc);
                } catch (e) {}
            }

            function scheduleBgUpdate(finalSrc) {
                // Chrome mobile can briefly flash black if we switch the backdrop to the new
                // frame before the visible <img> has actually painted. Keep the old backdrop
                // for a couple frames, then update.
                try {
                    if (!finalSrc) return;
                    var token = String(Date.now()) + ':' + String(Math.random());
                    wrap.setAttribute('data-bg-pending', token);

                    var raf = (window.requestAnimationFrame || function (cb) { return setTimeout(cb, 16); });
                    raf(function () {
                        raf(function () {
                            try {
                                if (wrap.getAttribute('data-bg-pending') !== token) return;
                                var activeImg = null;
                                try { activeImg = layerInfo.getActive ? layerInfo.getActive() : img; } catch (e) { activeImg = img; }
                                var visNow = (activeImg && activeImg.getAttribute && activeImg.getAttribute('src')) ? (activeImg.getAttribute('src') || '') : '';
                                if (visNow === finalSrc) setWrapBg(wrap, finalSrc);
                                wrap.removeAttribute('data-bg-pending');
                            } catch (e) {}
                        });
                    });
                } catch (e) {}
            }

            function decodePromiseFor(el) {
                try {
                    if (!el) return Promise.resolve();
                    if (typeof el.decode === 'function') {
                        return el.decode().catch(function () { return null; });
                    }
                } catch (e) {}
                return Promise.resolve();
            }

            function swapBufferedTo(finalSrc) {
                // Swap using the inactive <img> as a buffer.
                // Steps:
                // 1) Put finalSrc into inactive img.
                // 2) Wait for decode() best-effort.
                // 3) Unhide inactive (new frame) on a frame boundary.
                // 4) Hide old active on the next frame boundary.
                try {
                    if (!finalSrc) return;

                    // Resolve current active/inactive at the moment of calling.
                    var active = null;
                    var inactive = null;
                    try { active = layerInfo.getActive(); } catch (e) { active = img; }
                    try { inactive = layerInfo.getInactive(); } catch (e) { inactive = imgAlt; }
                    if (!active || !inactive) return;

                    var activeSrc = (active.getAttribute && active.getAttribute('src')) ? (active.getAttribute('src') || '') : '';
                    if (activeSrc === finalSrc) {
                        syncShownMeta(finalSrc);
                        scheduleBgUpdate(finalSrc);
                        maybePulseLoadedFor(finalSrc);
                        return;
                    }

                    // Cancel any prior pending buffer swap.
                    var token = String(Date.now()) + ':' + String(Math.random());
                    wrap.setAttribute('data-swap-token', token);
                    // Mark swap-in-progress so ensureJsImageLayer doesn't fight us.
                    wrap.setAttribute('data-swap-in-progress', token);

                    // Prepare inactive.
                    try { inactive.setAttribute('aria-hidden', 'true'); } catch (e) {}
                    try { inactive.setAttribute('src', finalSrc); } catch (e) {}

                    decodePromiseFor(inactive).then(function () {
                        try {
                            if (wrap.getAttribute('data-swap-token') !== token) return;
                            var wantNow = wrap.getAttribute('data-current-src') || '';
                            if (wantNow && wantNow !== finalSrc) {
                                try {
                                    if (wrap.getAttribute('data-swap-in-progress') === token) {
                                        wrap.removeAttribute('data-swap-in-progress');
                                    }
                                } catch (e) {}
                                return;
                            }

                            var raf = (window.requestAnimationFrame || function (cb) { return setTimeout(cb, 16); });

                            // Show new frame first.
                            raf(function () {
                                try {
                                    if (wrap.getAttribute('data-swap-token') !== token) return;
                                    // Put new frame on top.
                                    try { inactive.style.zIndex = '2'; } catch (e) {}
                                    try { active.style.zIndex = '1'; } catch (e) {}
                                    try { inactive.removeAttribute('aria-hidden'); } catch (e) {}

                                    // Then hide the old frame on the next tick.
                                    raf(function () {
                                        try {
                                            if (wrap.getAttribute('data-swap-token') !== token) return;
                                            try { active.setAttribute('aria-hidden', 'true'); } catch (e) {}
                                            // Flip active slot marker.
                                            try {
                                                var newSlot = inactive.getAttribute ? (inactive.getAttribute('data-slot') || '') : '';
                                                if (layerInfo.setActiveSlot && (newSlot === 'a' || newSlot === 'b')) {
                                                    layerInfo.setActiveSlot(newSlot);
                                                }
                                            } catch (e) {}

                                            // Swap finished.
                                            try {
                                                if (wrap.getAttribute('data-swap-in-progress') === token) {
                                                    wrap.removeAttribute('data-swap-in-progress');
                                                }
                                            } catch (e) {}

                                            // Sync state + backdrop after the new frame is actually visible.
                                            syncShownMeta(finalSrc);
                                            scheduleBgUpdate(finalSrc);
                                            maybePulseLoadedFor(finalSrc);
                                        } catch (e) {}
                                    });
                                } catch (e) {}
                            });
                        } catch (e) {}
                    });
                } catch (e) {}
            }

            function maybePulseLoadedFor(finalSrc) {
                try {
                    var lastAck = wrap.getAttribute('data-last-ack-src') || '';
                    if (lastAck === finalSrc) return;
                    wrap.setAttribute('data-last-ack-src', finalSrc);

                    // Pace autoplay to ~4fps (250ms). When paused we pulse immediately so overlays stay in sync.
                    var fpsIntervalMs = 250;
                    if (!playingNow) {
                        safeClick(pulseBtn);
                        return;
                    }

                    var now = Date.now();
                    var lastMs = parseIntSafe(wrap.getAttribute('data-last-pulse-ms'), 0) || 0;
                    var dueIn = (lastMs + fpsIntervalMs) - now;
                    if (dueIn <= 0) {
                        wrap.setAttribute('data-last-pulse-ms', String(now));
                        safeClick(pulseBtn);
                        return;
                    }

                    // Avoid stacking timeouts during fast cache hits.
                    var tok = wrap.getAttribute('data-pulse-timeout') || '';
                    if (tok) return;
                    wrap.setAttribute('data-pulse-timeout', '1');
                    setTimeout(function () {
                        try {
                            wrap.removeAttribute('data-pulse-timeout');
                            var now2 = Date.now();
                            wrap.setAttribute('data-last-pulse-ms', String(now2));
                            safeClick(pulseBtn);
                        } catch (e) {}
                    }, Math.min(2000, Math.max(0, dueIn)));
                } catch (e) {}
            }

            function swapTo(finalSrc) {
                // Keep old frame visible until the new one has painted.
                try {
                    swapBufferedTo(finalSrc);
                } catch (e) {}
            }

            if (currentSrc !== src) {
                wrap.setAttribute('data-current-src', src);
                try {
                    wrap.setAttribute('data-current-at-ms', String(Date.now()));
                } catch (e) {}

                // Ensure we always have something behind the visible layer.
                var activeNow = null;
                try { activeNow = layerInfo.getActive ? layerInfo.getActive() : img; } catch (e) { activeNow = img; }
                var fallbackBg = shownSrc || (activeNow && activeNow.getAttribute ? (activeNow.getAttribute('src') || '') : '') || '';
                if (fallbackBg) setWrapBg(wrap, fallbackBg);

                // Preload using persistent Image() to avoid any Shiny DOM races.
                var pre = getPersistentPreloader();
                if (!pre || !pre.img) {
                    swapTo(src);
                } else {
                    pre.target = src;
                    pre.pid = pid;
                    try {
                        pre.img.src = src;
                        // If it's already in cache, swap without waiting for the async load event.
                        try {
                            if (pre.img.complete && pre.img.naturalWidth > 0) {
                                // Defer a tick so pre.img.src has settled.
                                setTimeout(function () {
                                    try {
                                        var wantNow = wrap.getAttribute('data-current-src') || '';
                                        if (wantNow && wantNow === src) swapTo(src);
                                    } catch (e) {}
                                }, 0);
                            }
                        } catch (e) {}
                    } catch (e) {
                        swapTo(src);
                    }
                }
            } else {
                // If server re-render replaced the <img> node, ensure it has the current src.
                var existing = (img.getAttribute && img.getAttribute('src')) ? (img.getAttribute('src') || '') : '';
                if (!existing) swapTo(src);
            }

            // Bind persistent preloader handlers once.
            var pre2 = getPersistentPreloader();
            if (pre2 && pre2.img && !pre2.__bound) {
                pre2.__bound = true;
                pre2.img.addEventListener('load', function () {
                    try {
                        var target = pre2.target || '';
                        var want = wrap.getAttribute('data-current-src') || '';
                        if (target && want && target === want) swapTo(target);
                    } catch (e) {}
                });
                pre2.img.addEventListener('error', function () {
                    try {
                        var want = wrap.getAttribute('data-current-src') || '';
                        if (want) swapTo(want);
                    } catch (e) {}
                });
            }
        }


        function setupObserverLoop() {
            try {
                attachOnce();
            } catch (e) {}

            // Observe the Shiny output container so updates are instant.
            try {
                // NOTE: Shiny module output IDs are namespaced, so don't rely on a fixed "show_event_picture" id.
                // Observe our own stable container instead.
                var out = document.getElementById('event_modal_overlay_container') || document.getElementById('event_modal_picture_wrap');
                if (out) {
                    var prevTarget = window.__khEventModalIndicatorObserverTarget || null;
                    if (prevTarget !== out) {
                        try {
                            if (window.__khEventModalIndicatorObserver && window.__khEventModalIndicatorObserver.disconnect) {
                                window.__khEventModalIndicatorObserver.disconnect();
                            }
                        } catch (e) {}
                        try {
                            var obs = new MutationObserver(function () {
                                try { attachOnce(); } catch (e) {}
                            });
                            obs.observe(out, { childList: true, subtree: true, attributes: true });
                            window.__khEventModalIndicatorObserver = obs;
                            window.__khEventModalIndicatorObserverTarget = out;
                        } catch (e) {}
                    }
                }
            } catch (e) {}

            // Still keep a lightweight poll as a fallback (covers modal open/close).
            setTimeout(setupObserverLoop, document.querySelector('.modal') ? 250 : 800);
        }

        setupObserverLoop();
    }

    // Expose a single init entrypoint so server-rendered HTML can call it.
    // It is safe to call multiple times.
    window.kittyhackEventModal = window.kittyhackEventModal || {};
    window.kittyhackEventModal.init = function () {
        try { initModalStaticPulseObserver(); } catch (e) {}
        try { initEventImageObserver(); } catch (e) {}
    };

    // Auto-init (safe because observers poll and are idempotent)
    try {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () {
                try { window.kittyhackEventModal.init(); } catch (e) {}
            }, { once: true });
        } else {
            setTimeout(function () {
                try { window.kittyhackEventModal.init(); } catch (e) {}
            }, 0);
        }
    } catch (e) {}
})();
