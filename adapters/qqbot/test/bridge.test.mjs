import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createBridge } from "../bridge.mjs";
import { Journal, eventKey } from "../journal.mjs";
const key = "test-private-bridge-key-at-least-32-chars";
function fakeBot() {
  const bot = new EventEmitter();
  bot.sent = [];
  bot.uploads = [];
  bot.api = {
    post: async (path, value) => {
      if (path.endsWith("/messages")) {
        bot.sent.push(value);
        if (bot.fail) throw new Error("lost receipt");
        return { id: "platform-" + bot.sent.length };
      }
      if (path.endsWith("/prepare"))
        return {
          upload_id: "upload",
          presigned_url: "https://test.cos.ap-guangzhou.myqcloud.com/image",
        };
      if (path.endsWith("/complete"))
        return {
          image_id: "image",
          url: "qqbot-media://image/v1/reference",
          ttl: 7776000,
        };
      throw new Error("unknown path");
    },
    delete: async () => ({}),
  };
  bot.uploadMedia = async (p) => {
    bot.uploads.push(p);
    return { file_info: "valid-file-reference" };
  };
  return bot;
}
async function fixture(t, options = {}) {
  const root =
    options.root || (await mkdtemp(join(tmpdir(), "mutsumi-qqbot-")));
  const bot = fakeBot(),
    posted = [];
  const fetcher =
    options.fetcher ||
    (async (url, init) => {
      if (init?.method === "PUT") return new Response("", { status: 200 });
      posted.push(JSON.parse(init.body));
      return Response.json({ accepted: true });
    });
  const bridge = createBridge({
    bot,
    appId: "12345",
    key,
    root,
    ingressUrl: "http://127.0.0.1:18240/qqbot/events",
    fetcher,
  });
  const port = await bridge.listen(0);
  bot.emit("ready");
  t.after(async () => {
    await bridge.close();
    if (!options.root) await rm(root, { recursive: true, force: true });
  });
  return {
    root,
    bot,
    bridge,
    posted,
    post: async (value) => {
      const r = await fetch(`http://127.0.0.1:${port}/send`, {
        method: "POST",
        headers: {
          Authorization: "Bearer " + key,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(value),
      });
      return { code: r.status, data: await r.json() };
    },
  };
}
const message = () => ({
  kind: "c2c",
  senderId: "owner",
  messageId: "msg1",
  content: "hello",
  timestamp: new Date().toISOString(),
});
test("inbound carries verified routing but no media URL into the core; duplicates do not resend", async (t) => {
  const f = await fixture(t);
  const msg = message();
  msg.attachments = [
    {
      content_type: "image/gif",
      url: "https://multimedia.nt.qq.com.cn/image?rkey=private-value",
    },
  ];
  await f.bridge.receive(msg);
  await f.bridge.receive(msg);
  assert.equal(f.posted.length, 1);
  assert.equal(f.posted[0].segments[1].type, "image");
  assert.ok(!JSON.stringify(f.posted[0]).includes("private-value"));
});
test("same action returns durable platform receipt and changed parameters are refused", async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message());
  const input = {
    action_id: "a",
    scope: "c2c",
    target_id: "owner",
    reply_id: "msg1",
    segments: [{ type: "text", text: "approved text" }],
  };
  assert.equal((await f.post(input)).code, 200);
  assert.equal((await f.post(input)).data.message_id, "platform-1");
  assert.equal(f.bot.sent.length, 1);
  assert.equal(
    (await f.post({ ...input, segments: [{ type: "text", text: "changed" }] }))
      .code,
    409,
  );
});
test("lost or interrupted send receipt is never automatically replayed", async (t) => {
  const f = await fixture(t);
  f.bot.fail = true;
  const input = {
    action_id: "unknown",
    scope: "c2c",
    target_id: "owner",
    segments: [{ type: "text", text: "once" }],
  };
  assert.equal((await f.post(input)).data.status, "unknown");
  assert.equal((await f.post(input)).code, 409);
  assert.equal(f.bot.sent.length, 1);
});
test("original GIF bytes use upload without auto-send; mixed media reuses platform material", async (t) => {
  const f = await fixture(t);
  const bytes = Buffer.from("GIF89a-original-animation-bytes");
  const image = {
    type: "image",
    mime: "image/gif",
    base64: bytes.toString("base64"),
  };
  const base = { scope: "c2c", target_id: "owner" };
  assert.equal(
    (await f.post({ ...base, action_id: "gif", segments: [image] })).code,
    200,
  );
  assert.deepEqual(f.bot.uploads[0].buffer, bytes);
  assert.equal(f.bot.uploads[0].srvSendMsg, false);
  assert.equal(
    (
      await f.post({
        ...base,
        action_id: "mixed",
        segments: [
          { type: "text", text: "before" },
          image,
          { type: "text", text: "after" },
        ],
      })
    ).code,
    200,
  );
  assert.match(
    f.bot.sent[1].markdown.content,
    /before!\[图片\]\(qqbot-media:\/\/image\/v1\/reference\)after/,
  );
  assert.equal(f.bot.sent[1].markdown.force_verify_image_resource, true);
});
test("pending incoming delivery is recovered after reopening the journal", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "mutsumi-recovery-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const bot = fakeBot();
  const one = createBridge({
    bot,
    appId: "12345",
    key,
    root,
    ingressUrl: "http://127.0.0.1:18240/qqbot/events",
    fetcher: async () => new Response("", { status: 503 }),
  });
  const msg = message();
  await one.receive(msg);
  await one.close();
  const journal = new Journal(join(root, "inbox"));
  const packet = {
    app_id: "12345",
    scope: "c2c",
    conversation_id: "owner",
    id: "msg1",
  };
  const id = eventKey(packet),
    saved = journal.get(id);
  assert.equal(saved.accepted, false);
  saved.due = 0;
  journal.put(id, saved);
  const two = await fixture(t, { root });
  await two.bridge.drain();
  assert.equal(two.posted.length, 1);
  assert.equal(journal.get(id).accepted, true);
});
