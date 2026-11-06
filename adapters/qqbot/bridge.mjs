import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { join } from "node:path";
import { Journal, hash, eventKey } from "./journal.mjs";
import { Materials, download, imageBytes, markdownText } from "./media.mjs";

const identifier = (s) =>
  typeof s === "string" && /^[A-Za-z0-9_.-]{1,160}$/.test(s);
function normalize(appId, msg) {
  if (!["c2c", "group"].includes(msg.kind) || msg.senderIsBot) return null;
  const conversation = msg.kind === "group" ? msg.groupOpenid : msg.senderId;
  if (
    !identifier(conversation) ||
    !identifier(msg.senderId) ||
    !identifier(msg.messageId)
  )
    throw new Error("invalid platform identity");
  const segments = [],
    attachments = [];
  if (msg.content) segments.push({ type: "text", text: msg.content });
  // Preserve platform attachment order. Text/attachment interleaving is not supplied by this SDK.
  for (const attachment of msg.attachments || []) {
    if (attachment.content_type?.startsWith("image/")) {
      segments.push({ type: "image", position: attachments.length });
      attachments.push(attachment);
    } else
      segments.push({
        type: "text",
        text: `[官方QQ暂未接入的附件：${attachment.content_type || "unknown"}]`,
      });
  }
  if (!segments.length) return null;
  if (attachments.length > 8) throw new Error("too many images");
  return {
    packet: {
      app_id: appId,
      id: msg.messageId,
      scope: msg.kind,
      conversation_id: conversation,
      actor_id: msg.senderId,
      occurred_at: msg.timestamp,
      segments,
    },
    attachments,
  };
}
async function body(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 24 * 1024 * 1024) throw new Error("request too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
function equal(a, b) {
  const x = Buffer.from(hash(a)),
    y = Buffer.from(hash(b));
  return timingSafeEqual(x, y);
}
function reply(response, status, data) {
  response.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(data));
}

export function createBridge({
  bot,
  appId,
  key,
  root,
  ingressUrl,
  fetcher = fetch,
}) {
  if (!identifier(appId) || typeof key !== "string" || key.length < 24)
    throw new Error("invalid bridge configuration");
  const ingress = new URL(ingressUrl);
  if (
    ingress.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(ingress.hostname) ||
    ingress.username ||
    ingress.password
  )
    throw new Error("ingress must use loopback HTTP");
  const inbox = new Journal(join(root, "inbox")),
    outbox = new Journal(join(root, "outbox")),
    state = new Journal(join(root, "state"));
  const materials = new Materials(
    bot,
    new Journal(join(root, "materials")),
    fetcher,
  );
  let ready = false,
    stopped = false,
    delivering = false,
    lastError = "",
    server;
  let serial = Promise.resolve();
  bot.on("ready", () => {
    ready = true;
    lastError = "";
  });
  bot.on("resumed", () => {
    ready = true;
  });
  bot.on("disconnected", () => {
    ready = false;
  });
  bot.on("error", () => {
    lastError =
      "QQ connection error; check credentials and platform permissions";
  });
  async function receive(msg) {
    const normalized = normalize(appId, msg);
    if (!normalized) return;
    const id = eventKey(normalized.packet),
      old = inbox.get(id);
    if (old) {
      if (JSON.stringify(old.packet) !== JSON.stringify(normalized.packet))
        throw new Error("conflicting duplicate event");
      return;
    }
    if (inbox.entries().filter(([, item]) => !item.accepted).length >= 1000)
      throw new Error("inbox capacity reached");
    inbox.put(id, {
      ...normalized,
      accepted: false,
      created_at: Date.now(),
      attempts: 0,
      due: 0,
    });
    await drain();
  }
  bot.on("message", (_ctx, msg) =>
    receive(msg).catch(() => {
      lastError = "Incoming message could not be persisted or forwarded";
    }),
  );
  async function drain() {
    if (delivering || stopped) return;
    delivering = true;
    try {
      for (const [id, item] of inbox.entries()) {
        if (item.accepted) {
          if (Date.now() - item.created_at > 7 * 86400000) inbox.remove(id);
          continue;
        }
        if (item.due > Date.now()) continue;
        try {
          const r = await fetcher(ingressUrl, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: "Bearer " + key,
            },
            body: JSON.stringify(item.packet),
            redirect: "error",
            signal: AbortSignal.timeout(15000),
          });
          const result = await r.json();
          if (!r.ok || (!result.accepted && !result.ignored))
            throw new Error("ingress not confirmed");
          item.accepted = true;
          item.accepted_at = Date.now();
          item.ignored = result.ignored || null;
        } catch {
          item.attempts++;
          item.due =
            Date.now() +
            Math.min(60000, 1000 * 2 ** Math.min(item.attempts, 6));
          lastError = "Incoming delivery pending; retrying";
        }
        inbox.put(id, item);
      }
    } finally {
      delivering = false;
    }
  }
  async function send(input) {
    if (
      !identifier(input.target_id) ||
      !["c2c", "group"].includes(input.scope) ||
      typeof input.action_id !== "string" ||
      input.action_id.length > 240 ||
      !Array.isArray(input.segments) ||
      !input.segments.length ||
      input.segments.length > 32
    )
      return { code: 400, data: { not_started: true, error: "invalid send" } };
    const fingerprint = hash(JSON.stringify(input)),
      id = hash(input.action_id),
      old = outbox.get(id);
    if (old) {
      if (old.fingerprint !== fingerprint)
        return {
          code: 409,
          data: { error: "action parameters changed", not_started: true },
        };
      return old.status === "succeeded"
        ? { code: 200, data: old.receipt }
        : {
            code: 409,
            data: {
              status: "unknown",
              error: "previous attempt has no verified receipt",
            },
          };
    }
    if (!ready)
      return {
        code: 503,
        data: { not_started: true, error: "official QQ is not connected" },
      };
    if (
      input.segments.some((p) =>
        p.type === "text"
          ? typeof p.text !== "string" || p.text.length > 16384
          : p.type !== "image",
      )
    )
      return {
        code: 400,
        data: { not_started: true, error: "unsupported segment" },
      };
    let bytes = 0;
    const mixed =
      input.segments.some((p) => p.type === "image") &&
      input.segments.length > 1;
    try {
      for (const part of input.segments)
        if (part.type === "image") {
          const length = imageBytes(part).length;
          if (mixed && length > 10 * 1024 * 1024) throw new Error();
          bytes += length;
        }
      if (bytes > 16 * 1024 * 1024) throw new Error();
    } catch {
      return {
        code: 400,
        data: { not_started: true, error: "invalid image payload" },
      };
    }
    let seq = 1;
    if (input.reply_id) {
      const received = inbox.get(
        eventKey({
          app_id: appId,
          scope: input.scope,
          conversation_id: input.target_id,
          id: input.reply_id,
        }),
      );
      const at = received && Date.parse(received.packet.occurred_at),
        countKey = hash(`${input.scope}:${input.target_id}:${input.reply_id}`),
        count = state.get(countKey)?.count || 0;
      if (
        !received ||
        !Number.isFinite(at) ||
        Date.now() < at ||
        Date.now() - at >= 5 * 60000 ||
        count >= (input.scope === "c2c" ? 4 : 5)
      )
        return {
          code: 409,
          data: {
            not_started: true,
            error: "reply opportunity expired or exhausted",
          },
        };
      seq = count + 1;
      state.put(countKey, { count: seq });
    }
    outbox.put(id, { fingerprint, status: "started", at: Date.now() });
    try {
      const target = {
        scope: input.scope,
        targetId: input.target_id,
        ...(input.reply_id ? { msgId: input.reply_id } : {}),
      };
      let payload;
      if (input.segments.every((p) => p.type === "text"))
        payload = {
          msg_type: 0,
          content: input.segments.map((p) => p.text).join(""),
        };
      else if (
        input.segments.length === 1 &&
        input.segments[0].type === "image"
      ) {
        const upload = await bot.uploadMedia({
          target,
          fileType: 1,
          buffer: imageBytes(input.segments[0]),
          srvSendMsg: false,
        });
        payload = { msg_type: 7, media: { file_info: upload.file_info } };
      } else {
        const parts = [];
        for (const part of input.segments)
          parts.push(
            part.type === "text"
              ? markdownText(part.text)
              : `![图片](${await materials.reference(part)})`,
          );
        payload = {
          msg_type: 2,
          markdown: {
            content: parts.join(""),
            force_verify_image_resource: true,
          },
        };
      }
      if (input.reply_id) {
        payload.msg_id = input.reply_id;
        payload.msg_seq = seq;
      }
      const result = await bot.api.post(
        `/v2/${input.scope === "c2c" ? "users" : "groups"}/${encodeURIComponent(input.target_id)}/messages`,
        payload,
      );
      if (typeof result.id !== "string" || !result.id)
        throw new Error("missing platform receipt");
      const receipt = { status: "succeeded", message_id: result.id };
      outbox.put(id, {
        fingerprint,
        status: "succeeded",
        receipt,
        at: Date.now(),
      });
      return { code: 200, data: receipt };
    } catch {
      outbox.put(id, { fingerprint, status: "unknown", at: Date.now() });
      return {
        code: 502,
        data: {
          status: "unknown",
          error: "send receipt not confirmed; do not replay",
        },
      };
    }
  }
  const timer = setInterval(
    () =>
      void drain().catch(() => {
        lastError = "Incoming delivery store unavailable";
      }),
    1000,
  );
  timer.unref();
  return {
    receive,
    drain,
    async listen(port = 18241) {
      server = createServer(async (req, res) => {
        try {
          if (
            !req.headers.authorization?.startsWith("Bearer ") ||
            !equal(req.headers.authorization.slice(7), key)
          )
            return reply(res, 401, { error: "unauthorized" });
          if (req.method === "GET" && req.url === "/status")
            return reply(res, 200, {
              app_id: appId,
              ready,
              pending: inbox.entries().filter(([, v]) => !v.accepted).length,
              last_error: lastError,
            });
          if (req.method === "POST" && req.url === "/media") {
            const p = await body(req),
              item = inbox.get(p.event_key),
              image = item?.attachments[p.position];
            if (!image) return reply(res, 404, { error: "image unavailable" });
            const bytes = await download(image.url, fetcher);
            return reply(res, 200, { base64: bytes.toString("base64") });
          }
          if (req.method === "POST" && req.url === "/send") {
            const input = await body(req);
            const operation = serial.then(() => send(input));
            serial = operation.catch(() => {});
            const result = await operation;
            return reply(res, result.code, result.data);
          }
          reply(res, 404, { error: "not found" });
        } catch {
          reply(res, 400, { error: "request could not be completed" });
        }
      });
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, "127.0.0.1", resolve);
      });
      return server.address().port;
    },
    async close() {
      stopped = true;
      clearInterval(timer);
      await serial;
      while (delivering) await new Promise((resolve) => setTimeout(resolve, 5));
      await new Promise((resolve) =>
        server ? server.close(resolve) : resolve(),
      );
    },
  };
}
