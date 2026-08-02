# Nature Access Notes

Checked 2026-08-02 from the official Springer Nature sites. This is operational guidance, not legal advice; re-check the linked pages before production use.

## What worked

- `https://www.nature.com/nature.rss` returned `200` with `application/rss+xml` and included current `Daily briefing:` items.
- `https://www.nature.com/search?q=Daily%20Briefing` returned public search results with canonical `/articles/d...` links for Daily Briefing pages.
- A sampled briefing page, `https://www.nature.com/articles/d41586-026-02307-9`, returned `200`, exposed `meta name="access" content="Yes"`, and exposed Schema.org `isAccessibleForFree=true`. Its page contained links to several child items, including Nature reading items and non-article/video items.

The briefing itself is therefore practical to discover and read as public HTML. Child pages must still be checked individually: `Nature Briefing` is a content type, not a blanket open-access license for every referenced Nature article.

## Robots and terms

- `https://www.nature.com/robots.txt` permits the general `User-agent: *` section subject to listed disallows, but explicitly disallows a number of AI/crawler user agents, including `GPTBot`, `ChatGPT-User`, `ClaudeBot`, `PerplexityBot`, and others. Do not impersonate a disallowed bot or use robots.txt as permission to copy content.
- The Nature Terms & Conditions, versioned 15 December 2025 at the time checked, allow viewing site content for private, non-commercial use. They prohibit making site content, feeds, or data available to others except as specifically permitted, and prohibit copying/downloading/storing content to populate a database or publication, subject to applicable exceptions and permissions. A QQ group delivery of transformed exercises may still require permission; link-first, excerpt-minimal output is the safer prototype posture.

## Cookie answer

A valid logged-in cookie can technically be sent with an HTTP client and may change a response from a login/paywall page to an authenticated page. That answers only the transport question. It does not:

- prove that automated fetching is allowed by the account or subscription;
- grant permission to redistribute, store, or transform subscription content for a group;
- make a paywalled article open access; or
- justify placing the cookie in a Bot prompt or OpenCode environment.

If subscription content is genuinely required, use Springer Nature's licensed TDM route and ask the rights holder. The official TDM page says subscriber institutions receive TDM rights for non-commercial research under their institutional terms, API keys are available for TDM APIs, and non-subscribers' paywalled-content requests are handled case by case. It also describes Open Access full-text APIs for eligible open-access resources:

`https://www.springernature.com/gp/researchers/text-and-data-mining`

For this Skill, the default is to skip a child article whose public-access status is not clear. Do not add cookie support until a separate, secret-managed fetcher and written permission/licensing decision exist.

