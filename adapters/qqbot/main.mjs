import { QQBot } from "@tencent-connect/qqbot-nodejs";
import { join } from "node:path";
import { createBridge } from "./bridge.mjs";
import { Journal } from "./journal.mjs";

const appId = process.env.QQBOT_APP_ID,
  appSecret = process.env.QQBOT_APP_SECRET,
  key = process.env.MUTSUMI_QQBOT_BRIDGE_KEY;
const root = process.env.QQBOT_STATE_DIR || "/var/lib/mutsumi-qqbot";
if (!appId || !appSecret || !key)
  throw new Error(
    "Configure official QQ credentials and the private bridge key before starting",
  );
const gateway = new Journal(join(root, "gateway"));
const bot = new QQBot({
  appId,
  appSecret,
  baseUrl: "https://api.bot.qq.com",
  markdownSupport: true,
  reconnect: { maxAttempts: 10 },
  sessionPersistence: {
    load: () => gateway.get("session"),
    save: (s) => gateway.put("session", s),
    clear: () => {
      if (gateway.get("session")) gateway.remove("session");
    },
  },
});
const bridge = createBridge({
  bot,
  appId,
  key,
  root,
  ingressUrl:
    process.env.QQBOT_INGRESS_URL || "http://127.0.0.1:18240/qqbot/events",
});
await bridge.listen(Number(process.env.QQBOT_BRIDGE_PORT || 18241));
for (const signal of ["SIGTERM", "SIGINT"])
  process.once(signal, () => {
    bot.stop();
    void bridge.close().then(() => process.exit(0));
  });
void bot.start().catch(async () => {
  console.error(
    "Official QQ gateway stopped; inspect credentials and permissions.",
  );
  await bridge.close();
  process.exitCode = 1;
});
