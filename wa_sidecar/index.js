import path from "path";
import { fileURLToPath } from "url";
import dotenv from "dotenv";
import express from "express";
import axios from "axios";
import pino from "pino";
import qrcode from "qrcode-terminal";
import makeWASocket, {
  DisconnectReason,
  fetchLatestWaWebVersion,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.join(__dirname, ".env") });

const PYTHON_APP_URL = (process.env.PYTHON_APP_URL || "http://localhost:8000").replace(/\/$/, "");
const PORT = Number(process.env.PORT || 3000);

const logger = pino({ level: "silent" });

// ── Dedupe (last 100 keys) ──────────────────────────────────────────────────
const seenQueue = [];
const seenSet = new Set();

function rememberDedupeKey(key) {
  if (!key || seenSet.has(key)) return false;
  seenSet.add(key);
  seenQueue.push(key);
  if (seenQueue.length > 100) seenSet.delete(seenQueue.shift());
  return true;
}

// ── State ───────────────────────────────────────────────────────────────────
let sock = null;
let reconnectAttempt = 0;
let connectionOpen = false;
let lastQr = null;
let waConnectionState = "disconnected";

// ── Helpers ─────────────────────────────────────────────────────────────────
function detectMediaKind(m) {
  if (!m) return null;
  if (m.imageMessage)        return "image";
  if (m.videoMessage)        return "video";
  if (m.stickerMessage)      return "sticker";
  if (m.documentMessage)     return "document";
  if (m.audioMessage?.ptt)   return "ptt";
  if (m.audioMessage)        return "audio";
  return null;
}

function extractText(m) {
  if (!m) return "";
  if (m.conversation)                  return m.conversation;
  if (m.extendedTextMessage?.text)     return m.extendedTextMessage.text;
  return "";
}

// ── Forward to Python ────────────────────────────────────────────────────────
async function forwardToPython(body) {
  const url = `${PYTHON_APP_URL}/webhook`;
  console.log(`[wa_sidecar] → forwarding to Python:`, JSON.stringify(body, null, 2));
  try {
    const res = await axios.post(url, body, {
      timeout: 30_000,
      headers: { "Content-Type": "application/json" },
    });
    console.log(`[wa_sidecar] ✓ Python responded: ${res.status}`);
  } catch (e) {
    if (e.code === "ECONNREFUSED") {
      console.warn(`[wa_sidecar] ⚠ Python app not running at ${PYTHON_APP_URL} — message received but not forwarded.`);
      console.warn(`[wa_sidecar]   group_jid to copy into router.py → ${body.group_jid}`);
    } else {
      console.error(`[wa_sidecar] ✗ Forward failed:`, e?.message || e);
    }
  }
}

// ── Reconnect logic ──────────────────────────────────────────────────────────
const TERMINAL_DISCONNECT_CODES = new Set([403, 405, 409, 412, 500]);

async function connect() {
  if (sock) {
    try { sock.ev.removeAllListeners("creds.update"); }    catch { /* ignore */ }
    try { sock.ev.removeAllListeners("connection.update"); } catch { /* ignore */ }
    try { sock.ev.removeAllListeners("messages.upsert"); }  catch { /* ignore */ }
    try { sock.end(undefined); } catch { /* ignore */ }
    sock = null;
  }

  const { state, saveCreds } = await useMultiFileAuthState(
    path.join(__dirname, "auth_state")
  );

  let version;
  try {
    const v = await fetchLatestWaWebVersion({ timeout: 25_000 });
    version = v.version;
    if (!v.isLatest && v.error) {
      console.warn("[wa_sidecar] fetchLatestWaWebVersion fallback:", v.error?.message || v.error);
    }
  } catch (e) {
    console.warn("[wa_sidecar] Could not fetch latest WA Web version (using library default):", e?.message || e);
  }

  sock = makeWASocket({
    auth: state,
    logger,
    version,
    markOnlineOnConnect: false,
    generateHighQualityLinkPreview: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (connection === "connecting") {
      waConnectionState = "connecting";
      console.log("[wa_sidecar] Connecting to WhatsApp...");
    }

    if (qr) {
      lastQr = qr;
      console.log("\n[wa_sidecar] WhatsApp → Settings → Linked devices → Link a device — scan the QR below:\n");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      lastQr = null;
      connectionOpen = true;
      waConnectionState = "open";
      reconnectAttempt = 0;
      console.log("[wa_sidecar] ✅ WhatsApp connected! Listening for group messages...");
      console.log(`[wa_sidecar]    Bot number: ${sock?.user?.id || "unknown"}`);
      console.log(`[wa_sidecar]    Send any message in your group now to discover the group_jid.`);
    }

    if (connection === "close") {
      connectionOpen = false;
      waConnectionState = "disconnected";
      const code = lastDisconnect?.error?.output?.statusCode;
      const isLoggedOut = code === DisconnectReason.loggedOut;
      const isTerminal = code != null && TERMINAL_DISCONNECT_CODES.has(code);
      const shouldReconnect = !isLoggedOut && !isTerminal;
      console.log("[wa_sidecar] WhatsApp closed", { code, shouldReconnect });

      if (isLoggedOut) {
        console.error("[wa_sidecar] ❌ Logged out. Delete auth_state/ and re-scan QR.");
      }

      if (isTerminal) {
        console.error(
          "[wa_sidecar] ❌ Terminal disconnect (code " + code + "). " +
          "Delete auth_state/, run: npm update @whiskeysockets/baileys, restart, scan new QR."
        );
      }

      if (shouldReconnect) {
        const delay = Math.min(30_000, 1000 * 2 ** reconnectAttempt);
        reconnectAttempt += 1;
        console.log(`[wa_sidecar] Reconnecting in ${delay}ms (attempt ${reconnectAttempt})...`);
        setTimeout(() => connect().catch((e) => console.error("reconnect failed", e)), delay);
      }
    }
  });

  // ── Message handler ────────────────────────────────────────────────────────
  sock.ev.on("messages.upsert", async (upsert) => {
    try {
      const msgs = upsert.messages || [];
      console.log(`[wa_sidecar] messages.upsert fired — ${msgs.length} message(s), notifyType=${upsert.type}`);

      for (const msg of msgs) {
        const remoteJid = msg.key.remoteJid;

        // Log every message so you can see what's arriving
        console.log(`[wa_sidecar] ← raw msg | fromMe=${msg.key.fromMe} | jid=${remoteJid} | id=${msg.key.id}`);

        if (msg.key.fromMe) {
          console.log("[wa_sidecar]   skipped (fromMe)");
          continue;
        }

        if (!remoteJid) {
          console.log("[wa_sidecar]   skipped (no remoteJid)");
          continue;
        }

        if (!remoteJid.endsWith("@g.us")) {
          console.log(`[wa_sidecar]   skipped (not a group — jid=${remoteJid})`);
          continue;
        }

        const messageId = msg.key.id;
        const dedupeKey = `${remoteJid}:${messageId}`;
        if (!rememberDedupeKey(dedupeKey)) {
          console.log(`[wa_sidecar]   skipped (duplicate: ${dedupeKey})`);
          continue;
        }

        const senderJid = msg.key.participant || msg.participant || "";
        const senderPushname = msg.pushName || "";
        const ts = Number(msg.messageTimestamp || Date.now() / 1000);
        const m = msg.message || {};
        const text = extractText(m).trim();
        const mediaKind = detectMediaKind(m);

        console.log(`[wa_sidecar] ✓ group message received!`);
        console.log(`[wa_sidecar]   group_jid  : ${remoteJid}`);   // ← COPY THIS into router.py
        console.log(`[wa_sidecar]   sender_jid : ${senderJid}`);
        console.log(`[wa_sidecar]   sender_name: ${senderPushname}`);
        console.log(`[wa_sidecar]   text       : "${text}"`);
        console.log(`[wa_sidecar]   media_kind : ${mediaKind || "none"}`);

        const base = {
          group_jid: remoteJid,
          sender_jid: senderJid,
          sender_name: senderPushname,
          message_id: messageId,
          timestamp: Math.floor(ts),
        };

        if (text) {
          await forwardToPython({ ...base, text });
        } else if (mediaKind) {
          await forwardToPython({ ...base, text: "", message_kind: mediaKind });
        } else {
          console.log("[wa_sidecar]   skipped (no text or known media kind)");
        }
      }
    } catch (e) {
      console.error("[wa_sidecar] messages.upsert handler error:", e?.message || e);
    }
  });
}

// ── Express ──────────────────────────────────────────────────────────────────
const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  const user = sock?.user;
  const qrPending = Boolean(lastQr);
  res.json({
    ok: true,
    connection_open: connectionOpen,
    wa_connection_state: waConnectionState,
    qr_pending: qrPending,
    has_user: Boolean(user),
    user_id: user?.id || null,
    last_qr_pending: qrPending,
  });
});

app.post("/send", async (req, res) => {
  try {
    const { group_jid, text } = req.body || {};
    if (!group_jid || text == null)
      return res.status(400).json({ ok: false, error: "group_jid and text required" });
    if (!sock)
      return res.status(503).json({ ok: false, error: "socket not ready" });

    console.log(`[wa_sidecar] /send → ${group_jid} | "${String(text).slice(0, 80)}..."`);
    await sock.sendMessage(group_jid, { text: String(text) });
    return res.json({ status: "sent" });
  } catch (e) {
    console.error("[wa_sidecar] /send error:", e);
    return res.status(500).json({ ok: false, error: String(e?.message || e) });
  }
});

app.post("/react", async (req, res) => {
  try {
    const { group_jid, message_id, emoji } = req.body || {};
    if (!group_jid || !message_id || !emoji)
      return res.status(400).json({ ok: false, error: "group_jid, message_id, emoji required" });
    if (!sock)
      return res.status(503).json({ ok: false, error: "socket not ready" });

    console.log(`[wa_sidecar] /react → ${group_jid} | msg=${message_id} | emoji=${emoji}`);
    await sock.sendMessage(group_jid, {
      react: {
        text: emoji,
        key: { remoteJid: group_jid, id: message_id, fromMe: false },
      },
    });
    return res.json({ status: "ok" });
  } catch (e) {
    console.error("[wa_sidecar] /react error:", e);
    return res.status(500).json({ ok: false, error: String(e?.message || e) });
  }
});

app.listen(PORT, () => {
  console.log(`wa_sidecar listening on :${PORT} → Python ${PYTHON_APP_URL}`);
  connect().catch((e) => console.error("connect failed", e));
});