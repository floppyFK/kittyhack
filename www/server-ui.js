// Kittyhack - UI helpers extracted from src/server.py inline scripts
// Served from www/ via app.py static_assets mapping.

(function () {
    "use strict";

    function q(sel, root) {
        try { return (root || document).querySelector(sel); } catch (e) { return null; }
    }

    function qa(sel, root) {
        try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; }
    }

    function closest(el, sel) {
        try { return el && el.closest ? el.closest(sel) : null; } catch (e) { return null; }
    }

    function onceFlag(obj, key) {
        try {
            if (!obj) return false;
            if (obj[key]) return false;
            obj[key] = true;
            return true;
        } catch (e) {
            return true;
        }
    }

    function initTooltipWrappers(scope) {
        // For event tables rendered as raw HTML: .tooltip-wrapper[title]
        // Initialize Bootstrap tooltips without requiring jQuery.
        if (!(window.bootstrap && window.bootstrap.Tooltip)) return;
        var root = scope || document;
        var els = qa('.tooltip-wrapper[title]', root);
        els.forEach(function (el) {
            try {
                if (el.getAttribute('data-kh-tooltip-init') === '1') return;
                // Bootstrap expects either title attribute or data-bs-title.
                window.bootstrap.Tooltip.getOrCreateInstance(el);
                el.setAttribute('data-kh-tooltip-init', '1');
            } catch (e) {
                // ignore per element
            }
        });
    }

    function initCollapseAriaExpanded() {
        if (!onceFlag(window, '__khCollapseAriaExpanded')) return;
        // Keep aria-expanded in sync for buttons that control collapse targets.
        document.addEventListener('shown.bs.collapse', function (e) {
            try {
                var btn = document.querySelector('[data-bs-target="#' + e.target.id + '"]');
                if (btn) btn.setAttribute('aria-expanded', 'true');
            } catch (err) {}
        });
        document.addEventListener('hidden.bs.collapse', function (e) {
            try {
                var btn = document.querySelector('[data-bs-target="#' + e.target.id + '"]');
                if (btn) btn.setAttribute('aria-expanded', 'false');
            } catch (err) {}
        });
    }

    function initUnsavedChangesHighlighter(opts) {
        // Generic unsaved-change highlighter using event delegation.
        // opts: { containerId, saveButtonId, initDelayMs, sliderSelector }
        var container = q('#' + (opts.containerId || ''));
        var saveBtn = q('#' + (opts.saveButtonId || ''));
        if (!container || !saveBtn) return;

        if (container.getAttribute('data-kh-unsaved-init') === '1') return;
        container.setAttribute('data-kh-unsaved-init', '1');

        var isInitializing = true;
        var initDelay = (typeof opts.initDelayMs === 'number') ? opts.initDelayMs : 1000;
        setTimeout(function () { isInitializing = false; }, initDelay);

        function markUnsaved(el) {
            if (isInitializing) return;
            try { el.classList.add('unsaved-input'); } catch (e) {}
            try { saveBtn.classList.add('save-button-highlight'); } catch (e) {}
        }

        function markSliderUnsaved(inputEl) {
            if (isInitializing) return;
            if (!inputEl || !inputEl.id) return;
            var group = closest(inputEl, '.form-group') || closest(inputEl, '.shiny-input-container') || inputEl.parentElement;
            if (!group) return;
            // Ion.RangeSlider markup uses these classes.
            qa('.irs-single, .irs-bar, .irs-handle', group).forEach(function (n) {
                try { n.classList.add('unsaved-input'); } catch (e) {}
            });
            try { saveBtn.classList.add('save-button-highlight'); } catch (e) {}
        }

        // Input/change delegation
        container.addEventListener('change', function (ev) {
            var t = ev.target;
            if (!t) return;
            // Range slider hidden input
            if (t.matches && t.matches(opts.sliderSelector || 'input.js-range-slider')) {
                markSliderUnsaved(t);
                return;
            }
            if (t.matches && (t.matches('input') || t.matches('select') || t.matches('textarea'))) {
                markUnsaved(t);
            }
        }, true);

        container.addEventListener('input', function (ev) {
            var t = ev.target;
            if (!t) return;
            if (t.matches && t.matches(opts.sliderSelector || 'input.js-range-slider')) {
                markSliderUnsaved(t);
                return;
            }
            if (t.matches && (t.matches('input[type="text"]') || t.matches('input[type="password"]') || t.matches('textarea'))) {
                markUnsaved(t);
            }
        }, true);

        // Also detect slider drag end (mousedown/touchstart on handle -> one-shot mouseup/touchend)
        container.addEventListener('mousedown', function (ev) {
            var handle = closest(ev.target, '.irs-handle');
            if (!handle) return;
            var group = closest(handle, '.form-group') || closest(handle, '.shiny-input-container') || handle.parentElement;
            var sliderInput = group ? q('input.js-range-slider', group) : null;
            if (!sliderInput) return;
            var done = false;
            function onUp() {
                if (done) return;
                done = true;
                markSliderUnsaved(sliderInput);
                document.removeEventListener('mouseup', onUp, true);
                document.removeEventListener('touchend', onUp, true);
            }
            document.addEventListener('mouseup', onUp, true);
            document.addEventListener('touchend', onUp, true);
        }, true);

        // Reset on save click
        saveBtn.addEventListener('click', function () {
            try {
                qa('.unsaved-input', container).forEach(function (n) { n.classList.remove('unsaved-input'); });
                saveBtn.classList.remove('save-button-highlight');
            } catch (e) {}
        }, true);
    }

    function initIpCameraUrlToggle() {
        // Toggle IP camera specific inputs visibility based on #camera_source
        // Expects containers: #ip_camera_url_container, #ip_camera_warning, #ip_camera_pipeline_settings
        var sel = q('#camera_source');
        var urlWrap = q('#ip_camera_url_container');
        var warn = q('#ip_camera_warning');
        var pipelineWrap = q('#ip_camera_pipeline_settings');
        if (!sel || (!urlWrap && !warn && !pipelineWrap)) return;

        if (sel.getAttribute('data-kh-ipcam-bound') === '1') return;
        sel.setAttribute('data-kh-ipcam-bound', '1');

        function apply() {
            var isIp = false;
            try { isIp = (String(sel.value) === 'ip_camera'); } catch (e) {}
            if (urlWrap) urlWrap.style.display = isIp ? '' : 'none';
            if (warn) warn.style.display = isIp ? '' : 'none';
            if (pipelineWrap) pipelineWrap.style.display = isIp ? '' : 'none';
        }

        apply();
        sel.addEventListener('change', apply, true);
    }

    function initLogicToggles() {
        // Handles "Show decision logic" blocks for entry and exit.
        if (!onceFlag(window, '__khLogicToggleGlobal')) return;

        function toggleSection(btnId, sectionId, hintId) {
            var btn = document.getElementById(btnId);
            var section = document.getElementById(sectionId);
            var hint = document.getElementById(hintId);
            if (!btn || !section) return;
            var span = btn.querySelector('span');
            var showLbl = btn.getAttribute('data-show-label') || '';
            var hideLbl = btn.getAttribute('data-hide-label') || '';
            var isHidden = (section.style.display === 'none' || section.style.display === '');
            section.style.display = isHidden ? 'block' : 'none';
            if (hint) hint.style.display = isHidden ? 'block' : 'none';
            if (span) span.textContent = isHidden ? hideLbl : showLbl;
        }

        function showLogic(containerId, mode) {
            var container = document.getElementById(containerId);
            if (!container) return;
            qa('.logic-img-wrapper', container).forEach(function (el) {
                try {
                    el.style.display = (el.getAttribute('data-mode') === mode) ? 'block' : 'none';
                } catch (e) {}
            });
        }

        document.addEventListener('click', function (e) {
            var t = e.target;
            if (!t) return;

            if (t.id === 'btn_toggle_entry_logic' || closest(t, '#btn_toggle_entry_logic')) {
                toggleSection('btn_toggle_entry_logic', 'entry_logic_expand', 'entry_logic_hint');
                var sel = document.getElementById('txtAllowedToEnter');
                if (sel) showLogic('entry_logic_images', sel.value);
            }

            if (t.id === 'btn_toggle_exit_logic' || closest(t, '#btn_toggle_exit_logic')) {
                toggleSection('btn_toggle_exit_logic', 'exit_logic_expand', 'exit_logic_hint');
                var sel2 = document.getElementById('btnAllowedToExit');
                if (sel2) showLogic('exit_logic_images', sel2.value);
            }
        }, true);

        document.addEventListener('change', function (e) {
            var t = e.target;
            if (!t) return;
            if (t.id === 'txtAllowedToEnter') {
                showLogic('entry_logic_images', t.value);
            }
            if (t.id === 'btnAllowedToExit') {
                showLogic('exit_logic_images', t.value);
            }
        }, true);
    }

    function initManageCatsRFIDValidation() {
        // Validates all inputs with id prefix mng_cat_rfid_ and writes status into mng_cat_rfid_status_<id>
        var container = q('#manage_cats_container');
        if (!container) return;

        var i18n = q('#kh_manage_cats_i18n');
        var msgEmpty = i18n ? (i18n.getAttribute('data-msg-empty') || '') : '';
        var msgValid = i18n ? (i18n.getAttribute('data-msg-valid') || 'Valid RFID') : 'Valid RFID';
        var msgInvalid = i18n ? (i18n.getAttribute('data-msg-invalid') || 'Invalid RFID') : 'Invalid RFID';

        function validateInput(inp) {
            if (!inp || !inp.id) return;
            var id = String(inp.id).replace('mng_cat_rfid_', '');
            var box = document.getElementById('mng_cat_rfid_status_' + id);
            if (!box) return;

            var raw = (inp.value || '');
            var val = raw.trim();
            var hex = val.toUpperCase();

            if (hex === '') {
                box.textContent = msgEmpty;
                box.className = 'rfid-status rfid-empty';
                return;
            }

            if (hex.length === 16 && /^[0-9A-F]{16}$/.test(hex)) {
                box.textContent = msgValid;
                box.className = 'rfid-status rfid-valid';
            } else {
                box.textContent = msgInvalid;
                box.className = 'rfid-status rfid-invalid';
            }
        }

        // Bind each input once
        qa('input[id^="mng_cat_rfid_"]', container).forEach(function (inp) {
            try {
                if (inp.getAttribute('data-kh-rfid-bound') === '1') return;
                inp.setAttribute('data-kh-rfid-bound', '1');
                ['input', 'change', 'blur'].forEach(function (evt) {
                    inp.addEventListener(evt, function () { validateInput(inp); }, true);
                });
                validateInput(inp);
            } catch (e) {}
        });
    }

    function initUpdateProgressModal() {
        // Keeps progress bar in sync with percent text + animates dots.
        var modal = q('#update_progress_modal');
        if (!modal) return;

        if (modal.getAttribute('data-kh-update-init') === '1') return;
        modal.setAttribute('data-kh-update-init', '1');

        var percentTextEl = q('#progress_percent_text', modal);
        var barEl = q('#progress_bar', modal);
        var msgEl = q('#in_progress_dots', modal);

        function updateBar() {
            if (!percentTextEl || !barEl) return;
            var percent = String(percentTextEl.textContent || '').trim();
            if (percent.endsWith('%')) percent = percent.slice(0, -1);
            var val = parseInt(percent, 10);
            if (!isNaN(val)) barEl.style.width = val + '%';
        }

        try {
            if (window.MutationObserver && percentTextEl) {
                var observer = new MutationObserver(updateBar);
                observer.observe(percentTextEl, { childList: true, subtree: true });
                updateBar();
            } else {
                updateBar();
            }
        } catch (e) {
            updateBar();
        }

        // Dots animation; stop when modal is removed
        var dots = 0;
        var intervalId = setInterval(function () {
            if (!document.body.contains(modal)) {
                clearInterval(intervalId);
                return;
            }
            if (msgEl) {
                dots = (dots + 1) % 4;
                msgEl.textContent = '.'.repeat(dots);
            }
        }, 700);
    }

    function initAll(scope) {
        initCollapseAriaExpanded();
        initTooltipWrappers(scope);
        // These run opportunistically when the corresponding UI is present.
        initIpCameraUrlToggle();
        initLogicToggles();
        initManageCatsRFIDValidation();
        initUpdateProgressModal();

        // Unsaved change markers
        initUnsavedChangesHighlighter({ containerId: 'config_tab_container', saveButtonId: 'bSaveKittyhackConfig', initDelayMs: 1000, sliderSelector: 'input.js-range-slider' });
        initUnsavedChangesHighlighter({ containerId: 'manage_cats_container', saveButtonId: 'mng_cat_save_changes', initDelayMs: 800 });
    }

    function installMutationInit() {
        if (!onceFlag(window, '__khServerUiMutationInit')) return;
        // Run once after DOM ready, and again when Shiny re-renders outputs.
        function run() { try { initAll(document); } catch (e) {} }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', run, { once: true });
        } else {
            setTimeout(run, 0);
        }

        try {
            var scheduled = false;
            var obs = new MutationObserver(function () {
                if (scheduled) return;
                scheduled = true;
                setTimeout(function () {
                    scheduled = false;
                    run();
                }, 0);
            });
            obs.observe(document.body, { childList: true, subtree: true });
        } catch (e) {
            // ignore
        }
    }

    window.kittyhackServerUI = window.kittyhackServerUI || {};
    window.kittyhackServerUI.initAll = initAll;

    // Auto-install
    installMutationInit();
})();
