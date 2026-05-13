const express = require("express");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const app = express();
app.use(express.json({ limit: "10mb" }));

let lastQr = null;
let ready = false;
let lastAuthAt = null;
let lastDisconnect = null;
let contactsCache = null;
let contactsCacheAt = null;
let contactsRefreshing = false;

const client = new Client({
  authStrategy: new LocalAuth({ clientId: "codexia" }),
  puppeteer: {
    headless: true,
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-first-run",
      "--no-zygote",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-default-apps",
      "--disable-sync",
      "--disable-translate"
    ]
  }
});

client.on("qr", (qr) => {
  lastQr = String(qr || "");
  ready = false;
  lastDisconnect = null;
  console.log("WA: qr_received");
});

client.on("authenticated", () => {
  lastAuthAt = new Date().toISOString();
  console.log("WA: authenticated");
});

client.on("ready", () => {
  ready = true;
  lastQr = null;
  lastDisconnect = null;
  console.log("WA: ready");
  setTimeout(() => {
    try {
      refreshContactsCache();
    } catch (_) {}
  }, 1500);
});

client.on("auth_failure", () => {
  ready = false;
  console.log("WA: auth_failure");
});

client.on("disconnected", (reason) => {
  ready = false;
  lastDisconnect = { at: new Date().toISOString(), reason: String(reason || "") };
  console.log("WA: disconnected", reason || "");
});

function normalizeToChatId(raw) {
  const digits = String(raw || "").replace(/\D+/g, "");
  if (!digits) return null;
  return `${digits}@c.us`;
}

async function getStateSafe() {
  try {
    const s = await client.getState();
    return s ? String(s) : null;
  } catch (_) {
    return null;
  }
}

function sanitizeContacts(contacts) {
  const out = [];
  for (const c of contacts || []) {
    try {
      if (!c || !c.id || !c.id._serialized) continue;
      const isUser = c.isUser === true;
      const isGroup = c.isGroup === true;
      if (!isUser || isGroup) continue;
      const serialized = String(c.id._serialized);
      const number = serialized.endsWith("@c.us") ? serialized.replace("@c.us", "") : serialized;
      const name = String(c.pushname || c.name || number);
      out.push({ id: serialized, number, name });
    } catch (_) {}
  }
  out.sort((a, b) => String(a.name).localeCompare(String(b.name)));
  return out;
}

async function refreshContactsCache() {
  if (contactsRefreshing) return;
  contactsRefreshing = true;
  try {
    const contacts = await client.getContacts();
    contactsCache = sanitizeContacts(contacts);
    contactsCacheAt = new Date().toISOString();
  } finally {
    contactsRefreshing = false;
  }
}

app.get("/status", async (_req, res) => {
  const state = await getStateSafe();
  const isConnected = state && String(state).toUpperCase() === "CONNECTED";
  res.json({
    ready: !!ready || !!isConnected,
    state: state || "",
    has_qr: !!(lastQr && !ready && !isConnected),
    last_auth_at: lastAuthAt || "",
    last_disconnect: lastDisconnect || null
  });
});

app.get("/qr", async (_req, res) => {
  if (ready) {
    res.json({ ready: true, data_url: "" });
    return;
  }
  if (!lastQr) {
    res.status(503).json({ error: "QR não disponível ainda. Aguarde alguns segundos." });
    return;
  }
  try {
    const dataUrl = await QRCode.toDataURL(lastQr, { margin: 1, scale: 8 });
    res.json({ ready: false, data_url: dataUrl });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

app.post("/logout", async (_req, res) => {
  lastQr = null;
  ready = false;
  lastDisconnect = { at: new Date().toISOString(), reason: "manual_logout" };
  contactsCache = null;
  contactsCacheAt = null;
  res.json({ status: "ok" });

  const withTimeout = async (p, ms) => {
    return await Promise.race([
      p,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))
    ]);
  };

  (async () => {
    try {
      try {
        await withTimeout(client.logout(), 4000);
      } catch (_) {}
      try {
        await withTimeout(client.destroy(), 4000);
      } catch (_) {}
      setTimeout(() => {
        try {
          client.initialize();
        } catch (_) {}
      }, 500);
    } catch (_) {}
  })();
});

app.get("/contacts", async (_req, res) => {
  const state = await getStateSafe();
  const isConnected = state && String(state).toUpperCase() === "CONNECTED";
  if (!ready && !isConnected) {
    res.status(503).json({ error: "WhatsApp não conectado. Escaneie o QR Code." });
    return;
  }
  const forceRefresh = String((_req.query && _req.query.refresh) || "").trim() === "1";
  const now = Date.now();
  const cacheAgeMs = contactsCacheAt ? Math.max(0, now - Date.parse(contactsCacheAt)) : null;
  const cacheFresh = cacheAgeMs !== null && cacheAgeMs < 10 * 60 * 1000;
  if (!forceRefresh && contactsCache && cacheFresh) {
    res.json({ contacts: contactsCache, cached: true, cached_at: contactsCacheAt || "" });
    return;
  }
  if (!forceRefresh && contactsCache) {
    try {
      refreshContactsCache();
    } catch (_) {}
    res.json({ contacts: contactsCache, cached: true, cached_at: contactsCacheAt || "" });
    return;
  }
  try {
    refreshContactsCache();
  } catch (_) {}
  res.json({
    contacts: [],
    loading: true,
    cached: false,
    cached_at: contactsCacheAt || ""
  });
});

app.post("/send", async (req, res) => {
  const state = await getStateSafe();
  const isConnected = state && String(state).toUpperCase() === "CONNECTED";
  if (!ready && !isConnected) {
    res.status(503).json({ error: "WhatsApp não conectado. Escaneie o QR Code." });
    return;
  }
  let toRaw = (req.body && req.body.to) ? String(req.body.to) : "";
  let to = null;
  if (toRaw.includes("@c.us")) {
    to = toRaw;
  } else {
    to = normalizeToChatId(toRaw);
  }
  const message = String((req.body && req.body.message) || "").trim();
  const mediaPath = (req.body && req.body.media_path) ? String(req.body.media_path) : null;
  if (!to) {
    res.status(400).json({ error: "Destinatário inválido." });
    return;
  }
  if (!message && !mediaPath) {
    res.status(400).json({ error: "Mensagem vazia." });
    return;
  }
  try {
    if (mediaPath) {
      const media = MessageMedia.fromFilePath(mediaPath);
      const r = await client.sendMessage(to, media, message ? { caption: message } : undefined);
      res.json({ status: "sent", id: r && r.id ? r.id._serialized : null });
      return;
    }
    const r = await client.sendMessage(to, message);
    res.json({ status: "sent", id: r && r.id ? r.id._serialized : null });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

const port = Number(process.env.PORT || 3030);
client.initialize();
app.listen(port, "0.0.0.0", () => {
  console.log(`WhatsApp Bridge listening on :${port}`);
});
