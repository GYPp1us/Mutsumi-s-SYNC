---
name: nature-daily-cet4
description: Create a daily CET-4 English reading exercise from a publicly accessible Nature Briefing child article, save the exercise and answer key, and return a compact report for the bound Mutsumi bot pipeline. Use for scheduled Nature Briefing tasks or explicit requests to make and deliver this kind of exercise.
---

# Nature Daily CET-4 Worker

This is the detailed OpenCode worker instruction. Read [references/nature-access.md](references/nature-access.md) before fetching Nature content. The worker may research and write files, but it must not claim that a QQ message was sent: delivery belongs to the original Bot pipeline.

## Inputs

The delegation prompt must provide these values. Do not invent missing target IDs or paths.

- `OUTPUT_DIR`: local prototype default `skills/nature-daily-cet4/output`; production should use a shared-data path such as `/opt/mutsumi-sync-v3/shared/data/nature-daily-cet4`
- `TARGET_GROUP_ID`: the group where the exercise is to be posted
- `ANSWER_PRIVATE_POLICY`: normally, reply in the asking member's private conversation; the current runtime must provide a generic cross-peer send capability before this can be fulfilled from a group pipeline
- `BOT_SKILL_PATH`: `skills/nature-daily-cet4/BOT.md`
- `RUN_DATE`: UTC+8 date used for filenames and deduplication

If `TARGET_GROUP_ID`, `OUTPUT_DIR`, or the private-answer routing policy is absent, report `blocked: missing configuration` and do not create a partial exercise.

The local prototype path is readable by the current `file_edit` root. A production shared-data path is durable across releases but is currently protected from that Bot file tool; do not claim later answer lookup works until a secure read bridge or equivalent generic capability is configured.

## Workflow

1. Discover the newest Nature Briefing.

   - Prefer the official RSS feed `https://www.nature.com/nature.rss`.
   - Select a newest item whose title begins with `Daily briefing:` and retain its canonical URL, DOI if present, title, publication date, and feed timestamp.
   - If the feed is unavailable, use the official search fallback `https://www.nature.com/search?q=Daily%20Briefing` and select a newest result with that title prefix.
   - Do not use search-engine cache, copied articles, or unofficial mirrors as the source.

2. Fetch the briefing page once and identify child items from its HTML links.

   - Prefer a child item that is a Nature article with a short reading label such as `Nature | 5 min read`.
   - Skip videos, immersive pages, external links, reference-only links, and items already recorded in `state.json`.
   - Check the child page before using it. Accept it only when the page indicates public access, such as `meta access=Yes` and/or Schema.org `isAccessibleForFree=true`. A public Nature Briefing page does not make every linked child article public.
   - On `403`, `429`, robots denial, login wall, or ambiguous access status, stop or choose another public child. Never bypass the restriction.

3. Produce an original CET-4 exercise.

   - Use the selected child article's facts only as background. Do not copy paragraphs, sentence sequences, distinctive wording, figures, tables, images, or the briefing's full text.
   - Write a self-contained English passage of about 300-450 words at CET-4 difficulty. Explain necessary science terms in context and preserve uncertainty, attribution, and whether a source is peer reviewed.
   - Create five four-option comprehension questions. Use a balanced mix of main idea, detail, inference, vocabulary-in-context, and author-purpose questions. Each question must have exactly one defensible answer.
   - Write a concise answer key and Chinese explanations that cite the relevant passage idea, not hidden chain-of-thought.
   - Include the official source link and a short attribution/copyright note. Do not present the exercise as Nature-produced or endorsed.

4. Write atomic, inspectable artifacts.

   Use UTF-8 Markdown and JSON. Create the output directory if needed.

   - `OUTPUT_DIR/exercises/YYYY-MM-DD-<slug>.md`: group-safe exercise only, with title, passage, questions, official source link, and attribution note. Do not include answers.
   - `OUTPUT_DIR/answers/YYYY-MM-DD-<slug>.md`: answer key and explanations for private delivery.
   - `OUTPUT_DIR/state.json`: a bounded list of source URL/DOI, source date, access check, exercise path, answer path, and creation time. Never store the source article body.

   Write temporary files beside the destination and rename them into place. If the same source URL or DOI is already in `state.json`, report `skipped: already processed` and do not send a duplicate.

5. Return a truthful report with `report_task`.

   The report must contain:

   - `completed` or `blocked` or `skipped`
   - selected briefing title and child title
   - official source URL
   - public-access check result
   - exact exercise and answer paths
   - whether the group artifact is ready for the Bot to read
   - any routing limitation for the private answer

   Do not include the Nature article body or a made-up message ID. Do not treat process exit, stdout, or file existence alone as a successful platform delivery.

## Delivery Contract

After a successful worker report, the original Bot should read the exercise file and send its plain text to the schedule-bound group. If rich formatting is required, it must use the platform's verified `send.markdown_image` path rather than placing complex Markdown in ordinary assistant text.

When a person asks for the answer in a private conversation, the Bot may read the matching answer file and send it there. When the question originates in a group, the current project has no generic cross-peer send tool: do not silently send the answer to the group or claim a private send. Report this as a runtime capability gap until a generic, server-bound peer-routing primitive is implemented.

## Safety And Access Rules

- Treat account cookies as bearer credentials. Never place them in this Skill, a delegation prompt, a report, an artifact, Git, or logs.
- A cookie can technically authenticate an HTTP request in a separate controlled fetcher, but it does not grant redistribution rights and is not a compliant fallback for paywalled content. Use licensed Springer Nature TDM/API access for subscription content, or obtain explicit permission.
- Use a descriptive User-Agent, low request volume, caching, and exponential backoff. Stop on repeated `403`/`429` responses.
- Do not build a database of Nature article bodies. Store only the original exercise, answer key, source metadata, and deduplication state.
