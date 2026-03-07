// Kittyhack – Event modal: fully client-side player
// After the server sends initial frame data (JSON in the modal DOM),
// all playback, scrubbing, overlay rendering and navigation happen
// entirely in the browser. Only delete + download still hit the server.

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

    // =====================================================================
    // Section 1 – Bundle cache globals
    // =====================================================================
    window.__khEventBundles = window.__khEventBundles || new Map();
    window.__khEventModalLastShownByBlock = window.__khEventModalLastShownByBlock || new Map();
    window.__khEventBundleStateByUrl = window.__khEventBundleStateByUrl || new Map();

    // =====================================================================
    // Section 2 – Utility helpers
    // =====================================================================
    function parseIntSafe(v, fallback) {
        var n = parseInt(v, 10);
        return (typeof n === 'number' && isFinite(n)) ? n : fallback;
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
        var u8 = new Uint8Array(arrayBuffer);
        var offset = 0;
        while (offset + 512 <= u8.length) {
            var header = u8.subarray(offset, offset + 512);
            if (tarIsAllZero(header)) break;
            var name = decodeNullTerminatedAscii(u8, offset + 0, offset + 100);
            var sizeOct = decodeNullTerminatedAscii(u8, offset + 124, offset + 136).trim();
            var size = 0;
            if (sizeOct) { try { size = parseInt(sizeOct, 8) || 0; } catch (e) { size = 0; } }
            var fileStart = offset + 512;
            var fileEnd = fileStart + size;
            if (fileEnd > u8.length) break;
            try { onFile(name, u8.subarray(fileStart, fileEnd)); } catch (e) {}
            offset = fileStart + Math.ceil(size / 512) * 512;
        }
    }

    function canDecompressGzip() {
        try { return (typeof DecompressionStream !== 'undefined'); } catch (e) { return false; }
    }

    // =====================================================================
    // Section 3 – Bundle fetch / cache
    // =====================================================================
    async function fetchAndCacheBundle(blockId, bundleUrl) {
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

        if (window.__khEventBundles.get(key) && window.__khEventBundles.get(key).__inFlight) return;
        window.__khEventBundles.set(key, { urlsByPid: new Map(), lastUsedMs: Date.now(), __inFlight: true });

        try {
            var resp = await fetch(bundleUrl, { cache: 'force-cache' });
            if (!resp || !resp.ok) return;

            var buf;
            var isGz = /\.gz(?:\?|#|$)/.test(bundleUrl);
            if (isGz) {
                if (!canDecompressGzip() || !resp.body) return;
                buf = await new Response(resp.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
            } else {
                buf = await resp.arrayBuffer();
            }

            var urlsByPid = new Map();
            parseTar(buf, function (name, bytes) {
                if (!name || typeof name !== 'string') return;
                var m = name.match(/(^|\/)(\d+)\.jpg$/);
                if (!m) return;
                var pid = parseIntSafe(m[2], null);
                if (pid === null) return;
                try {
                    urlsByPid.set(pid, URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' })));
                } catch (e) {}
            });

            var entry = window.__khEventBundles.get(key) || { urlsByPid: new Map(), lastUsedMs: Date.now() };
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
                window.__khEventBundleStateByUrl.set(key, st1);
            } catch (e) {}

            // Prune old bundles (keep last 3)
            try {
                if (window.__khEventBundles.size > 3) {
                    var items = Array.from(window.__khEventBundles.entries());
                    items.sort(function (a, b) { return (a[1].lastUsedMs || 0) - (b[1].lastUsedMs || 0); });
                    while (items.length > 3) {
                        var evict = items.shift();
                        if (!evict) break;
                        try {
                            if (evict[1] && evict[1].urlsByPid) evict[1].urlsByPid.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) {} });
                        } catch (e) {}
                        window.__khEventBundles.delete(evict[0]);
                    }
                }
            } catch (e) {}
        } catch (e) {
            try {
                var stE = window.__khEventBundleStateByUrl.get(key) || {};
                stE.status = 'error';
                window.__khEventBundleStateByUrl.set(key, stE);
            } catch (e) {}
        } finally {
            try {
                var ent = window.__khEventBundles.get(key);
                if (ent) ent.__inFlight = false;
            } catch (e) {}
        }
    }

    // =====================================================================
    // Section 4 – Blob-URL helpers
    // =====================================================================
    function lookupBlobUrlByPid(bundleKey, pid) {
        try {
            var entry = window.__khEventBundles.get(String(bundleKey));
            if (!entry || !entry.urlsByPid) return '';
            return entry.urlsByPid.get(pid) || '';
        } catch (e) { return ''; }
    }

    // =====================================================================
    // Section 5 – DOM / image helpers
    // =====================================================================
    function setWrapBg(wrap, src) {
        try {
            if (!wrap || !src) return;
            var host = wrap.querySelector('#event_modal_js_layer') || wrap;
            var bg = host.querySelector('img[data-role="bg"]');
            if (!bg) {
                try {
                    bg = document.createElement('img');
                    bg.setAttribute('data-role', 'bg');
                    bg.setAttribute('aria-hidden', 'true');
                    bg.className = 'event-modal-picture-bg';
                    try { bg.decoding = 'async'; } catch (e) {}
                    try { bg.loading = 'eager'; } catch (e) {}
                    host.insertBefore(bg, host.firstChild);
                } catch (e) { bg = null; }
            }
            if (bg) {
                if ((bg.getAttribute('src') || '') !== src) bg.setAttribute('src', src);
            }
            wrap.style.setProperty('--kh-event-bg', 'url("' + src + '")');
        } catch (e) {}
    }

    function ensureJsImageLayer(wrap) {
        try {
            if (!wrap) return null;
            var layer = wrap.querySelector('#event_modal_js_layer');
            if (!layer) {
                layer = document.createElement('div');
                layer.id = 'event_modal_js_layer';
                layer.className = 'event-modal-js-layer';
                wrap.insertBefore(layer, wrap.firstChild);
            }

            var a = layer.querySelector('img[data-role="visible"][data-slot="a"]');
            var b = layer.querySelector('img[data-role="visible"][data-slot="b"]');

            function mk(slot) {
                var el = document.createElement('img');
                el.setAttribute('data-role', 'visible');
                el.setAttribute('data-slot', slot);
                el.setAttribute('aria-hidden', 'true');
                try { el.decoding = 'async'; } catch (e) {}
                try { el.loading = 'eager'; } catch (e) {}
                el.style.zIndex = (slot === 'a') ? '2' : '1';
                layer.appendChild(el);
                return el;
            }
            if (!a) a = mk('a');
            if (!b) b = mk('b');

            var activeSlot = wrap.getAttribute('data-active-slot') || 'a';
            if (activeSlot !== 'a' && activeSlot !== 'b') {
                activeSlot = 'a';
                wrap.setAttribute('data-active-slot', activeSlot);
            }

            if (activeSlot === 'a') {
                a.removeAttribute('aria-hidden'); b.setAttribute('aria-hidden', 'true');
                a.style.zIndex = '2'; b.style.zIndex = '1';
            } else {
                b.removeAttribute('aria-hidden'); a.setAttribute('aria-hidden', 'true');
                b.style.zIndex = '2'; a.style.zIndex = '1';
            }

            return {
                layer: layer, a: a, b: b,
                getActive:   function () { return (wrap.getAttribute('data-active-slot') === 'b') ? b : a; },
                getInactive: function () { return (wrap.getAttribute('data-active-slot') === 'b') ? a : b; },
                setActiveSlot: function (s) { if (s === 'a' || s === 'b') wrap.setAttribute('data-active-slot', s); }
            };
        } catch (e) { return null; }
    }

    function prefetchSrc(src) {
        try {
            if (!src) return;
            var pool = window.__khPlayerPrefetchPool;
            if (!pool) {
                var im1 = new Image(); var im2 = new Image();
                try { im1.decoding = 'async'; } catch (e) {} try { im2.decoding = 'async'; } catch (e) {}
                pool = { imgs: [im1, im2], idx: 0, seen: new Map() };
                window.__khPlayerPrefetchPool = pool;
            }
            var now = Date.now();
            if ((pool.seen.get(src) || 0) > now - 2500) return;
            pool.seen.set(src, now);
            if (pool.seen.size > 200) pool.seen.clear();
            pool.imgs[pool.idx % pool.imgs.length].src = src;
            pool.idx = (pool.idx + 1) % pool.imgs.length;
        } catch (e) {}
    }

    // =====================================================================
    // Section 6 – Client-side Event Player
    // =====================================================================
    var _player = null;

    /**
     * @constructor
     * @param {HTMLElement} rootEl  – #event_modal_root
     */
    function Player(rootEl) {
        this.root = rootEl;
        this.wrap = rootEl.querySelector('#event_modal_picture_wrap');
        this.overlayContainer = rootEl.querySelector('#event_modal_overlay_container');

        // Parse frame data from embedded JSON
        var dataScript = rootEl.querySelector('#event_modal_data');
        var data = {};
        try { data = JSON.parse((dataScript && dataScript.textContent) || '{}'); } catch (e) { data = {}; }

        this.frames = data.frames || [];
        this.mouseThreshold = parseFloat(data.mouseThreshold) || 50.0;
        this.overlayOn = (data.overlayInitial !== false);
        this.fps = parseFloat(data.fps) || 4.0;
        this.fallbackMode = !!data.fallbackMode;
        this.blockId = String(data.blockId || '');

        this.currentIdx = 0;
        this.playing = true;
        this.playTimer = null;
        this.destroyed = false;

        this.bundleUrl = rootEl.getAttribute('data-bundle-url') || '';
        this.bundleKey = this.bundleUrl || '';
        this.nsFrameIdx = rootEl.getAttribute('data-ns-frame-idx') || '';

        this._layerInfo = null;
        this._swapToken = '';
        this._lastShownSrc = '';
        this._scrubberInput = null;

        // Bound handlers for cleanup
        this._boundKeyDown = null;
        this._boundModalClick = null;
    }

    // ---- Initialisation ------------------------------------------------

    Player.prototype.init = function () {
        if (this.frames.length === 0) {
            this._showEmptyState();
            return;
        }

        this._layerInfo = ensureJsImageLayer(this.wrap);

        // Fetch bundle (non-blocking)
        if (this.bundleUrl) {
            var self = this;
            fetchAndCacheBundle(this.blockId, this.bundleUrl).then(function () {
                if (!self.destroyed) self._upgradeCurrentFrame();
            }).catch(function () {});
        }

        this._buildScrubber();
        this._attachControls();
        this._attachModalCloseHandler();
        this._attachKeyboardHandler();

        // Show first frame
        this.showFrame(0);

        // Auto-play for multi-frame events
        if (this.frames.length > 1) {
            this._startPlayback();
        } else {
            this.playing = false;
        }
        this._updatePlayPauseUI();
        this._updateNavButtonsState();
        this._updateScrubberPlayingState();
    };

    // ---- Frame display -------------------------------------------------

    Player.prototype.showFrame = function (idx) {
        if (this.destroyed) return;
        if (idx < 0 || idx >= this.frames.length) return;
        this.currentIdx = idx;

        var frame = this.frames[idx];
        var src = this._resolveFrameSrc(frame.pid);

        if (!src) {
            // Bundle still loading – retry shortly
            var self = this;
            setTimeout(function () { if (!self.destroyed && self.currentIdx === idx) self.showFrame(idx); }, 200);
            return;
        }

        this._swapImage(src);
        this._renderOverlay(frame, idx);
        this._updateScrubber(idx);
        this._signalFrameIdx(idx);
        this._prefetchAdjacent(idx);
    };

    Player.prototype._resolveFrameSrc = function (pid) {
        // Prefer blob URL from bundle cache
        if (this.bundleKey) {
            var blob = lookupBlobUrlByPid(this.bundleKey, pid);
            if (blob) return blob;

            // If bundle is still loading (< 2.5 s), suppress /thumb fallback
            // to avoid a burst of individual HTTP requests
            var st = window.__khEventBundleStateByUrl.get(this.bundleKey);
            if (st && st.status !== 'ready' && st.status !== 'error') {
                var age = st.startedAtMs ? (Date.now() - st.startedAtMs) : 0;
                if (age > 0 && age < 2500) return '';
            }
        }
        return '/thumb/' + pid + '.jpg';
    };

    Player.prototype._upgradeCurrentFrame = function () {
        if (this.destroyed || this.currentIdx < 0 || this.currentIdx >= this.frames.length) return;
        var pid = this.frames[this.currentIdx].pid;
        var newSrc = this._resolveFrameSrc(pid);
        if (newSrc && newSrc.indexOf('blob:') === 0) {
            this._swapImage(newSrc);
        }
    };

    // ---- Double-buffered image swap ------------------------------------

    Player.prototype._swapImage = function (src) {
        if (!src || !this.wrap || !this._layerInfo) return;
        var layer = this._layerInfo;
        var active = layer.getActive();
        var inactive = layer.getInactive();
        if (!active || !inactive) return;

        var currentSrc = active.getAttribute('src') || '';
        if (currentSrc === src) {
            this.wrap.classList.add('has-image');
            return;
        }

        // Keep old frame as backdrop during transition
        if (currentSrc) setWrapBg(this.wrap, currentSrc);
        this.wrap.classList.add('has-image');

        var token = String(Date.now()) + ':' + String(Math.random());
        this._swapToken = token;

        inactive.setAttribute('src', src);

        var self = this;
        var raf = window.requestAnimationFrame || function (cb) { setTimeout(cb, 16); };

        function doSwap() {
            if (self._swapToken !== token || self.destroyed) return;

            inactive.style.zIndex = '2';
            active.style.zIndex = '1';
            inactive.removeAttribute('aria-hidden');

            raf(function () {
                if (self._swapToken !== token || self.destroyed) return;
                active.setAttribute('aria-hidden', 'true');
                var slot = inactive.getAttribute('data-slot') || '';
                if (slot === 'a' || slot === 'b') layer.setActiveSlot(slot);
                setWrapBg(self.wrap, src);
                self._lastShownSrc = src;

                var blockKey = self.blockId ? String(self.blockId) : '';
                if (blockKey) window.__khEventModalLastShownByBlock.set(blockKey, src);
            });
        }

        if (typeof inactive.decode === 'function') {
            inactive.decode().then(doSwap).catch(doSwap);
        } else if (inactive.complete && inactive.naturalWidth > 0) {
            raf(doSwap);
        } else {
            inactive.onload = doSwap;
            inactive.onerror = doSwap;
        }
    };

    // ---- Overlay rendering ---------------------------------------------

    Player.prototype._renderOverlay = function (frame, idx) {
        if (!this.overlayContainer) return;

        var html = '<div id="event_modal_overlay" style="position:absolute;inset:0;pointer-events:none;">';

        if (this.overlayOn && !this.fallbackMode && frame.objects && frame.objects.length > 0) {
            for (var i = 0; i < frame.objects.length; i++) {
                var obj = frame.objects[i];
                var nameL = (obj.name || '').toLowerCase();
                var isPrey = (nameL === 'prey' || nameL === 'beute');
                var prob = parseFloat(obj.prob) || 0;

                var preyStrong = isPrey && prob >= this.mouseThreshold;
                var strokeRgb, strokeHex;

                if (isPrey && !preyStrong) {
                    strokeRgb = '252, 165, 165'; strokeHex = '#fca5a5';
                } else if (isPrey) {
                    strokeRgb = '255, 0, 0'; strokeHex = '#ff0000';
                } else {
                    strokeRgb = '0, 180, 0'; strokeHex = '#00b400';
                }

                var labelPos = (parseFloat(obj.y) || 0) < 16 ? 'bottom: -26px' : 'top: -26px';

                html += '<div style="position:absolute;'
                    + 'left:' + obj.x + '%;top:' + obj.y + '%;'
                    + 'width:' + obj.w + '%;height:' + obj.h + '%;'
                    + 'border:2px solid ' + strokeHex + ';'
                    + 'background-color:rgba(' + strokeRgb + ',0.05);'
                    + 'pointer-events:none;z-index:3;">'
                    + '<div style="position:absolute;' + labelPos + ';left:0px;'
                    + 'background-color:rgba(' + strokeRgb + ',0.7);color:white;'
                    + 'padding:2px 5px;border-radius:5px;white-space:nowrap;font-size:12px;">'
                    + _escHtml(obj.name) + ' (' + Math.round(prob) + '%)</div></div>';
            }
        }

        // Timestamp (keep time portion only: after first space)
        var ts = frame.ts || '';
        if (ts) {
            html += '<div id="event_modal_timestamp" style="position:absolute;top:12px;left:50%;'
                + 'transform:translateX(-50%);background-color:rgba(0,0,0,0.5);color:white;'
                + 'padding:2px 5px;border-radius:3px;z-index:3;">' + _escHtml(ts) + '</div>';
        }

        // Counter
        html += '<div id="event_modal_counter" style="position:absolute;bottom:12px;right:8px;'
            + 'background-color:rgba(0,0,0,0.5);color:white;padding:2px 5px;border-radius:3px;z-index:3;">'
            + (idx + 1) + '/' + this.frames.length + '</div>';

        html += '</div>';
        this.overlayContainer.innerHTML = html;
    };

    function _escHtml(s) {
        if (!s) return '';
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ---- Scrubber (native range input) ---------------------------------

    Player.prototype._buildScrubber = function () {
        var container = document.getElementById('event_modal_scrubber_container');
        if (!container) return;

        var n = this.frames.length;
        if (n <= 1) { container.innerHTML = ''; return; }

        var html = '<div id="event_scrubber_wrap" class="event-modal-scrubber" data-playing="' + (this.playing ? '1' : '0') + '">';
        html += '<div class="event-modal-range-wrap">';
        html += '<input type="range" class="event-modal-range" min="0" max="' + (n - 1) + '" value="0" step="1">';
        html += '</div>';

        // Marker overlay (prey / other detection segments)
        html += '<div class="event-scrubber-markers" aria-hidden="true">';
        html += this._buildMarkerSegments(n);
        html += '</div>';
        html += '</div>';

        container.innerHTML = html;

        var rangeInput = container.querySelector('input.event-modal-range');
        if (rangeInput) {
            var self = this;
            this._scrubberInput = rangeInput;
            this._updateScrubberFill(rangeInput);

            rangeInput.addEventListener('input', function () {
                var idx = parseIntSafe(rangeInput.value, 0);
                if (self.playing) {
                    self._stopPlayback();
                    self.playing = false;
                    self._updatePlayPauseUI();
                    self._updateNavButtonsState();
                    self._updateScrubberPlayingState();
                }
                self.showFrame(idx);
                self._updateScrubberFill(rangeInput);
            });
        }
    };

    Player.prototype._buildMarkerSegments = function (nFrames) {
        if (nFrames <= 1) return '';
        var html = '';

        var preySegs = [], otherSegs = [];
        var preyStart = -1, preyEnd = -1, preyKind = '';
        var otherStart = -1, otherEnd = -1, otherOn = false;

        for (var i = 0; i < nFrames; i++) {
            var frame = this.frames[i];
            var hasPrey = false, preyStrong = false, hasOther = false;
            var preyMaxProb = null;

            if (frame.objects) {
                for (var j = 0; j < frame.objects.length; j++) {
                    var obj = frame.objects[j];
                    var nameL = (obj.name || '').toLowerCase();
                    if (nameL === 'false-accept') continue;
                    if (nameL === 'prey' || nameL === 'beute') {
                        var p = parseFloat(obj.prob) || 0;
                        preyMaxProb = (preyMaxProb === null) ? p : Math.max(preyMaxProb, p);
                    } else if (nameL) {
                        hasOther = true;
                    }
                }
            }
            hasPrey = (preyMaxProb !== null);
            preyStrong = hasPrey && (preyMaxProb >= this.mouseThreshold);

            var thisPreyKind = preyStrong ? 'prey-strong' : (hasPrey ? 'prey-soft' : '');

            if (!thisPreyKind) {
                if (preyKind) { preySegs.push([preyStart, preyEnd, preyKind]); preyKind = ''; }
            } else {
                if (thisPreyKind === preyKind && i === preyEnd + 1) { preyEnd = i; }
                else { if (preyKind) preySegs.push([preyStart, preyEnd, preyKind]); preyKind = thisPreyKind; preyStart = i; preyEnd = i; }
            }

            if (!hasOther) {
                if (otherOn) { otherSegs.push([otherStart, otherEnd, 'other']); otherOn = false; }
            } else {
                if (otherOn && i === otherEnd + 1) { otherEnd = i; }
                else { if (otherOn) otherSegs.push([otherStart, otherEnd, 'other']); otherOn = true; otherStart = i; otherEnd = i; }
            }
        }
        if (preyKind) preySegs.push([preyStart, preyEnd, preyKind]);
        if (otherOn) otherSegs.push([otherStart, otherEnd, 'other']);

        function pct(idx) { return (idx / Math.max(1, nFrames - 1)) * 100; }

        function segHtml(seg, lane) {
            var left = pct(seg[0]);
            var right = pct(seg[1]);
            var w = Math.max(0, right - left);
            var single = (seg[0] === seg[1]) ? ' is-single' : '';
            return '<div class="event-scrubber-seg lane-' + lane + ' is-' + seg[2] + single + '" '
                + 'style="left:calc(' + left.toFixed(4) + '% - 1px);width:calc(' + w.toFixed(4) + '% + 2px);"></div>';
        }

        for (var pi = 0; pi < preySegs.length; pi++) html += segHtml(preySegs[pi], 'prey');
        for (var oi = 0; oi < otherSegs.length; oi++) html += segHtml(otherSegs[oi], 'other');
        return html;
    };

    Player.prototype._updateScrubber = function (idx) {
        if (this._scrubberInput) {
            this._scrubberInput.value = String(idx);
            this._updateScrubberFill(this._scrubberInput);
        }
    };

    Player.prototype._updateScrubberFill = function (input) {
        try {
            var pct = ((input.value - input.min) / (input.max - input.min)) * 100;
            var fill = 'var(--kh-scrubber-fill, var(--bs-primary, #0d6efd))';
            var track = 'var(--kh-scrubber-track, rgba(0,0,0,0.18))';
            input.style.background = 'linear-gradient(to right, ' + fill + ' ' + pct + '%, ' + track + ' ' + pct + '%)';
        } catch (e) {}
    };

    // ---- Playback control ----------------------------------------------

    Player.prototype._startPlayback = function () {
        this._stopPlayback();
        var intervalMs = Math.max(10, Math.round(1000 / Math.max(0.1, Math.min(30, this.fps))));
        var self = this;
        this.playTimer = setInterval(function () {
            if (self.destroyed || !self.playing) { self._stopPlayback(); return; }
            var next = (self.currentIdx + 1) % self.frames.length;
            self.showFrame(next);
        }, intervalMs);
    };

    Player.prototype._stopPlayback = function () {
        if (this.playTimer) { clearInterval(this.playTimer); this.playTimer = null; }
    };

    Player.prototype.togglePlayPause = function () {
        if (this.frames.length <= 1) return;
        this.playing = !this.playing;
        if (this.playing) {
            this._startPlayback();
        } else {
            this._stopPlayback();
        }
        this._updatePlayPauseUI();
        this._updateNavButtonsState();
        this._updateScrubberPlayingState();
    };

    Player.prototype.prevFrame = function () {
        if (this.playing || this.frames.length <= 1) return;
        var idx = (this.currentIdx - 1 + this.frames.length) % this.frames.length;
        this.showFrame(idx);
    };

    Player.prototype.nextFrame = function () {
        if (this.playing || this.frames.length <= 1) return;
        var idx = (this.currentIdx + 1) % this.frames.length;
        this.showFrame(idx);
    };

    Player.prototype.toggleOverlay = function () {
        if (this.fallbackMode) return;
        this.overlayOn = !this.overlayOn;
        this._updateOverlayToggleUI();
        if (this.currentIdx >= 0 && this.currentIdx < this.frames.length) {
            this._renderOverlay(this.frames[this.currentIdx], this.currentIdx);
        }
    };

    // ---- UI state updates ----------------------------------------------

    Player.prototype._updatePlayPauseUI = function () {
        var btn = this.root.closest('.modal-content');
        if (!btn) btn = this.root.closest('.modal');
        if (!btn) btn = document;
        var ppBtn = btn.querySelector('[data-action="play-pause"]');
        if (!ppBtn) return;
        var iconPlay = ppBtn.querySelector('.kh-icon-play');
        var iconPause = ppBtn.querySelector('.kh-icon-pause');
        if (iconPlay) iconPlay.style.display = this.playing ? 'none' : '';
        if (iconPause) iconPause.style.display = this.playing ? '' : 'none';
    };

    Player.prototype._updateNavButtonsState = function () {
        var scope = this.root.closest('.modal-content') || this.root.closest('.modal') || document;
        var prevBtn = scope.querySelector('[data-action="prev"]');
        var nextBtn = scope.querySelector('[data-action="next"]');
        if (prevBtn) {
            prevBtn.disabled = this.playing;
            prevBtn.style.opacity = this.playing ? '0.5' : '';
        }
        if (nextBtn) {
            nextBtn.disabled = this.playing;
            nextBtn.style.opacity = this.playing ? '0.5' : '';
        }
        this._updateDownloadSingleState();
    };

    Player.prototype._updateDownloadSingleState = function () {
        try {
            var scope = this.root.closest('.modal-content') || document;
            var dlBtn = scope.querySelector('.event-modal-toolbar-bottom-left a, .event-modal-toolbar-bottom-left button');
            if (dlBtn) {
                if (this.playing) {
                    dlBtn.classList.add('disabled');
                    dlBtn.style.opacity = '0.5';
                    dlBtn.style.pointerEvents = 'none';
                } else {
                    dlBtn.classList.remove('disabled');
                    dlBtn.style.opacity = '';
                    dlBtn.style.pointerEvents = '';
                }
            }
        } catch (e) {}
    };

    Player.prototype._updateOverlayToggleUI = function () {
        var scope = this.root.closest('.modal-content') || this.root.closest('.modal') || document;
        var btn = scope.querySelector('[data-action="toggle-overlay"]');
        if (!btn) return;
        var iconOn = btn.querySelector('.kh-icon-overlay-on');
        var iconOff = btn.querySelector('.kh-icon-overlay-off');
        if (iconOn) iconOn.style.display = this.overlayOn ? '' : 'none';
        if (iconOff) iconOff.style.display = this.overlayOn ? 'none' : '';
    };

    Player.prototype._updateScrubberPlayingState = function () {
        var wrap = document.getElementById('event_scrubber_wrap');
        if (wrap) wrap.setAttribute('data-playing', this.playing ? '1' : '0');
    };

    // ---- Controls attachment -------------------------------------------

    Player.prototype._attachControls = function () {
        var self = this;
        var modal = this.root.closest('.modal-content') || this.root.closest('.modal') || document;

        this._boundModalClick = function (e) {
            if (self.destroyed) return;
            var target = e.target.closest('[data-action]');
            if (!target) return;
            var action = target.getAttribute('data-action');
            switch (action) {
                case 'play-pause': self.togglePlayPause(); break;
                case 'prev': self.prevFrame(); break;
                case 'next': self.nextFrame(); break;
                case 'toggle-overlay': self.toggleOverlay(); break;
            }
        };
        modal.addEventListener('click', this._boundModalClick);

        // Signal current frame index before Shiny processes download click
        try {
            var dlSingle = modal.querySelector('a[id$="btn_download_single"]');
            if (dlSingle) {
                dlSingle.addEventListener('click', function () {
                    self._signalFrameIdx(self.currentIdx);
                }, true);
            }
        } catch (e) {}
    };

    Player.prototype._attachKeyboardHandler = function () {
        var self = this;
        this._boundKeyDown = function (e) {
            if (self.destroyed) return;
            var modal = document.querySelector('.modal.show');
            if (!modal) return;
            var tag = (document.activeElement && document.activeElement.tagName) ? document.activeElement.tagName.toLowerCase() : '';
            if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

            switch (e.key) {
                case ' ':
                case 'k':
                    e.preventDefault();
                    self.togglePlayPause();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    if (self.playing) { self.playing = false; self._stopPlayback(); self._updatePlayPauseUI(); self._updateNavButtonsState(); self._updateScrubberPlayingState(); }
                    self.prevFrame();
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    if (self.playing) { self.playing = false; self._stopPlayback(); self._updatePlayPauseUI(); self._updateNavButtonsState(); self._updateScrubberPlayingState(); }
                    self.nextFrame();
                    break;
            }
        };
        document.addEventListener('keydown', this._boundKeyDown);
    };

    Player.prototype._attachModalCloseHandler = function () {
        var self = this;
        var modalEl = this.root.closest('.modal');
        if (modalEl) {
            modalEl.addEventListener('hidden.bs.modal', function () {
                self.destroy();
                try {
                    var btn = document.querySelector('button[id$="modal_pulse"]');
                    if (btn) btn.click();
                } catch (e) {}
            }, { once: true });
        }
    };

    // ---- Prefetch adjacent ---------------------------------------------

    Player.prototype._prefetchAdjacent = function (idx) {
        try {
            if (this.frames.length <= 1) return;
            var prevIdx = (idx - 1 + this.frames.length) % this.frames.length;
            var nextIdx = (idx + 1) % this.frames.length;
            var prevSrc = this._resolveFrameSrc(this.frames[prevIdx].pid);
            var nextSrc = this._resolveFrameSrc(this.frames[nextIdx].pid);
            if (prevSrc) prefetchSrc(prevSrc);
            if (nextSrc) prefetchSrc(nextSrc);
        } catch (e) {}
    };

    // ---- Shiny communication -------------------------------------------

    Player.prototype._signalFrameIdx = function (idx) {
        try {
            if (this.nsFrameIdx && typeof Shiny !== 'undefined' && Shiny.setInputValue) {
                Shiny.setInputValue(this.nsFrameIdx, idx);
            }
        } catch (e) {}
    };

    // ---- Empty state ---------------------------------------------------

    Player.prototype._showEmptyState = function () {
        if (this.overlayContainer) {
            this.overlayContainer.innerHTML =
                '<div class="placeholder-image" style="display:flex;align-items:center;justify-content:center;min-height:200px;">'
                + '<strong>No pictures found for this event.</strong></div>';
        }
    };

    // ---- Cleanup -------------------------------------------------------

    Player.prototype.destroy = function () {
        this.destroyed = true;
        this._stopPlayback();
        if (this._boundKeyDown) {
            document.removeEventListener('keydown', this._boundKeyDown);
            this._boundKeyDown = null;
        }
        if (this._boundModalClick) {
            var modal = this.root.closest('.modal-content') || this.root.closest('.modal') || document;
            modal.removeEventListener('click', this._boundModalClick);
            this._boundModalClick = null;
        }
    };

    // =====================================================================
    // Section 7 – Modal pulse observer (Bootstrap modal-static detection)
    // =====================================================================
    function initModalStaticPulseObserver() {
        if (window.__khModalStaticObserver) return;
        window.__khModalStaticObserver = true;

        function setupPulseObserver() {
            var modal = document.querySelector('.modal');
            var btn = document.querySelector('button[id$="modal_pulse"]');
            if (!modal || !btn) { setTimeout(setupPulseObserver, 100); return; }
            try {
                var observer = new MutationObserver(function (mutations) {
                    mutations.forEach(function (mutation) {
                        if (mutation.type === "attributes" && mutation.attributeName === "class" &&
                            mutation.target.classList.contains("modal-static") &&
                            (!mutation.oldValue || !mutation.oldValue.includes("modal-static"))) {
                            try { btn.click(); } catch (e) {}
                        }
                    });
                });
                observer.observe(modal, { attributes: true, attributeOldValue: true });
            } catch (e) {}
        }
        setupPulseObserver();
    }

    // =====================================================================
    // Section 8 – Initialisation / polling
    // =====================================================================
    function tryInitPlayer() {
        var root = document.getElementById('event_modal_root');
        if (!root) return;
        if (root.getAttribute('data-player-init')) return;
        root.setAttribute('data-player-init', '1');

        if (_player) { _player.destroy(); _player = null; }

        _player = new Player(root);
        _player.init();
    }

    function pollLoop() {
        try { tryInitPlayer(); } catch (e) {}
        try { initModalStaticPulseObserver(); } catch (e) {}
        setTimeout(pollLoop, document.querySelector('.modal') ? 200 : 800);
    }

    window.kittyhackEventModal = window.kittyhackEventModal || {};
    window.kittyhackEventModal.init = function () {
        try { tryInitPlayer(); } catch (e) {}
        try { initModalStaticPulseObserver(); } catch (e) {}
    };

    try {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () { pollLoop(); }, { once: true });
        } else {
            setTimeout(pollLoop, 0);
        }
    } catch (e) {}
})();
