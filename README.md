# 蒸馏人 (distill-person)

从**本机微信 4.x**（`Weixin.exe`，Windows）的本地加密数据库里，把某个联系人的全部聊天记录提取出来，解密、清洗、转成可直接喂给 AI 的结构化 JSON。

- 支持微信 4.0.x ~ 4.1.x
- 零第三方依赖：AES 解密直接调用系统 `bcrypt.dll`，只需 Python 3.8+
- 只读扫描`Weixin.exe`进程内存提取 SQLCipher/WCDB 密钥，不注入、不修改进程

> ⚠️ **隐私警告**：本工具只读取你**自己本机、自己登录**的微信数据，运行时不联网、不上传任何内容。导出的 JSON 可能包含真实聊天隐私，请自行妥善处置，发布或分享前务必脱敏。

## 快速开始

```bash
# 一键蒸馏某个联系人
python scripts/distill_person.py distill --target "张三" --output chat.json
```

分步用法、参数说明、输出格式、数据库结构见 [`SKILL.md`](SKILL.md)。

## 能力边界

| 支持 | 不支持 |
| --- | --- |
| Windows + 微信 4.x | macOS / Linux / 微信 UWP |
| 本机已登录账号 | 他人设备 / 云端抓取 |
| 只读内存扫描提密钥 | 任何写入或修改微信进程的行为 |

## 致谢

核心密钥提取算法来自开源项目 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)，本仓库在其基础上整合了解密、查人、导出全流程。

## License

[MIT](LICENSE)
