# Nature Daily CET-4 Bot Hint

Use this Skill when the owner asks for a recurring Nature Briefing reading exercise, or when a scheduled prompt says to run it.

1. In the target group, register a normal `scheduler` task. Use a UTC+8 time/cron and put all business details in `prompt`; do not add `skill_name`, group fields, or workflow steps to the scheduler payload.
2. The prompt should tell the worker to read `skills/nature-daily-cet4/SKILL.md`, use the configured `OUTPUT_DIR` (local prototype: `skills/nature-daily-cet4/output`; production: a shared-data path), and include the target group ID, private-answer policy, and current UTC+8 date.
3. On a scheduled trigger, read this file, then call the generic `delegate_task` tool with a natural-language prompt containing the detailed Skill path and delivery requirements. The worker must call `report_task`.
4. After a verified `completed` report, read the reported exercise Markdown and send the exercise to the current schedule-bound group. Do not send the answer key to the group.
5. For an answer request in a private conversation, locate the matching answer file and send the answer and explanations there. If the request came from a group, current runtime cannot route a message to that member's private peer; state the limitation instead of claiming success.
6. Ordinary replies must remain flat text. Use `send` only for special segments or a verified Markdown image. Do not paste Nature's source article or expose any account cookie.

Configuration placeholders to resolve in the scheduling conversation:

- `TARGET_GROUP_ID=<numeric QQ group id>`
- `ANSWER_PRIVATE_POLICY=requester-private-conversation`
- `OUTPUT_DIR=skills/nature-daily-cet4/output`

For production, replace `OUTPUT_DIR` with `/opt/mutsumi-sync-v3/shared/data/nature-daily-cet4` and provide a secure generic Bot read bridge before relying on later answer lookup.
