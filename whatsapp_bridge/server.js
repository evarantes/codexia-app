const express = require("express");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const app = express();
app.use(express.json({ limit: "10mb" }));

let lastQr = null;
let ready = false;

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
});

client.on("ready", () => {
  ready = true;
});

client.on("auth_failure", () => {
  ready = false;
});

client.on("disconnected", () => {
  ready = false;
});

function normalizeToChatId(raw) {
  const digits = String(raw || "").replace(/\D+/g, "");
  if (!digits) return null;
  return `${digits}@c.us`;
}

app.get("/status", async (_req, res) => {
  res.json({ ready: !!ready, has_qr: !!(lastQr && !ready) });
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

app.get("/contacts", async (_req, res) => {
  if (!ready) {
    res.status(503).json({ error: "WhatsApp não conectado. Escaneie o QR Code." });
    return;
  }
  try {
    const contacts = await client.getContacts();
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
    res.json({ contacts: out });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

app.post("/send", async (req, res) => {
  if (!ready) {
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
