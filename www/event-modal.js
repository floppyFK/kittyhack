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
            try { bg = wrap.querySelector('img[data-role="bg"]'); } catch (e) { bg = null; }
            if (!bg) {
                try {
                    bg = document.createElement('img');
                    bg.setAttribute('data-role', 'bg');
                    bg.setAttribute('aria-hidden', 'true');
                    bg.className = 'event-modal-picture-bg';
                    // Help mobile browsers prioritize decode.
                    try { bg.decoding = 'async'; } catch (e) {}
                    try { bg.loading = 'eager'; } catch (e) {}
                    wrap.insertBefore(bg, wrap.firstChild);
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
            var pic = document.getElementById('event_modal_picture');
            var img = q('#event_modal_picture img[data-role="visible"]');
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

            if (!wrap || !pic || !img || !pulseBtn) return;

            // Mark that the dedicated event-modal observer is active (so global app.js doesn't interfere).
            try {
                wrap.setAttribute('data-event-modal-observer', '1');
            } catch (e) {}

            // Ensure src is set (blob if available, else data-src) BEFORE we proceed.
            var preload = q('#event_modal_picture img[aria-hidden="true"]');
            try {
                if (preload) ensureImgSrc(modal, preload);
            } catch (e) {}

            var src = ensureImgSrc(modal, img) || '';
            if (!src) return;

            // Detect src changes across server re-renders (DOM replacement)
            var currentSrc = wrap.getAttribute('data-current-src') || '';
            var shownSrc = wrap.getAttribute('data-shown-src') || '';

            // Prefer per-block remembered value if wrap doesn't have it yet.
            if (!shownSrc && blockIdKey) {
                shownSrc = window.__khEventModalLastShownByBlock.get(blockIdKey) || '';
            }

            if (currentSrc !== src) {
                wrap.setAttribute('data-current-src', src);
                // Keep old visible as background until new is loaded
                if (shownSrc) {
                    setWrapBg(wrap, shownSrc);
                } else {
                    // If we don't have a stored shownSrc yet, at least avoid flashing the black background.
                    setWrapBg(wrap, src);
                }

                // Only hide/fade the visible image if we have a previous frame behind it.
                // This prevents "first play on mobile" from briefly showing black while decoding.
                var canHide = !!shownSrc;
                try {
                    if (img && img.complete && img.naturalWidth > 0) {
                        canHide = false;
                    }
                } catch (e) {}
                if (canHide) {
                    try { wrap.classList.remove('has-image'); } catch (e) {}
                }
            }

            function maybePulseLoaded() {
                // Re-read on demand so play/pause changes are respected.
                var playingNow = (pic.getAttribute('data-playing') === '1');
                if (!playingNow) return;
                var lastAck = wrap.getAttribute('data-last-ack-src') || '';
                if (lastAck === src) return;
                wrap.setAttribute('data-last-ack-src', src);
                safeClick(pulseBtn);
            }

            function maybeSetAspectRatio() {
                // Set aspect ratio once per modal/event based on the first image.
                try {
                    if (wrap.getAttribute('data-aspect-set') === '1') return;
                    var w = img.naturalWidth || 0;
                    var h = img.naturalHeight || 0;
                    if (w > 0 && h > 0) {
                        wrap.style.aspectRatio = String(w) + ' / ' + String(h);
                        wrap.setAttribute('data-aspect-set', '1');
                    }
                } catch (e) {}
            }

            // (Re)attach handlers for this img element
            if (!img.__kittyhackBound) {
                img.__kittyhackBound = true;
                img.addEventListener('load', function () {
                    try {
                        maybeSetAspectRatio();
                        wrap.classList.add('has-image');
                        wrap.setAttribute('data-shown-src', src);
                        // Keep current frame as background so transitions never flash black.
                        setWrapBg(wrap, src);
                        if (blockIdKey) window.__khEventModalLastShownByBlock.set(blockIdKey, src);
                    } catch (e) {}
                    maybePulseLoaded();
                });
                img.addEventListener('error', function () {
                    // Don't stall the slideshow forever on a failed request.
                    try {
                        wrap.classList.add('has-image');
                        // Keep background (previous shownSrc) if any
                    } catch (e) {}
                    maybePulseLoaded();
                });
            }

            // If already loaded (from cache), ensure state is updated and pulse once if playing
            try {
                if (img.complete && img.naturalWidth > 0) {
                    maybeSetAspectRatio();
                    wrap.classList.add('has-image');
                    wrap.setAttribute('data-shown-src', src);
                    setWrapBg(wrap, src);
                    if (blockIdKey) window.__khEventModalLastShownByBlock.set(blockIdKey, src);
                    maybePulseLoaded();
                }
            } catch (e) {}
        }


        function poll() {
            try { attachOnce(); } catch (e) {}
            // Slightly slower when modal isn't present yet.
            var delay = document.querySelector('.modal') ? 250 : 500;
            setTimeout(poll, delay);
        }

        poll();
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
