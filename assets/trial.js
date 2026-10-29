'use strict';
const $ = id => document.getElementById(id);
let cursor = 0, polling = false, authenticated = false, uploaded = [], settings = {}, activePanel = 'chat';
const log = [], bubbles = new Map();
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Sync-UI': '1'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  let value; try { value = await response.json(); } catch { throw new Error('服务响应未确认，请查看事件记录后再决定是否重试'); }
  if (!response.ok) { if (response.status === 401 && authenticated) showLogin(); throw new Error(value.error || '请求未完成'); }
  return value;
}
function notice(error) { $('notice').textContent = error ? String(error.message || error) : ''; }
function showLogin() { authenticated = false; $('app').classList.add('hidden'); $('nav').classList.add('hidden'); $('login').classList.remove('hidden'); $('state').textContent = '未登录'; }
function showApp() { authenticated = true; $('login').classList.add('hidden'); $('app').classList.remove('hidden'); $('nav').classList.remove('hidden'); }
async function login(key) { await api('api/login', {access_key:key}); $('key').value = ''; showApp(); await poll(); }
$('loginForm').onsubmit = async event => { event.preventDefault(); try { notice(); await login($('key').value); } catch (error) { notice(error); } };
$('logout').onclick = async () => { try { await api('api/logout', {}); location.reload(); } catch (error) { notice(error); } };
document.querySelectorAll('[data-panel]').forEach(button => button.onclick = () => {
  activePanel = button.dataset.panel;
  document.querySelectorAll('.panel').forEach(panel => panel.classList.toggle('hidden', panel.id !== activePanel));
  if (activePanel === 'connections') checkQQ();
  if (activePanel === 'roles') loadRoles();
  if (activePanel === 'models') loadModel();
});
function image(id, parent) { if (!/^media_[a-f0-9]{64}$/.test(id)) return; const img = document.createElement('img'); img.src = 'api/media/' + encodeURIComponent(id); img.alt = '原始媒体'; img.loading = 'lazy'; parent?.append(img); return img; }
function bubble(label, owner, key) {
  if (key && bubbles.has(key)) return bubbles.get(key);
  $('empty')?.remove(); const node = document.createElement('div'); node.className = 'bubble' + (owner ? ' owner' : '');
  const caption = document.createElement('small'); caption.textContent = label; node.append(caption); $('messages').append(node);
  if (key) bubbles.set(key, node); return node;
}
function segments(parts, node) {
  let position = 0;
  for (const part of parts || []) {
    if (part.type === 'image' && part.media_id) image(part.media_id, node);
    else { const text = document.createElement('span'); text.className = 'body'; text.textContent = part.type === 'text' ? part.text : part.type === 'face' ? `[QQ 表情 ${part.face_id}]` : '[图片待观察]'; if (part.type === 'image') text.dataset.imagePosition = String(position); node.append(text); }
    if (part.type === 'image') position++;
  }
}
function displayEvent(event) {
  const data = event.data;
  if (event.kind === 'inbound' && (data.source_id === 'owner_web' || data.source_id?.startsWith('qq_'))) {
    const node = bubble(data.source_id?.startsWith('qq_') ? `${data.actor} · QQ · ${data.relationship}` : '你 · 网页', true, data.event_id);
    if (data.source_id?.startsWith('qq_')) segments(data.segments, node);
    else {
      const text = document.createElement('div'); text.className = 'body'; text.textContent = data.text || ''; node.append(text);
      for (const id of data.transport?.media || []) if (typeof id === 'string') image(id, node);
    }
  }
  if (event.kind === 'web_message') segments(data.message.segments, bubble('睦 · 网页', false));
  if (event.kind === 'action_result' && data.status === 'succeeded' && data.result?.transport === 'napcat_onebot') segments(data.action.message.segments, bubble('睦 · QQ 接口已确认', false));
  if (event.kind === 'media_observation' && bubbles.has(data.event_id)) {
    const node = bubbles.get(data.event_id);
    for (const item of data.observations || []) {
      const placeholder = node.querySelector(`[data-image-position="${Number(item.position)}"]`);
      if (!placeholder) continue;
      const img = item.data?.media?.media_id && image(item.data.media.media_id);
      if (img) placeholder.replaceWith(img); else placeholder.textContent = '[图片暂不可用]';
    }
  }
  if (event.kind !== 'context_snapshot') { log.push(event); appendTrace(event); }
}
function appendTrace(event) {
  const query = $('eventFilter').value.toLowerCase();
  if (query && !JSON.stringify(event).toLowerCase().includes(query)) return;
  const item = document.createElement('details'), title = document.createElement('summary'), text = document.createElement('pre');
  title.textContent = `#${event.seq} · ${event.kind} · ${event.data.source_id || event.data.action?.name || ''}`;
  text.textContent = JSON.stringify(event.data, null, 2); item.append(title, text); $('eventList').prepend(item);
}
$('eventFilter').oninput = () => { $('eventList').replaceChildren(); log.forEach(appendTrace); };
async function poll() {
  if (polling) return; polling = true;
  try {
    settings = await api('api/status'); showApp();
    $('model').textContent = settings.model; $('state').textContent = !settings.provider_configured ? '模型尚未配置' : settings.busy ? '正在处理' : settings.needs_attention ? '等待新指令' : '就绪';
    $('attention').classList.toggle('hidden', !settings.needs_attention);
    $('qqScope').textContent = settings.qq_scope; $('toggleQQ').textContent = settings.qq_enabled ? '关闭 QQ 收发' : '开启 QQ 收发';
    const result = await api('api/events?after=' + cursor);
    const messages = $('messages'), nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    for (const event of result.events) { displayEvent(event); cursor = Math.max(cursor, event.seq); }
    if (nearBottom) messages.scrollTop = messages.scrollHeight;
  } catch (error) { if (authenticated) notice(error); }
  finally { polling = false; }
}
function readBase64(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); }); }
$('files').onchange = async () => {
  $('send').disabled = true; notice();
  try {
    const files = [...$('files').files]; if (files.length + uploaded.length > 8) throw new Error('一条消息最多 8 张图片');
    for (const file of files) {
      if (file.size > 16 * 1024 * 1024) throw new Error('单张图片不能超过 16 MB');
      const result = await api('api/uploads', {base64: await readBase64(file), allow_qq:$('allowQQ').checked});
      uploaded.push(result.media_id); image(result.media_id, $('attachments'));
    }
  } catch (error) { notice(error); }
  finally { $('send').disabled = false; $('files').value = ''; }
};
$('chatForm').onsubmit = async event => {
  event.preventDefault(); if (!$('text').value.trim() && !uploaded.length) return; $('send').disabled = true; notice();
  try {
    await api('api/messages', {id:crypto.randomUUID(), text:$('text').value, media_ids:uploaded});
    $('text').value = ''; uploaded = []; $('attachments').replaceChildren(); await poll();
  } catch (error) { notice(error); }
  finally { $('send').disabled = false; }
};
async function checkQQ() {
  try { const result = await api('api/napcat/status'); $('qqStatus').textContent = result.logged_in ? (result.onebot_available ? 'QQ 已登录，消息接口可用' : 'QQ 已登录，正在等待消息接口') : result.panel_available ? '等待扫码登录' : 'NapCat 暂未连接'; }
  catch (error) { notice(error); }
}
$('checkQQ').onclick = checkQQ;
$('showQR').onclick = () => { $('qr').src = 'api/napcat/qr?t=' + Date.now(); $('qr').classList.remove('hidden'); };
$('qr').onerror = () => notice('登录二维码暂不可用；可能已经登录，请先检查状态');
$('refreshQR').onclick = async () => { try { await api('api/napcat/refresh', {}); $('showQR').click(); } catch (error) { notice(error); } };
$('toggleQQ').onclick = async () => { try { notice(); await api('api/napcat/enabled', {enabled:!settings.qq_enabled}); await poll(); await checkQQ(); } catch (error) { notice(error); } };
$('loadHooks').onclick = async () => {
  try {
    const result = await api('api/hooks'); $('hookList').replaceChildren();
    const endpoint = document.createElement('p'); endpoint.textContent = '统一端点：' + result.endpoint; $('hookList').append(endpoint);
    for (const event of result.events) {
      const card = document.createElement('details'), title = document.createElement('summary'), code = document.createElement('pre');
      title.textContent = `${event.event} · ${event.source_kind} · ${event.enabled ? '启用' : '停用'} · ${event.description}`;
      const body = JSON.stringify({event:event.event, id:crypto.randomUUID(), content:{message:'一条新的外部观察'}});
      code.textContent = `curl -X POST '${result.endpoint}' \\\n  -H 'Authorization: Bearer ${event.token}' \\\n  -H 'Content-Type: application/json' \\\n  --data '${body}'`;
      card.append(title, code); $('hookList').append(card);
    }
    if (!result.events.length) { const empty = document.createElement('p'); empty.textContent = '还没有注册事件。请在聊天中让助手注册，然后重新加载。'; $('hookList').append(empty); }
  } catch (error) { notice(error); }
};
(async () => {
  const fragment = new URLSearchParams(location.hash.slice(1)); const key = fragment.get('access');
  if (key) { history.replaceState(null, '', location.pathname); try { await login(key); } catch (error) { notice(error); } }
  else await poll();
  setInterval(() => { if (authenticated) poll(); }, 2500);
})();

let rolesVersion = 0, rolesDraft = [];
const roleLabels = {owner:'主人', friend:'朋友', stranger:'陌生人', blocked:'屏蔽'};
const ingressLabels = {accepted:'已进入模型', qq_disabled:'QQ 收发关闭', unregistered_stranger:'未登记，未接入', role_or_scope_disabled:'角色或接入范围未启用', group_not_enabled:'群聊未接入'};
function renderRoles() {
  $('roleList').replaceChildren();
  for (const contact of rolesDraft) {
    const row = document.createElement('div'); row.className = 'role-row';
    for (const [field, title] of [['qq_id','QQ号'],['name','称呼']]) {
      const label = document.createElement('label'), input = document.createElement('input');
      label.append(document.createTextNode(title)); input.type = 'text'; input.value = contact[field]; input.maxLength = field === 'qq_id' ? 20 : 80;
      input.readOnly = field === 'qq_id' && contact.existing; input.oninput = () => contact[field] = input.value; label.append(input); row.append(label);
    }
    const select = document.createElement('select');
    for (const [value, label] of Object.entries(roleLabels)) { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); }
    select.value = contact.role; select.onchange = () => contact.role = select.value; row.append(select);
    const label = document.createElement('label'), enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = contact.enabled;
    enabled.onchange = () => contact.enabled = enabled.checked; label.append(enabled, document.createTextNode('启用')); row.append(label);
    const remove = document.createElement('button'); remove.textContent = '移除'; remove.onclick = () => { rolesDraft = rolesDraft.filter(c => c !== contact); renderRoles(); }; row.append(remove); $('roleList').append(row);
  }
}
async function loadRoles() {
  try {
    notice(); const data = await api('api/roles'); rolesVersion = data.version;
    rolesDraft = data.contacts.map(c => ({...c, existing:true})); $('roleScope').value = data.scope; renderRoles();
    $('recentRoles').replaceChildren();
    for (const item of data.recent) {
      const row = document.createElement('div'); row.className = 'role-row';
      const text = document.createElement('span'); text.textContent = `${item.qq_id} · ${item.nickname || ''} · ${roleLabels[item.role] || item.role} · ${ingressLabels[item.decision] || item.decision} · ${item.at}`; row.append(text);
      if (!rolesDraft.some(c => c.qq_id === item.qq_id)) { const add = document.createElement('button'); add.textContent = '登记此账号'; add.onclick = () => { if (!rolesDraft.some(c => c.qq_id === item.qq_id)) rolesDraft.push({qq_id:item.qq_id, name:item.nickname || item.qq_id, role:'stranger', enabled:false, existing:true}); renderRoles(); }; row.append(add); }
      $('recentRoles').append(row);
    }
    if (!data.recent.length) $('recentRoles').textContent = '尚未收到已通过签名校验的 QQ 消息。可先检查 QQ 连接，再发送一条新消息。';
  } catch (error) { notice(error); }
}
$('reloadRoles').onclick = loadRoles;
$('addRole').onclick = () => { rolesDraft.push({qq_id:'',name:'',role:'stranger',enabled:false}); renderRoles(); };
$('saveRoles').onclick = async () => {
  try {
    notice(); await api('api/roles', {version:rolesVersion, scope:$('roleScope').value, contacts:rolesDraft.map(({qq_id,name,role,enabled}) => ({qq_id,name,role,enabled}))});
    await loadRoles(); await poll(); notice('角色设置已保存并生效。');
  } catch (error) { notice(error); }
};

let modelVersion = 0;
async function loadModel() {
  try {
    notice(); const data = await api('api/model'); modelVersion = data.version;
    $('modelBase').value = data.base_url; $('modelEndpoint').value = data.chat_endpoint || '';
    $('modelName').value = data.model; $('modelConsole').value = data.console_url;
    $('modelTimeout').value = data.timeout_seconds; $('modelTokens').value = data.max_tokens;
    $('modelOptions').value = JSON.stringify(data.request_options, null, 2); $('modelKey').value = '';
    $('modelKeyState').textContent = data.key_configured ? '已配置 Key，当前值不会回显。' : '尚未配置 Key。';
    $('modelResolvedEndpoint').textContent = '当前实际端点：' + data.effective_endpoint;
  } catch (error) { notice(error); }
}
function modelInput() {
  const options = JSON.parse($('modelOptions').value || '{}');
  if (!options || Array.isArray(options) || typeof options !== 'object') throw new Error('高级选项必须是 JSON 对象');
  return {version:modelVersion, base_url:$('modelBase').value.trim(), chat_endpoint:$('modelEndpoint').value.trim() || null,
    model:$('modelName').value.trim(), console_url:$('modelConsole').value.trim(), api_key:$('modelKey').value,
    timeout_seconds:Number($('modelTimeout').value), max_tokens:Number($('modelTokens').value), request_options:options};
}
$('reloadModel').onclick = loadModel;
$('testModel').onclick = async () => {
  $('testModel').disabled = true;
  try { notice('正在测试连接…'); const result = await api('api/model/test', modelInput()); notice(`连接成功：${result.resolved_model || result.model}，耗时 ${result.elapsed_ms} ms。当前配置尚未保存。`); }
  catch (error) { notice(error); } finally { $('testModel').disabled = false; }
};
$('modelForm').onsubmit = async event => {
  event.preventDefault();
  try { notice(); await api('api/model', modelInput()); await loadModel(); await poll(); notice('模型配置已保存，下一轮使用新配置。'); }
  catch (error) { notice(error); }
};
