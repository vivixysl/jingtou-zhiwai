# 《镜头之外》V0.4｜Render 私人 iPhone 线上版

**当前状态：代码与部署配置已备好；只有在你自己的 Render 账户成功部署，才会出现真正可用的网址。**

## 最简单的上线方式：GitHub 私人仓库 + Render Blueprint

1. 登录 [GitHub](https://github.com/new)，建立一个 **Private** 仓库，例如 `jingtou-zhiwai-private`。建议新建仓库，不要覆盖之前 Codex 的项目。
2. 解压本 ZIP，**把里面所有文件、`assets` 文件夹、`data` 文件夹、`render.yaml` 放在仓库根目录**。GitHub 网页端 Add file → Upload files 可以上传整个解压后的项目；如果网页无法上传空 `data` 目录没关系，它会在服务器启动时创建。不要把外层 ZIP 文件本身上传当源代码。
3. 登录 [Render Dashboard](https://dashboard.render.com)，点 **New → Blueprint**，连接刚才的 GitHub 私人仓库并选中它。
4. Render 识别仓库根目录中的 `render.yaml` 后，会请求你填两个**私密变量**（不要发给 ChatGPT、不要提交到 GitHub）：
   - `OPENAI_API_KEY`：之前 OpenAI Platform 的 API key，只要仍有效就可以继续用。
   - `GAME_PIN`：你自己设置的独特强密码，**至少16位**，建议24位以上。此密码用于 iPhone 登录游戏。
   - `SESSION_SECRET` 不需要手填，Blueprint 会自动生成一串随机字符串。
5. **确认价格再创建**：配置中使用付费 512 MB Web Service + 1GB 持久化磁盘，不能选 free。当前官网标价约 **$7/月 + $0.25/月（1GB磁盘）**，再加 OpenAI API 实际用量及其他可能费用，最终以 Render 付款页为准。每月预算/费用提醒建议在两处账户都设置。
6. Render 完成部署后会给你一个 `https://……onrender.com` 地址。手机 Safari 打开 → 输入 `GAME_PIN` → Safari 分享 → 添加到主屏幕。
7. 平时点开微信 / 切换七部手机不消耗新的 API 调用；只在点「继续一天」时调用 OpenAI API，并把新事件记录到持久化 SQLite 数据库里。

## 必须注意

- **不要把 OpenAI Key / GAME_PIN 提交到 GitHub、发到聊天或写进 HTML。** 只在 Render 创建 Blueprint 时填写私密变量。
- GitHub 仓库设成 **Private**。即使是公开作品也不要上传任何真实聊天或个人隐私。
- Render 磁盘必须挂载到 `/app/data`。程序在 Render 上会检查挂载，避免误用临时空间导致丢档。
- Render 持久化磁盘 ≠ 备份。每次重要推进后点游戏内的「导出世界事件备份 JSON」。该 JSON 并非完整数据库镜像，迁移/完整恢复需要额外导出 SQLite 与用户上传的头像；未来可增加正式的备份恢复功能。
- 默认**单人单存档、只跑一台实例**；不要开多个副本。此版为观察档，AI 续写内容会遵循七人关系与事件账本。
- Render 网站登录与 ChatGPT 中的 Render 插件连接是不同操作；你可以只在 Render 网页部署，不必须把它连接到 ChatGPT。
- 这个安装包**并未直接替你创建 Render 服务或完成线上付费 API 调用**。

## 出错时查哪里

- `Deploy failed`：Render → Service → Logs，核对私密变量是否已填，付费磁盘是否按 `render.yaml` 建立，蓝图是否读到文件夹根目录。
- `/api/health`：公开、不含私密信息的健康检查应返回 `{"ok": true}`。
- 登录失败：核对输入的 `GAME_PIN`；它不等于 OpenAI API Key。
- AI 失败：检查 OpenAI 额度、API Key 和模型可用性；有时模型生成不合格 JSON，需要重试。
- iPhone 关闭网页后重新进入应保留世界事件和手账；为了保险仍建议定期下载备份。

## 架构

iPhone Safari (HTTPS) → Render Docker Python Server (PRIVATE COOKIE LOGIN) → OpenAI Responses API；`/app/data` Render persistent disk 保存 `world.sqlite3` 与用户上传头像。所有公开可见的真人名和关系均为**虚构平行世界创作**。
