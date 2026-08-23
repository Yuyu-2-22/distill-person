# 蒸馏人 (distill-person)

> 一句话：**把你微信里某个人的聊天记录，从手机/电脑本地加密数据库里「蒸馏」成一份干净的 JSON 文件，方便给 AI 分析、画像或做成「某个人」的专属 Skill。**

---

## 这个 Skill 是干什么的？（用途）

微信 4.x 把聊天记录存在你电脑本地的加密数据库里（SQLCipher/WCDB 加密），普通人打不开、也导不出来。

「蒸馏人」要解决的就是这个痛点：**把某个指定联系人的全部聊天记录，自动解密、清洗、整理成一份结构化 JSON**，让你能直接拿去：

- 🤖 **喂给 AI 做分析**：聊天画像、关系分析、情绪时间线、高频话题统计
- 🧬 **蒸馏成「某个人」的 Skill**：把一个人的说话习惯、口头禅、性格特征提取出来，做成可复用的 AI 人格包（这正是「蒸馏人」名字的由来）
- 📦 **备份 / 迁移**：把自己的重要对话导出为可读、可检索的纯文本格式
- 🔍 **自查**：回顾某段关系的沟通模式、谁更主动、话题分布

## 它具体做什么？（作用）

一条命令，自动跑完下面整条流水线：

1. **探测**微信数据目录（默认 `~\Documents\xwechat_files\<你的wxid>\db_storage`）
2. **提密钥**：在正在运行的微信进程内存里，定位 `com.Tencent.WCDB.Config.Cipher`，沿对象链取出密钥并用 HMAC 校验（**只读扫描，不注入、不修改微信进程**）
3. **解密**：对每个 `.db` 调用系统 `bcrypt.dll` 做 AES-256 解密（SQLCipher 4 参数）
4. **查人**：在 `contact.db` 里按备注名/昵称匹配出对方的 `wxid`
5. **导出**：在 `message_*.db` 里按 `wxid` 找对应消息表，合并多个分片，还原「我 / 对方」发送者，输出统一 JSON

### 核心特性

- ✅ 支持微信 **4.0.x ~ 4.1.x**（Windows，`Weixin.exe`）
- ✅ **零第三方依赖**：AES 解密直接调用 Windows 自带的 `bcrypt.dll`，只需 Python 3.8+
- ✅ **本地离线**：运行时不联网、不上传任何数据
- ✅ 自动处理多消息分片、发送者 rowid 映射错乱等问题
- ❌ 不支持 macOS / Linux / 微信 UWP / 企业微信
- ❌ 不支持从他人设备或云端抓取（只处理你自己本机、自己登录的微信）

---

## 怎么用？

### 环境要求

- Windows 10/11，已安装并登录微信 4.x（`Weixin.exe` **正在运行**）
- Python 3.8+（仅标准库）
- **不需要**安装任何第三方包

### 一键蒸馏

```bash
python scripts/distill_person.py distill --target "张三" --output chat.json
```

会自动完成：探测目录 → 提密钥 → 解密 → 查人 → 导出。

### 分步使用

```bash
# 1) 只提取密钥（会写入 all_keys.json，含解密密钥，请勿外传）
python scripts/distill_person.py extract --db-dir "C:\Users\You\Documents\xwechat_files\wxid_example_me\db_storage" --output all_keys.json

# 2) 用已有密钥文件，蒸馏指定联系人
python scripts/distill_person.py distill --target "张三" --keys all_keys.json --output zhangsan.json

# 3) 仅解密某个库，不导出聊天
python scripts/distill_person.py decrypt --keys all_keys.json --db "C:\...\db_storage\message\message_0.db" --output message_0_dec.db
```

### 参数说明

| 参数 | 含义 |
| --- | --- |
| `--target` | 联系人备注名/昵称（如 `张三`），也接受直接传 `wxid_xxx` |
| `--output` | 输出 JSON 路径 |
| `--keys` | 已有密钥文件路径（跳过内存扫描） |
| `--db-dir` | 微信数据库根目录（不传则自动探测） |
| `--no-key` | 仅解密已有解密库（调试用） |

### 输出 JSON 格式

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

每条消息固定 4 个字段：`time`（本地时间）、`sender`（对方昵称 / `me`）、`type`（`text`/`emoji`/`image`/`voice`/`system`…）、`content`（正文）。

> 更完整的数据库结构、字段含义见 [`SKILL.md`](SKILL.md) 与 [`references/wechat_db_structure.md`](references/wechat_db_structure.md)。

---

## ⚠️ 隐私与合规

- 本工具**只读取你自己本机、自己登录的微信**数据，运行期间**不联网、不上传任何内容**。
- 导出的 JSON **可能包含真实聊天隐私**，请自行妥善处置；**发布或分享前务必脱敏**（联系人姓名、wxid、消息内容等）。
- 请遵守微信软件许可及所在地区法律法规，仅用于你有权处理的数据。
- 本仓库已**完全脱敏**：所有示例中的姓名、wxid、消息内容均为占位数据，不含任何真实个人信息。

---

## 怎么看到 / 获取这个项目？

### 在线查看（最方便）

直接打开仓库主页即可看到全部文件、README 和源码：

👉 **https://github.com/Yuyu-2-22/distill-person**

- 点 `README.md` / `SKILL.md` 能在网页里直接看内容
- 点 **Code → Open with GitHub Desktop** 可用桌面客户端打开
- 点 **Code → Download ZIP** 可下载整个项目压缩包

### 命令行克隆到本地

```bash
git clone https://github.com/Yuyu-2-22/distill-person.git
cd distill-person
```

### 作为 CodeBuddy Skill 安装

把本仓库放到 CodeBuddy 的 skills 目录即可被识别（目录需含 `SKILL.md`）：

```
~/.codebuddy/skills/distill-person/     # 用户级
<项目>/.codebuddy/skills/distill-person/ # 项目级
```

---

## 致谢

核心密钥提取算法来自开源项目 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)，本仓库在其基础上整合了解密、查人、导出全流程。

## License

[MIT](LICENSE)
