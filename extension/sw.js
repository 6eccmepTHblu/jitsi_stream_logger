// Service worker: держит WebSocket к локальному приложению (127.0.0.1) и
// пересылает ему снапшоты вкладок с Jitsi. Пока открыта вкладка созвона,
// снапшоты приходят каждые ~5 секунд и не дают SW заснуть; сам WebSocket
// с трафиком тоже продлевает жизнь SW (Chrome 116+).
"use strict";

const WS_URL = "ws://127.0.0.1:8765"; // порт должен совпадать с ws_port в config.toml

let ws = null;
let reconnectTimer = null;
let backoffMs = 1000;
const tabs = new Map();   // tabId -> последний payload
let queue = [];           // сообщения, не отправленные из-за отсутствия связи

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }
    try {
        ws = new WebSocket(WS_URL);
    } catch (e) {
        scheduleReconnect();
        return;
    }
    ws.onopen = () => {
        backoffMs = 1000;
        // Приложение могло перезапуститься — прогоняем актуальное состояние вкладок.
        for (const [tabId, payload] of tabs) {
            rawSend({ type: "snapshot", tab_id: tabId, ts: Date.now(), resync: true, ...payload });
        }
        const q = queue;
        queue = [];
        q.forEach(rawSend);
    };
    ws.onclose = () => scheduleReconnect();
    ws.onerror = () => { try { ws.close(); } catch (e) { } };
}

function scheduleReconnect() {
    if (reconnectTimer !== null) return;
    // Не переподключаемся, когда сообщать нечего (нет вкладок с Jitsi и очередь
    // пуста): каждая неудачная попытка попадает в панель ошибок расширения.
    if (tabs.size === 0 && queue.length === 0) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, backoffMs);
    backoffMs = Math.min(backoffMs * 2, 30000);
}

function rawSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        try {
            ws.send(JSON.stringify(obj));
            return true;
        } catch (e) { /* упадём в очередь */ }
    }
    return false;
}

function send(obj) {
    if (!rawSend(obj)) {
        queue.push(obj);
        if (queue.length > 200) queue.shift();
        connect();
    }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
    if (!msg || msg.kind !== "jsl-snapshot" || !sender.tab || sender.tab.id == null) return;
    const tabId = sender.tab.id;
    tabs.set(tabId, msg.payload);
    send({ type: "snapshot", tab_id: tabId, ts: msg.ts || Date.now(), ...msg.payload });
});

chrome.tabs.onRemoved.addListener((tabId) => {
    if (tabs.has(tabId)) {
        tabs.delete(tabId);
        send({ type: "tab_closed", tab_id: tabId, ts: Date.now() });
    }
});

// Подключение ленивое: первый снапшот от вкладки с Jitsi сам вызовет connect()
// через send(). Так расширение не сыпет ошибками, пока приложение не запущено
// и созвонов нет.
