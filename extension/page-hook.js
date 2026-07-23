// Работает в MAIN-мире страницы Jitsi Meet: раз в секунду снимает состояние
// конференции через внутренний глобал APP (как это делают официальные тесты
// jitsi-meet-torture) и передаёт снапшот изолированному content-скрипту
// (bridge.js) через window.postMessage.
//
// Если структура APP недоступна (сильно изменённая сборка Jitsi) — деградация
// до DOM-эвристики: joined определяется по наличию тулбара конференции,
// список участников недоступен (null).
(() => {
    "use strict";
    const POLL_MS = 1000;      // период опроса состояния
    const HEARTBEAT_MS = 5000; // принудительная отправка (heartbeat), даже без изменений

    let lastJson = "";
    let lastSentAt = 0;

    function participantsFromState(state) {
        try {
            const p = state["features/base/participants"];
            if (!p) return null;
            const out = [];
            if (p.local && p.local.id) {
                out.push({ id: String(p.local.id), name: p.local.name || "", local: true });
            }
            const rem = p.remote;
            if (rem instanceof Map) {
                for (const v of rem.values()) {
                    if (v && v.id) out.push({ id: String(v.id), name: v.name || "", local: false });
                }
            } else if (rem && typeof rem === "object") {
                for (const v of Object.values(rem)) {
                    if (v && v.id) out.push({ id: String(v.id), name: v.name || "", local: false });
                }
            }
            return out;
        } catch (e) {
            return null;
        }
    }

    function roomFromUrl() {
        try {
            const seg = location.pathname.split("/").filter(Boolean).pop() || "";
            return decodeURIComponent(seg);
        } catch (e) {
            return "";
        }
    }

    function snapshot() {
        let joined = false;
        let room = "";
        let participants = null;
        let audioMuted = null;
        let via = "dom";
        try {
            const APP = window.APP;
            if (APP && APP.conference && typeof APP.conference.isJoined === "function") {
                via = "app";
                joined = !!APP.conference.isJoined();
                room = String(APP.conference.roomName || "");
                if (APP.store && typeof APP.store.getState === "function") {
                    const st = APP.store.getState();
                    participants = participantsFromState(st);
                    const media = st["features/base/media"];
                    if (media && media.audio && typeof media.audio.muted !== "undefined") {
                        audioMuted = !!media.audio.muted;
                    }
                }
            } else {
                joined = !!document.querySelector("#new-toolbox") &&
                    !document.querySelector(".premeeting-screen");
            }
        } catch (e) { /* остаёмся на значениях по умолчанию */ }
        if (!room) room = roomFromUrl();
        return {
            joined: joined,
            room: room,
            participants: participants,
            audioMuted: audioMuted,
            via: via,
            title: document.title || "",
            url: location.origin + location.pathname
        };
    }

    setInterval(() => {
        const s = snapshot();
        const j = JSON.stringify(s);
        const now = Date.now();
        if (j !== lastJson || now - lastSentAt >= HEARTBEAT_MS) {
            lastJson = j;
            lastSentAt = now;
            window.postMessage({ source: "jsl-hook", ts: now, payload: s }, location.origin);
        }
    }, POLL_MS);
})();
