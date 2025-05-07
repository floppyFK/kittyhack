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
            allowedToExitRanges.style.display = btnAllowedToExit.checked ? "block" : "none";
            btnAllowedToExit.addEventListener("change", function() {
                allowedToExitRanges.style.display = btnAllowedToExit.checked ? "block" : "none";
            });
        }
    }

    // --- Add functionality to reload on shiny-disconnected-overlay ---
    (function() {
        let reloadInterval = null;
        let reloadedOnce = false;

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
    })();
});
