// Изолированный content-скрипт: пересылает снапшоты из MAIN-мира (page-hook.js)
// в service worker расширения.
(() => {
    "use strict";
    window.addEventListener("message", (e) => {
        if (e.source !== window || !e.data || e.data.source !== "jsl-hook") return;
        try {
            chrome.runtime.sendMessage({
                kind: "jsl-snapshot",
                ts: e.data.ts,
                payload: e.data.payload
            }).catch(() => { /* SW перезапускается — снапшот придёт снова */ });
        } catch (err) {
            // «Extension context invalidated» после обновления расширения — не страшно.
        }
    });
})();
