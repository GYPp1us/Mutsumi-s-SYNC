# Skills

Skills are ordinary text files. They are not registered in a backend catalog,
interpreted as a workflow DSL, or executed by Mutsumi's SYNC.

Use this layout for a reusable workflow:

```text
skills/<name>/BOT.md       # short bot-facing hint, loaded only when needed
skills/<name>/SKILL.md     # complete instructions for OpenCode
```

`SKILL.md` follows OpenCode's file convention and should start with front
matter containing `name` and `description`:

```markdown
---
name: paper-digest
description: Retrieve and summarize a paper for the bound conversation.
---
```

The bot uses the single `file_edit` tool to list or read files under the
configured Skills root. When delegation is appropriate, it passes the
`SKILL.md` path and the delivery requirements in the natural-language prompt
to `delegate_task`. OpenCode reads the full file and must explicitly call
`report_task` with a concise result.

The scheduler stores only a bound conversation, time rule, and prompt. It does
not know Skill names, target groups, connectors, or workflow steps. Skill text
is temporary task context and is not copied into the Life Stream.
