import {
  mkdirSync,
  openSync,
  writeFileSync,
  fsyncSync,
  closeSync,
  renameSync,
  readFileSync,
  existsSync,
  readdirSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

export const hash = (text) => createHash("sha256").update(text).digest("hex");
export const eventKey = (p) =>
  hash(`${p.app_id}:${p.scope}:${p.conversation_id}:${p.id}`);
export class Journal {
  constructor(root) {
    this.root = root;
    mkdirSync(root, { recursive: true, mode: 0o700 });
  }
  path(key) {
    if (!/^[a-z0-9_-]{1,160}$/i.test(key))
      throw new Error("invalid journal key");
    return join(this.root, key + ".json");
  }
  get(key) {
    const path = this.path(key);
    return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null;
  }
  put(key, value) {
    const path = this.path(key),
      tmp = path + "." + randomUUID() + ".tmp";
    const fd = openSync(tmp, "wx", 0o600);
    try {
      writeFileSync(fd, JSON.stringify(value));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(tmp, path);
    if (process.platform !== "win32") {
      const dir = openSync(this.root, "r");
      try {
        fsyncSync(dir);
      } finally {
        closeSync(dir);
      }
    }
  }
  entries() {
    return readdirSync(this.root)
      .filter((f) => f.endsWith(".json"))
      .map((f) => [f.slice(0, -5), this.get(f.slice(0, -5))]);
  }
  remove(key) {
    unlinkSync(this.path(key));
  }
}
