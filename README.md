# 小红书图文工作台 MVP

这是一个本地运行的小红书内容工作台，帮助创作者按账号定位制作并发布图文笔记。当前主流程是：**账号定位 → 扫描热点 → 生成并编辑标题、正文和话题 → 调用生图接口 → 选择配图并预览 → 确认发布 → 记录与核验**。账号监测、发布记录和服务设置各自位于独立页面。

## 页面与流程

- **账号定位**：保存账号名称、赛道、定位、受众、语气、内容结构、边界、事实要求和常用话题。定位完成后解锁内容制作，每次变更产生新版本。
- **内容制作**：先按账号赛道或自定义主题扫描热点，再选择参考生成文案；可手动修改标题、正文和话题，配置生图模型后生成并选择最多 9 张图片，实时预览图文笔记。
- **账号监测**：从当前连接器读取登录状态、账号资料、可读互动数据并保存快照。当前连接器不提供播放量。
- **发布记录**：查看图文素材、定位版本、来源、提交状态和人工核验结果。
- **服务设置**：配置小红书 MCP、文案模型、生图模型，并查看热点来源状态。文案模型支持多套 OpenAI 兼容方案，可配置 DeepSeek、通义千问、OpenAI、AixHan 中转或自定义接口，并调整首选与备用顺序。
- **ToAPIs 媒体生成**：在服务设置填写 ToAPIs API Key；在内容制作页读取可用模型和参数说明，再提交图片或视频任务。结果保存到本机素材库，图片可加入笔记配图，视频可预览和下载。

## 热点来源

热点扫描是用户主动触发的一次检索，不是持续后台监控，也不代表“全网完整覆盖”。结果会列出来源、摘要、时间和链接；用户勾选后才会传给文案模型作为参考。

| 来源 | 当前接入方式 | 边界 |
| --- | --- | --- |
| AIHOT | 只读请求 `hot`、`latest` 和按主题 `search` | 当前针对 AI 资讯；匿名读取不代表所有用途均获许可 |
| 网页、GitHub、YouTube、B站、V2EX | Agent-Reach 默认渠道 | 受各渠道公开内容、安装状态和搜索索引限制 |
| Reddit | Agent-Reach `public-web` 搜索 | 公开网页结果，不保证站内排序、最新状态或完整内容 |
| X / Twitter | Agent-Reach `public-web` 搜索 | 当前联网测试没有返回可用内容，界面会显示该来源暂不可用；这不代表必须登录 |

Agent-Reach 按需搜索能力与小红书 MCP 登录会话相互独立。尚未启用的平台不会自动安装依赖或要求登录。它只用于读取公开内容，不会发帖、评论、点赞或私信。

AIHOT 服务条款允许个人非商业、公益非商业和组织内部使用免费；面向外部的商业用途需要事先取得 AIHOT 书面授权。详见 <https://aihot.news/terms>。AIHOT 原文和汇总内容均作为参考资料，发布前请自行核实。

## 启动

需要 Python 3.9–3.12；工作台后端使用 Python 标准库，不需要安装第三方 Python 包。Python 3.13 暂不支持，因为其已移除本项目当前使用的 multipart 标准库模块。

```bash
python3 app.py
```

浏览器打开 <http://127.0.0.1:8765>。服务只监听本机地址。账号、任务、监测快照和生成图片保存在 `data/`，此目录已加入 Git 忽略规则。

可用环境变量：

```bash
XHS_WORKBENCH_PORT=8765
XHS_WORKBENCH_DATA_DIR=/path/to/local-data
XHS_LLM_BASE_URL=https://api.openai.com/v1
XHS_LLM_MODEL=your-text-model
XHS_LLM_API_KEY=your-text-api-key
XHS_IMAGE_BASE_URL=https://api.openai.com/v1
XHS_IMAGE_MODEL=gpt-image-1
XHS_IMAGE_API_KEY=your-image-api-key
```

也可以在“服务设置”中填写文案模型配置。最多添加 8 套 OpenAI 兼容方案，每套可分别设置服务商、接口地址、Chat Completions 或 Responses 协议、模型 ID 和 API Key。DeepSeek、通义千问、OpenAI 与 AixHan 提供地址预设；自定义兼容服务填写自己的地址和协议。把低成本方案放在前面即可设为首选，后续方案按列表顺序作为备用。开启自动切换后，首选方案遇到 HTTP 429 限流时，工作台会尝试后续已配置 Key 的方案；其他服务错误、连接中断、超时或响应格式异常不会重发，以免结果不确定时重复计费。单独测试方案时只调用当前选中的方案。API Key 分别保存在工作台进程内存，不会写入 SQLite；重启后需要重新输入。

ToAPIs 图片和视频通过官方 Streamable HTTP MCP 工具接入，工作台使用 ToAPIs 专用 API Key；国际站和中国站可分别选择。生成前工作台会读取模型列表与参数说明，提交异步任务后按任务编号查询状态。生成结果会经 HTTPS 下载并保存为本机素材；下载失败时可以重新下载，不会重新提交生成任务。ToAPIs Key 只保存在工作台进程内存，重启后需要重新输入；服务区域保存在本机设置中。模型费用和可用范围以 ToAPIs 返回为准。

## 发布到小红书

发布模式默认为**模拟模式**。它只保存本地任务，不向小红书提交。接入本机 `xiaohongshu-mcp` 后，图文适配器调用 `publish_content`，传入标题、正文、图片路径列表和话题。发布前会要求用户确认；MCP 返回“已提交”不表示审核通过或笔记已经公开可见。

默认 MCP 地址为 `http://127.0.0.1:18060/mcp`，工作台只允许连接本机地址。MCP 登录会话由独立服务管理；发布前请核对实际登录账号。遇到超时或结果不明确时，任务进入待人工核验，不会自动重发。请先在小红书侧检查，再到“发布记录”登记结果。

## 当前范围

- 图文笔记制作仍是主流程；ToAPIs 可另外生成视频并在本机预览、下载。视频发布目前没有独立制作流程，工作台不会声称已自动完成视频发布。
- 文案和生图都由用户主动调用；生成内容是待审核草稿，不会自动核实事实或自动发布。
- 配图可使用现有 OpenAI 兼容 `images/generations` 服务或 ToAPIs 图片模型。ToAPIs 生成结果需使用受支持的 PNG、JPEG、WebP 格式；视频支持 MP4、MOV、WebM，最大 1 GB，媒体保存在本机。
- 当前单账号配置；账号定位与 MCP 实际登录身份分开保存，发布时由操作者核对。
- 热点来源不是全网监控；没有定时抓取、全量去重、趋势评分和通知。
- 监测数据取决于 MCP 当前能读取的内容；不提供播放量，也不能保证读取主页全部历史笔记。
- 不绕过验证码、安全校验、反爬或平台风控。上传、生成和发布的素材需确认拥有相应使用权。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用本地模拟 HTTP 服务和模拟发布器，不会发布到真实小红书。

## 代码结构

```text
app.py                  本机 HTTP 服务与工作流 API
workbench/store.py      SQLite 数据、图片素材、内容任务和监测快照
workbench/trends.py     AIHOT 与 Agent-Reach 只读热点适配器
workbench/generators.py OpenAI 兼容 Chat Completions / Responses 文案生成器
workbench/images.py     OpenAI 兼容生图接口
workbench/toapis.py     ToAPIs MCP 接入和生成媒体下载
workbench/publishers.py 模拟发布器与本机小红书 MCP 适配器
web/                    五个独立页面和工作流界面
data/                   本地数据库、图片和运行数据
docs/PRD.md             产品范围和验收标准
docs/ARCHITECTURE.md    模块边界和扩展方式
```
