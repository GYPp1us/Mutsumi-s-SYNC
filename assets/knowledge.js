"use strict";
let knowledgeData = { notes: [], skill_catalog: { skills: [] } },
  noteVersion = 0,
  skillRevision = null,
  historyAfter = 0;
function options(node, items, label) {
  node.replaceChildren(new Option("新建", ""));
  for (const item of items)
    node.append(new Option(label(item), item.id || item.name));
}
async function loadKnowledge() {
  try {
    knowledgeData = await api("api/knowledge");
    options(
      $("noteSelect"),
      knowledgeData.notes,
      (n) => `${n.title}${n.archived ? " · 已归档" : ""}`,
    );
    options(
      $("skillSelect"),
      knowledgeData.skill_catalog.skills,
      (s) => s.name + " · " + s.description,
    );
    $("deliverySummary").textContent = knowledgeData.deliveries.length
      ? knowledgeData.deliveries
          .map(
            (d) =>
              `${d.kind} · ${d.status} · ${d.count}项${d.attempts ? " · 重试" + d.attempts + "次" : ""}`,
          )
          .join("\n")
      : "目前没有待投递事项";
    if (knowledgeData.skill_catalog.warning)
      notice(knowledgeData.skill_catalog.warning);
  } catch (error) {
    notice(error);
  }
}
$("reloadKnowledge").onclick = loadKnowledge;
$("noteSelect").onchange = () => {
  const note = knowledgeData.notes.find((n) => n.id === $("noteSelect").value);
  noteVersion = note?.version || 0;
  $("noteId").value = note?.id || "";
  $("noteTitle").value = note?.title || "";
  $("noteBody").value = note?.body || "";
  $("noteEvidence").value = (note?.evidence_seqs || []).join(",");
  $("noteArchived").checked = note?.archived || false;
};
$("noteForm").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const evidence = $("noteEvidence")
      .value.split(",")
      .map((s) => Number(s.trim()));
    if (evidence.some((n) => !Number.isSafeInteger(n) || n <= 0))
      throw new Error("请填写有效的原始事件序号，用逗号分隔");
    const result = await api("api/notes", {
      id: $("noteId").value.trim(),
      expected_version: noteVersion,
      title: $("noteTitle").value,
      body: $("noteBody").value,
      evidence_seqs: evidence,
      archived: $("noteArchived").checked,
    });
    noteVersion = result.version;
    await loadKnowledge();
    $("noteSelect").value = result.id;
    notice("笔记已保存，原始历史仍保留。");
  } catch (error) {
    notice(error);
  }
};
$("skillSelect").onchange = async () => {
  const item = knowledgeData.skill_catalog.skills.find(
    (s) => s.name === $("skillSelect").value,
  );
  skillRevision = item?.revision || null;
  $("skillName").value = item?.name || "";
  $("skillPath").value = "SKILL.md";
  if (item) await readSkill();
  else
    $("skillBody").value =
      "---\nname: new-skill\ndescription: 何时使用这项技能\n---\n\n操作方法、需要的依据和如何验证结果。\n";
};
async function readSkill() {
  try {
    const result = await api(
      "api/skills/" +
        encodeURIComponent($("skillName").value) +
        "?path=" +
        encodeURIComponent($("skillPath").value) +
        "&revision=" +
        encodeURIComponent(skillRevision || ""),
    );
    $("skillBody").value = result.content;
  } catch (error) {
    notice(error);
  }
}
$("readSkill").onclick = readSkill;
$("skillForm").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const name = $("skillName").value.trim();
    const result = await api("api/skills/" + encodeURIComponent(name), {
      path: $("skillPath").value,
      content: $("skillBody").value,
      expected_revision: skillRevision,
    });
    skillRevision = result.revision;
    await loadKnowledge();
    $("skillSelect").value = name;
    notice("技能已保存，后续模型请求将看到新目录。");
  } catch (error) {
    notice(error);
  }
};
$("searchHistory").onclick = () => {
  historyAfter = 0;
  $("historyResults").replaceChildren();
  void nextHistory();
};
async function nextHistory() {
  try {
    const result = await api(
      "api/history?query=" +
        encodeURIComponent($("historyQuery").value) +
        "&after=" +
        historyAfter,
    );
    for (const entry of result.events) {
      const row = document.createElement("details"),
        summary = document.createElement("summary"),
        text = document.createElement("pre");
      summary.textContent = `#${entry.seq} · ${entry.kind}`;
      text.textContent = JSON.stringify(entry.data, null, 2);
      row.append(summary, text);
      $("historyResults").append(row);
    }
    if (result.next_after_seq === historyAfter)
      notice("没有更多匹配的原始记录。");
    historyAfter = result.next_after_seq;
  } catch (error) {
    notice(error);
  }
}
$("moreHistory").onclick = nextHistory;
async function checkOfficial() {
  try {
    const data = await api("api/qqbot/status");
    $("officialStatus").textContent = !data.configured
      ? "尚未配置官方机器人，可与 NapCat 并存。"
      : data.ready
        ? `官方机器人已连接 · ${data.enabled ? "收发已开启" : "收发关闭"}`
        : "官方机器人连接不可用";
    $("toggleOfficial").disabled = !data.configured;
    $("toggleOfficial").textContent = data.enabled
      ? "关闭官方 Bot 收发"
      : "开启官方 Bot 收发";
    $("officialStatus").dataset.enabled = String(data.enabled);
  } catch (error) {
    notice(error);
  }
}
$("checkOfficial").onclick = checkOfficial;
$("toggleOfficial").onclick = async () => {
  try {
    await api("api/qqbot/enabled", {
      enabled: $("officialStatus").dataset.enabled !== "true",
    });
    await checkOfficial();
  } catch (error) {
    notice(error);
  }
};
