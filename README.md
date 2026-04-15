# AI 资讯日报机器人

每天自动抓取 Claude 生态、AI 工具、国内云厂商动态，通过飞书卡片推送到群里。

## 架构

```
Claude Code Routine（每天 8:00）
    │
    ├── 读取 watch.yaml（关注维度配置）
    ├── 抓取各信源（GitHub / HN / 雷峰网 / DEV Community）
    ├── 过滤去重 + 添加"为什么关注"
    └── 写入 .ai-news-bot/latest-report.json → git push
                │
                └── GitHub Actions 触发
                        └── curl → 飞书 Webhook → 飞书群卡片
```

## 文件说明

| 文件 | 用途 |
|---|---|
| `watch.yaml` | 关注维度配置，直接编辑即可修改监控内容 |
| `routine_prompt.md` | Claude Code Routine 的完整 Prompt，粘贴到 Routine 创建界面 |
| `.github/workflows/notify-feishu.yml` | GitHub Actions：监听 report 文件变更，推送飞书 |
| `.ai-news-bot/latest-report.json` | Routine 每次运行写入的飞书卡片 payload（自动生成）|

## 信息源

| 信源 | 覆盖内容 | 访问方式 |
|---|---|---|
| GitHub Releases RSS | Claude Code / SDK 版本更新 | GitHub API |
| GitHub Trending | 快速增长的新工具 | WebFetch |
| GitHub Search API | 近期新出现的 AI 工具 | GitHub API（需 Token）|
| HN Algolia API | 英文社区热点讨论 | 官方 API |
| 雷峰网 | 国内 AI 企业动态 | WebFetch / Jina Reader |
| DEV Community | openspec / MCP 等工具讨论 | 官方 API |

## 快速开始

### 1. Fork 或 clone 这个仓库

### 2. 配置飞书机器人

在飞书群 → 设置 → 机器人 → 添加自定义机器人，复制 Webhook URL。

### 3. 配置 GitHub Secrets

仓库 Settings → Environments → 新建 `prod` 环境 → Secrets → 添加：

```
FEISHU_WEBHOOK_URL = https://open.feishu.cn/open-apis/bot/v2/hook/...
```

### 4. 创建 Claude Code Routine

进入 [claude.ai/code/routines](https://claude.ai/code/routines) → New Routine：

- **Prompt**：将 `routine_prompt.md` 中"PROMPT 正文"整段粘贴
- **Repository**：选择本仓库
- **Environment Variables**：添加 `GITHUB_TOKEN`（GitHub PAT，`public_repo` 权限）
- **Trigger**：Schedule → Daily → 08:00 Asia/Shanghai

### 5. 点击 Run now 验证

飞书群收到卡片即为成功。

## 修改关注维度

直接在 GitHub 上编辑 `watch.yaml`，保存后下次 Routine 运行自动生效，无需改动 Prompt 或 Actions。

**新增关注方向**：复制一个 topic 块，修改 `id`、`name`、`keywords`。

**关闭某个信源**：注释掉对应的 `url` 或 `queries` 行（YAML 用 `#` 注释）。

**调整过滤强度**：修改 `filters.lookback_hours`（默认 24）或 `filters.max_items_per_topic`（默认 5）。

## 常见问题

**飞书收不到消息**

1. 检查 GitHub Actions 是否触发（仓库 → Actions 页）
2. 查看 Actions 日志里 curl 的返回值：
   - `{"StatusCode":0}` → 成功
   - `{"code":9499}` → payload JSON 格式有误（检查 Routine session log）
   - `{"code":19021}` → Webhook 签名校验失败（检查机器人安全设置）

**GitHub Actions 没有触发**

确认 workflow 文件的 `paths` 配置与实际写入路径一致（`.ai-news-bot/latest-report.json`）。

**GitHub Search API 报 Rate Limit**

在 Routine 环境变量里配置 `GITHUB_TOKEN`，未认证限额 60次/小时，认证后 5000次/小时。

**某个信源一直 403**

在 `watch.yaml` 对应 source 下添加 `use_jina: true`，切换为 Jina Reader 抓取。
