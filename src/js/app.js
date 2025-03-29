document.addEventListener("DOMContentLoaded", function() {
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
});
