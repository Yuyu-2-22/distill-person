# 微信 4.x 数据库结构参考（脱敏示例）

> 本文档中的 `wxid`、联系人姓名、消息数字均为**示例占位**，不含任何真实个人数据。

## 一、目录结构

微信 4.x 数据目录默认位于：

```
~\Documents\xwechat_files\<你的wxid>\db_storage\
├── contact.db          # 联系人 / 群 / 备注
├── message\
│   ├── message_0.db    # 当前消息分片
│   ├── message_1.db    # 更早期分片
│   └── ...
├── public.db
└── ...
```

## 二、加密方式

- 引擎：**WCDB（基于 SQLCipher 4）**
- 算法：AES-256-CTR
- KDF：PBKDF2-HMAC-SHA512，迭代 **256000** 次
- 密钥派生：`key = PBKDF2(password=raw_key_32B, salt=salt_16B, 256000)`
- Page size：**4096**
- HMAC 校验：每页末尾 64 字节 = `HMAC-SHA512(key=hmac_key, page_content)[0:64]`，其中 `hmac_key = HMAC-SHA512(key=key, salt)[:64]`
- 第一页前 16 字节为 salt。

## 三、contact.db 关键表

### Friend 表（联系人主表）

```sql
SELECT user_name, remark, nick_name FROM Friend;
-- user_name 即 wxid，如 wxid_example001
-- remark   是备注名（用户常用来搜的「张三」就在这里）
-- nick_name 是昵称
```

### ChatRoom 表（群聊）

```sql
SELECT chat_room_name, chat_room_member FROM ChatRoom;
```

### Name2Id 表（名称 → rowid 映射）

每个 message 库都有 `Name2Id`，把 `wxid` 映射到内部 `rowid`，消息记录里存的是 rowid：

```sql
SELECT rowid, user_name FROM Name2Id;
-- 例如：wxid_example001 | 32 ；wxid_example_me | 2
```

## 四、message 库关键表

每个联系人对应一张 `Msg_<md5(wxid)>` 表，表名是 wxid 的 MD5：

```sql
-- wxid = wxid_example001  →  表名 Msg_9428c5169964400c956e5561b52bfe75
SELECT * FROM Msg_9428c5169964400c956e5561b52bfe75 LIMIT 1;
```

`Chat_xxx` 表中的核心字段（经 WCDB 解码后）：

| 字段 | 含义 |
| --- | --- |
| `msgSvrId` | 服务端消息 ID（唯一） |
| `msgLocalId` / `rowid` | 本地自增 ID |
| `createTime` | 发送时间（Unix 秒，本地时区） |
| `Des` / `sender` | 发送者 rowid（在 Name2Id 里查） |
| `message` | 消息内容（文本）或类型标记 |
| `type` / `msgType` | 消息类型（1=文本，更多见下方） |
| `strTalker` | 会话对方 wxid |
| `strContent` | 内容（部分版本） |

### 常见 msgType

| type | 含义 |
| --- | --- |
| 1 | 文本 |
| 3 | 图片 |
| 34 | 语音 |
| 42 | 名片 |
| 43 | 视频 |
| 47 | 表情/emoji |
| 10000 | 系统消息（如「你已添加了xx」「撤回了一条消息」） |

## 五、发送者还原逻辑

`message` 表里存的是 rowid，`rowid=2`（或库的 `self_rowid`）一般是「我」，其余为对方。导出时用 `Name2Id` 把 rowid 翻回昵称/备注，并打标 `me` / 对方。

> 注：不同 message 分片的 `self` rowid 可能不同（例如分片 0 里 `me=2`、对方=`32`；分片 1 里 `me=2`、对方=`1`），合并时需按每个库各自的 `Name2Id` 分别映射，再统一归并为 `me` / 对方。
