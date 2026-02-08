// Kittyhack - Event modal helpers
// Keeps the Python server code clean by hosting modal-specific JS here.
// This file is served via app.py static_assets mapping ("/" -> www/).

(function () {
    "use strict";

    function q(sel, root) {
        try {
            return (root || document).querySelector(sel);
        } catch (e) {
            return null;
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
            var pic = document.getElementById('event_modal_picture');
            var img = q('#event_modal_picture img[data-role="visible"]');
            var pulseBtn = q('button[id$="img_loaded_pulse"]');

            if (!wrap || !pic || !img || !pulseBtn) return;

            // Mark that the dedicated event-modal observer is active (so global app.js doesn't interfere).
            try {
                wrap.setAttribute('data-event-modal-observer', '1');
            } catch (e) {}

            var src = img.getAttribute('src') || '';
            if (!src) return;

            // Detect src changes across server re-renders (DOM replacement)
            var currentSrc = wrap.getAttribute('data-current-src') || '';
            var shownSrc = wrap.getAttribute('data-shown-src') || '';

            if (currentSrc !== src) {
                wrap.setAttribute('data-current-src', src);
                // Keep old visible as background until new is loaded
                if (shownSrc) {
                    try { wrap.style.backgroundImage = 'url(' + shownSrc + ')'; } catch (e) {}
                }
                try { wrap.classList.remove('has-image'); } catch (e) {}
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
                        wrap.style.backgroundImage = '';
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
                    wrap.style.backgroundImage = '';
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
