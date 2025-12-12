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
        let isNavigatingAway = false;

        // Listen for navigation attempts
        window.addEventListener('beforeunload', function() {
            isNavigatingAway = true;
        });

        // Reset flags when page is shown from bfcache or app switch
        window.addEventListener('pageshow', function() {
            isNavigatingAway = false;
            // Attempt service worker update on HTTPS
            if (navigator.serviceWorker && navigator.serviceWorker.ready) {
                navigator.serviceWorker.ready.then(reg => {
                    try { reg.update(); } catch (e) {}
                });
            }
        });

        document.addEventListener('visibilitychange', function() {
            if (document.visibilityState === 'visible') {
                isNavigatingAway = false;
            }
        });

        function checkForDisconnectOverlay() {
            const overlay = document.getElementById("shiny-disconnected-overlay");

            if (overlay) {
                if (!reloadedOnce) {
                    reloadedOnce = true;
                    console.log("Detected disconnection overlay. Reloading...");
                    location.reload();
                }

                if (!reloadInterval) {
                    reloadInterval = setInterval(() => {
                        const stillOverlay = document.getElementById("shiny-disconnected-overlay");
                        if (stillOverlay) {
                            console.log("Still disconnected. Reloading again...");
                            location.reload();
                        } else {
                            clearInterval(reloadInterval);
                            reloadInterval = null;
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
                    console.log('Service worker controller changed, reloading...');
                    location.reload();
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
                            location.reload();
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
    function tryReconnect() {
        // If Shiny overlay exists, reload; otherwise, ping server and reload on failure.
        const overlay = document.getElementById("shiny-disconnected-overlay");
        if (overlay) {
            console.log("Reconnect: overlay present, reloading...");
            location.reload();
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
                try {
                    window.location.href = '/offline.html';
                } catch (e) {
                    // Fallback to reload if redirect fails
                    location.reload();
                }
            });
    }

    document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            tryReconnect();
        }
    });

    window.addEventListener('online', tryReconnect);
});