---
name: distill-person
description: 蒸馏人——从微信 4.x（Weixin.exe）本地加密数据库中提取指定联系人的完整聊天记录，解密、清洗后输出可直接被 AI 消费的结构化 JSON 文件。此 skill 应在用户要求导出/提取/蒸馏某个微信联系人的聊天记录、将聊天记录转成可喂 AI 的格式、或分析特定微信好友对话内容时使用。仅支持 Windows 平台，仅适用于用户本机、用户自己的账号数据，需要微信已登录并保持运行。
---

# 蒸馏人 (distill-person)

从**本机微信 4.x** 的本地加密数据库里，把某个联系人的全部聊天记录提取出来，解密、清洗、转成干净的 JSON，方便喂给 AI（做聊天画像、关系分析、蒸馏成「某个人」的 skill 等）。

> ⚠️ 隐私声明：本 skill 只读取**你自己本机、你自己登录的微信**数据库，且运行期间**不联网、不上传任何数据**。导出的 JSON 可能包含真实隐私，请自行妥善处置，发布前务必脱敏。

## 能力边界

- ✅ 支持微信 4.0.x ~ 4.1.x（Windows, `Weixin.exe`）
- ✅ SQLCipher 4 / WCDB 加密库解密（零第三方密码学库依赖，调用系统 `bcrypt.dll`）
- ✅ 自动从运行中的微信进程内存提取密钥（只读扫描，不注入、不修改进程）
- ✅ contact.db 联系人检索 + message 库消息合并 + 发送者还原
- ❌ 不支持 macOS / Linux / 微信 UWP / 企业微信
- ❌ 不支持从他人设备或云端抓取

## 工作原理（简要）

1. **探测**微信数据目录（默认 `~\Documents\xwechat_files\<your_wxid>\db_storage`）。
2. **提密钥**：在 `Weixin.exe` 进程内存中定位 `com.Tencent.WCDB.Config.Cipher` 字符串，沿对象链取出 blob，用固定 XOR 掩码解码得到 `x'<key><salt>'`，再用 HMAC-SHA512 逐库校验。
3. **解密**：对每个 `.db` 用 `bcrypt.dll` 做 AES-256-CTR 解密（SQLCipher 4 参数：KDF=HMAC-SHA512、PBKDF2 迭代 256000、page size 4096）。
4. **查人**：在 `contact.db` 的 `Friend` / `ChatRoom` 表里按备注名/昵称匹配，得到 `wxid`。
5. **导出**：在 `message_*.db` 里按 `wxid` 找对应消息表，合并 `message_0..n`，还原发送者（`me` / 对方），输出统一 JSON。

## 使用方式

### 一键蒸馏

```bash
python distill_person.py distill --target "张三" --output chat.json
```

会自动完成：探测目录 → 提密钥 → 解密 → 查人 → 导出。

### 分步使用

```bash
# 1) 只提取密钥（会写入 all_keys.json，含解密密钥，请勿外传）
python distill_person.py extract --db-dir "C:\Users\You\Documents\xwechat_files\wxid_example_me\db_storage" --output all_keys.json

# 2) 用已有密钥文件，蒸馏指定联系人
python distill_person.py distill --target "张三" --keys all_keys.json --output zhangsan.json

# 3) 仅解密某个库，不导出聊天
python distill_person.py decrypt --keys all_keys.json --db "C:\...\db_storage\message\message_0.db" --output message_0_dec.db
```

### 参数说明

| 参数 | 含义 |
| --- | --- |
| `--target` | 联系人备注名/昵称（如 `张三`），也接受直接传 `wxid_xxx` |
| `--output` | 输出 JSON 路径 |
| `--keys` | 已有密钥文件路径（跳过内存扫描） |
| `--db-dir` | 微信数据库根目录（不传则自动探测） |
| `--no-key` | 仅解密已有解密库（调试用） |

## 输出 JSON 格式

```json
{
  "meta": {
    "contact_name": "张三",
    "wxid": "wxid_example001",
    "alias": "example_alias",
    "total_messages": 12345,
    "time_range": {"start": "2024-03-01 10:00:00", "end": "2024-08-01 18:30:00"},
    "sender_stats": {"张三": 6789, "me": 5567},
    "type_stats": {"text": 9801, "emoji": 1234, "image": 880, "system": 430}
  },
  "messages": [
    {"time": "2024-03-01 10:00:00", "sender": "张三", "type": "text", "content": "你好"},
    {"time": "2024-03-01 10:00:05", "sender": "me", "type": "text", "content": "在吗"}
  ]
}
```

每条消息固定 4 个字段：`time`（本地时间字符串）、`sender`（`张三` / `me`）、`type`（`text`/`emoji`/`image`/`voice`/`system` 等）、`content`（正文；非文本类型为空或占位）。

## 依赖与环境

- **系统**：Windows 10/11，已安装并登录微信 4.x（`Weixin.exe` 正在运行）
- **Python**：3.8+，标准库即可（`ctypes` / `hashlib` / `hmac` / `sqlite3` / `json`）
- **无第三方包**：AES 解密直接调用 Windows `bcrypt.dll`

## 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `找不到微信进程` | 微信没开 | 启动并登录微信后再跑 |
| `密钥校验全部失败` | 微信版本过新/过旧，XOR 掩码失效 | 提 issue 附微信版本号 |
| `未找到联系人` | 备注名不匹配 | 用 `--target` 直接传 `wxid_xxx` |
| `解密报错 0x......` | `bcrypt.dll` 调用失败 | 确认是 Windows 且非精简版系统 |

## 参考

- 微信数据库结构见 `references/wechat_db_structure.md`
- 核心算法来自开源项目 `TANGandXUE/wcdb-key-tool`（内存密钥提取），本 skill 在其基础上整合了解密、查人、导出全流程。
