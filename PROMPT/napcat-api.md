# NapCat QQ API 文档

> 版本: 4.17.53
> 文档来源: https://napneko.github.io/api/4.17.53

## 连接方式

| 连接方式 | 可用 |
|----------|------|
| HTTP 接口调用 | ✅ |
| HTTP POST 事件上报 | ✅ |
| HTTP POST 快速操作 | ✅ |
| 正向 WS 连接 | ✅ |

## 消息事件 (Message Events)

### 公共字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `time` | number | Unix 时间戳 |
| `self_id` | number | 机器人 QQ 号 |
| `post_type` | string | 始终为 `"message"` |
| `message_type` | string | `"private"` 或 `"group"` |
| `sub_type` | string | 消息子类型 |
| `message_id` | number | 消息 ID |
| `message_seq` | number | 消息序列号 |
| `user_id` | number | 发送者 QQ 号 |
| `message` | array/string | 消息内容 |
| `raw_message` | string | 原始消息文本 |
| `sender` | object | 发送者信息 |

### 私聊消息 (Private Message)

```json
{
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend|group|normal",
  "user_id": 987654321,
  "message": [{ "type": "text", "data": { "text": "Hello" } }],
  "sender": {
    "user_id": 987654321,
    "nickname": "Alice",
    "sex": "female"
  }
}
```

### 群消息 (Group Message)

```json
{
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal|anonymous|notice",
  "group_id": 123456789,
  "user_id": 987654321,
  "message": [{ "type": "text", "data": { "text": "Hello" } }],
  "sender": {
    "user_id": 987654321,
    "nickname": "Bob",
    "card": "群名片",
    "role": "owner|admin|member"
  }
}
```

### 消息段类型 (Message Segments)

| 类型 | 说明 | 示例 |
|------|------|------|
| `text` | 文本 | `{ "type": "text", "data": { "text": "你好" } }` |
| `image` | 图片 | `{ "type": "image", "data": { "file": "url", "url": "..." } }` |
| `face` | 表情 | `{ "type": "face", "data": { "id": "1" } }` |
| `at` | @某人 | `{ "type": "at", "data": { "qq": "123456" } }` |
| `reply` | 回复 | `{ "type": "reply", "data": { "id": "123456" } }` |
| `record` | 语音 | `{ "type": "record", "data": { "file": "..." } }` |
| `video` | 视频 | `{ "type": "video", "data": { "file": "..." } }` |
| `forward` | 转发消息 | `{ "type": "forward", "data": { "id": "..." } }` |
| `node` | 合并转发节点 | `{ "type": "node", "data": { "user_id": "...", "content": [...] } }` |

### CQ 码格式

消息也可以使用 CQ 码表示：

```
[CQ:text,text=Hello] [CQ:face,id=1] [CQ:at,qq=123456] [CQ:image,file=abc.jpg]
```

格式：`[CQ:type,param1=value1,param2=value2]`

---

## 消息动作 (Message Actions)

### send_msg - 发送消息

```json
// 请求
{
  "action": "send_msg",
  "params": {
    "message_type": "group",
    "group_id": "123456",
    "message": [
      { "type": "text", "data": { "text": "Hello " } },
      { "type": "at", "data": { "qq": "987654" } }
    ]
  }
}

// 响应
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "message_id": 12345678
  }
}
```

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `message_type` | string | `"private"` 或 `"group"` |
| `user_id` | string | 私聊时指定用户 QQ |
| `group_id` | string | 群聊时指定群号 |
| `message` | string/object/array | 消息内容 |
| `auto_escape` | boolean | 是否转义 CQ 码 |

### send_private_msg - 发送私聊消息

```json
{
  "action": "send_private_msg",
  "params": {
    "user_id": "123456",
    "message": "Hello!"
  }
}
```

### send_group_msg - 发送群消息

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": "123456",
    "message": "Hello group!"
  }
}
```

### delete_msg - 撤回消息

```json
{
  "action": "delete_msg",
  "params": {
    "message_id": 12345678
  }
}
```

### get_msg - 获取消息

```json
{
  "action": "get_msg",
  "params": {
    "message_id": 12345678
  }
}
```

### get_forward_msg - 获取转发消息内容

```json
{
  "action": "get_forward_msg",
  "params": {
    "message_id": "12345678"
  }
}
```

### set_msg_emoji_like - 表情回应

```json
{
  "action": "set_msg_emoji_like",
  "params": {
    "message_id": "12345678",
    "emoji_id": "1"
  }
}
```

---

## 群管理动作 (Group Actions)

### get_groups_list - 获取群列表

```json
{
  "action": "get_groups_list",
  "params": {}
}
```

### get_group_member_list - 获取群成员列表

```json
{
  "action": "get_group_member_list",
  "params": {
    "group_id": "123456789"
  }
}
```

### set_group_kick - 踢人

```json
{
  "action": "set_group_kick",
  "params": {
    "group_id": "123456789",
    "user_id": "987654321",
    "reject_add_request": false
  }
}
```

### set_group_ban - 禁言

```json
{
  "action": "set_group_ban",
  "params": {
    "group_id": "123456789",
    "user_id": "987654321",
    "duration": 600  // 秒，0 为解除
  }
}
```

### set_group_whole_ban - 全体禁言

```json
{
  "action": "set_group_whole_ban",
  "params": {
    "group_id": "123456789",
    "enable": true
  }
}
```

### set_group_card - 设置群名片

```json
{
  "action": "set_group_card",
  "params": {
    "group_id": "123456789",
    "user_id": "987654321",
    "card": "新名片"
  }
}
```

### set_group_leave - 退群

```json
{
  "action": "set_group_leave",
  "params": {
    "group_id": "123456789"
  }
}
```

### set_group_name - 群改名

```json
{
  "action": "set_group_name",
  "params": {
    "group_id": "123456789",
    "group_name": "新群名"
  }
}
```

### get_group_info - 获取群信息

```json
{
  "action": "get_group_info",
  "params": {
    "group_id": "123456789"
  }
}
```

### get_group_member_info - 获取群成员信息

```json
{
  "action": "get_group_member_info",
  "params": {
    "group_id": "123456789",
    "user_id": "987654321"
  }
}
```

---

## 好友动作 (User Actions)

### get_friend_list - 获取好友列表

```json
{
  "action": "get_friend_list",
  "params": {}
}
```

### get_stranger_info - 获取陌生人信息

```json
{
  "action": "get_stranger_info",
  "params": {
    "user_id": "987654321"
  }
}
```

### get_login_info - 获取登录信息

```json
{
  "action": "get_login_info",
  "params": {}
}
```

---

## 文件动作 (File Actions)

### upload_private_file - 上传私聊文件

```json
{
  "action": "upload_private_file",
  "params": {
    "user_id": "123456",
    "file": "/path/to/file",
    "name": "filename"
  }
}
```

### upload_group_file - 上传群文件

```json
{
  "action": "upload_group_file",
  "params": {
    "group_id": "123456",
    "file": "/path/to/file",
    "name": "filename",
    "folder": "/"
  }
}
```

### get_group_files_list - 获取群文件列表

```json
{
  "action": "get_group_files_list",
  "params": {
    "group_id": "123456",
    "folder_id": "/"
  }
}
```

---

## 核心 API (Core API - 插件模式)

> 以下为 NapCat 插件开发可用的核心 API（通过 `core.apis` 访问）

### Peer 对象

发送消息需要构建 Peer 对象：

```typescript
// 私聊
const peer = {
  chatType: 1,  // 或 ChatType.KCHATTYPEGROUP
  peerUid: 'user_uid'
};

// 群聊
const peer = {
  chatType: 2,  // 或 ChatType.KCHATTYPEGROUP
  peerUid: 'group_id'
};
```

### MsgApi - 消息 API

```typescript
// 发送消息
await core.apis.MsgApi.sendMsg(peer, msgElements)

// 获取消息历史
await core.apis.MsgApi.getMsgHistory(peer, msgId, count, isReverseOrder)

// 撤回消息
await core.apis.MsgApi.recallMsg(peer, msgId)

// 添加表情回应
await core.apis.MsgApi.setEmojiLike(peer, msgSeq, emojiId, set)
```

### GroupApi - 群管理 API

```typescript
// 获取群列表
await core.apis.GroupApi.getGroups(forced)

// 获取群成员
await core.apis.GroupApi.getGroupMemberAll(groupCode)

// 禁言成员
await core.apis.GroupApi.banMember(groupCode, [{ uid: 'member_uid', timeStamp: 600 }])

// 踢人
await core.apis.GroupApi.kickMember(groupCode, ['member_uid'])

// 退群
await core.apis.GroupApi.quitGroup(groupCode)
```

---

## WebSocket 事件

### 连接地址

```
ws://localhost:3000/ws
```

### 事件格式

```json
{
  "post_type": "message",
  "message_type": "group",
  "group_id": 123456,
  "user_id": 789,
  "message": [...]
}
```

---

## 快速回复 (HTTP POST 模式)

当使用 HTTP POST 适配器时，可通过返回响应快速回复：

```javascript
app.post('/event', (req, res) => {
  const event = req.body;
  
  if (event.post_type === 'message') {
    res.json({
      reply: '回复内容',
      at_sender: event.message_type === 'group',  // 群聊时 @ 发送者
      delete: false,
      kick: false,
      ban: false,
      ban_duration: 0
    });
  }
});
```
