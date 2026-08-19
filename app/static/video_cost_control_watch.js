(function () {
    'use strict';
    function tryInstall() {
        try {
            if (window.CodexiaVideoCost && typeof window.CodexiaVideoCost.installPanel === 'function') {
                window.CodexiaVideoCost.installPanel();
            }
        } catch (_) {}
    }
    const target = document.getElementById('app') || document.body;
    if (target && typeof MutationObserver !== 'undefined') {
        const observer = new MutationObserver(tryInstall);
        observer.observe(target, { childList: true, subtree: true });
    }
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) tryInstall();
    });
    window.addEventListener('focus', tryInstall);
    tryInstall();
})();
