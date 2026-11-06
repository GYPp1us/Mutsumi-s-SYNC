import { createHash } from "node:crypto";
const MAX = 16 * 1024 * 1024;
export async function download(url, fetcher = fetch) {
  const parsed = new URL(url);
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    ![
      "qq.com",
      "qq.com.cn",
      "qpic.cn",
      "qlogo.cn",
      "gtimg.cn",
      "myqcloud.com",
    ].some((d) => parsed.hostname === d || parsed.hostname.endsWith("." + d))
  )
    throw new Error("media host not allowed");
  const response = await fetcher(url, {
    redirect: "error",
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok || Number(response.headers.get("content-length") || 0) > MAX)
    throw new Error("media unavailable");
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > MAX) throw new Error("media too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
export function imageBytes(part) {
  if (
    !["image/png", "image/jpeg", "image/gif"].includes(part.mime) ||
    typeof part.base64 !== "string" ||
    part.base64.length > 23 * 1024 * 1024
  )
    throw new Error("unsupported image");
  const bytes = Buffer.from(part.base64, "base64");
  if (
    !bytes.length ||
    bytes.length > MAX ||
    bytes.toString("base64") !== part.base64
  )
    throw new Error("invalid image bytes");
  return bytes;
}
export class Materials {
  constructor(bot, journal, fetcher = fetch) {
    this.bot = bot;
    this.journal = journal;
    this.fetcher = fetcher;
  }
  async reference(part) {
    const bytes = imageBytes(part),
      key = createHash("sha256").update(bytes).digest("hex");
    const cached = this.journal.get(key);
    if (cached && cached.expires_at > Date.now() + 60000) {
      cached.used_at = Date.now();
      this.journal.put(key, cached);
      return cached.url;
    }
    const entries = this.journal.entries();
    if (!cached && entries.length >= 100) {
      const [oldest, value] = entries.sort(
        (a, b) => a[1].used_at - b[1].used_at,
      )[0];
      await this.bot.api.delete(
        `/v2/media-images/${encodeURIComponent(value.image_id)}/delete`,
      );
      this.journal.remove(oldest);
    }
    const extension = {
      "image/png": "png",
      "image/jpeg": "jpg",
      "image/gif": "gif",
    }[part.mime];
    const md5 = createHash("md5").update(bytes).digest("hex"),
      sha1 = createHash("sha1").update(bytes).digest("hex");
    const prep = await this.bot.api.post("/v2/media-images/prepare", {
      file_name: key + "." + extension,
      file_size: bytes.length,
      content_type: part.mime,
      md5,
      sha1,
    });
    const url = new URL(prep.presigned_url);
    if (url.protocol !== "https:" || !url.hostname.endsWith(".myqcloud.com"))
      throw new Error("invalid upload destination");
    const uploaded = await this.fetcher(url, {
      method: "PUT",
      body: bytes,
      headers: { "Content-Type": part.mime },
      redirect: "error",
      signal: AbortSignal.timeout(120000),
    });
    if (!uploaded.ok) throw new Error("upload failed");
    const result = await this.bot.api.post("/v2/media-images/complete", {
      upload_id: prep.upload_id,
      md5,
    });
    if (
      !/^qqbot-media:\/\/image\/v1\/[A-Za-z0-9_=-]+$/.test(result.url) ||
      typeof result.image_id !== "string"
    )
      throw new Error("invalid material receipt");
    this.journal.put(key, {
      url: result.url,
      image_id: result.image_id,
      expires_at:
        Date.now() + Math.min(Number(result.ttl) || 0, 7776000) * 1000,
      used_at: Date.now(),
    });
    return result.url;
  }
}
export const markdownText = (text) =>
  text.replace(/[\\`*_{}\[\]<>()#+.!|~-]/g, "\\$&");
