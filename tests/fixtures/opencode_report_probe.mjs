import { tool } from "file:///root/.config/opencode/node_modules/@opencode-ai/plugin/dist/index.js";
import { rename, writeFile } from "node:fs/promises";

export default async function ProbePlugin() {
  return {
    tool: {
      report_task: tool({
        description: "Report a completed probe task.",
        args: { report: tool.schema.string() },
        async execute(args) {
          const path = process.env.MUTSUMI_REPORT_PATH;
          const token = process.env.MUTSUMI_REPORT_TOKEN;
          if (!path || !token) throw new Error("report channel unavailable");
          const temporary = `${path}.tmp`;
          await writeFile(temporary, JSON.stringify({ token, report: args.report }), "utf8");
          await rename(temporary, path);
          return "accepted";
        },
      }),
    },
  };
}
