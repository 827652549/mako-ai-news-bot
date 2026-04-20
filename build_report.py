import json, os

today = "2026-04-20"

elements = []

# Topic 1: Claude 生态
claude_items = [
    (
        "Claude Code v2.1.113：CLI 原生二进制、/ultrareview、沙盒域名过滤",
        "https://github.com/anthropics/claude-code/releases/tag/v2.1.113",
        "/ultrareview 命令与 sandbox.network.deniedDomains 配置同步上线，可直接提升代码审查效率与本地沙盒安全性",
    ),
    (
        "Claude Code v2.1.111：Opus 4.7 xhigh + Auto 模式 + /effort + /less-permission-prompts",
        "https://github.com/anthropics/claude-code/releases/tag/v2.1.111",
        "Opus 4.7 xhigh 正式可用，Max 订阅者可用 /effort 滑块精控推理深度与 token 成本；/less-permission-prompts 大幅减少交互摩擦",
    ),
    (
        "Introducing Claude Design by Anthropic Labs",
        "https://www.anthropic.com/news/claude-design-anthropic-labs",
        "Anthropic Labs 首个独立产品线，Claude 进入设计工具赛道，预示未来 API 侧可能开放设计生成能力",
    ),
    (
        "Introducing Claude Opus 4.7",
        "https://www.anthropic.com/news/claude-opus-4-7",
        "Anthropic 旗舰模型升级公告，了解新能力边界有助于判断哪些编程任务可交由 Claude Code 自动化处理",
    ),
]

def build_section(header, items):
    lines = [header]
    for title, url, why in items:
        if len(title) > 40:
            title = title[:39] + "…"
        lines.append(f"· [{title}]({url})")
        lines.append(f"  _为什么关注：{why}_")
    return "\n".join(lines)

claude_md = build_section("**🔧 Claude 生态**", claude_items)
elements.append({"tag": "div", "text": {"tag": "lark_md", "content": claude_md}})
elements.append({"tag": "hr"})

# Topic 2: AI 工具发现
tools_items = [
    (
        "codeburn：Claude Code / Codex / Cursor 成本可观测 TUI 仪表盘",
        "https://github.com/getagentseal/codeburn",
        "2871 star，7 天新项目。AI coding token 费用黑盒问题首个可视化方案，直接影响团队 AI 工具选型决策",
    ),
    (
        "design-extract：一行命令提取任意网站完整设计系统 + MCP server",
        "https://github.com/Manavarya09/design-extract",
        "1089 star，5 天新项目。前端高频痛点工具，内置 MCP server 可直接插入 Claude Code/Cursor/Windsurf",
    ),
    (
        "agentic-stack：可移植 .agent/ 文件夹，兼容多 IDE",
        "https://github.com/codejunkie99/agentic-stack",
        "577 star，5 天新项目。IDE 切换零迁移成本的系统性解法，与 MCP 互补，代表 agent 工具链标准化趋势",
    ),
    (
        "Running 10 Claude Code Instances in Parallel — git worktree 隔离",
        "https://dev.to/kanta13jp1/running-10-claude-code-instances-in-parallel-git-worktree-isolation-design-2n18",
        "并发 Claude Code + git worktree 完整隔离架构实战，P6+ 工程师面试中 AI 工程化能力的热点话题",
    ),
    (
        "Memorix：让多 AI Agent 共享持久化项目记忆",
        "https://dev.to/_2340687267e5cacfe32da1/memorix-give-your-ai-coding-agents-shared-persistent-project-memory-1pk2",
        "多 agent 共享记忆是 AI 工程化下一阶段核心挑战，今日出现的具体落地方案，值得跟踪",
    ),
]
tools_md = build_section("**🛠️ AI 工具发现**", tools_items)
elements.append({"tag": "div", "text": {"tag": "lark_md", "content": tools_md}})
elements.append({"tag": "hr"})

# Topic 3: 国内 AI 动态
china_items = [
    (
        "腾讯开源混元世界模型 2.0：一句话生成 3D 世界，兼容游戏引擎",
        "https://www.leiphone.com/category/industrynews/OC4vHTtPgnG8nNzl.html",
        "腾讯云 AI 战略里程碑，开源策略 + 游戏引擎兼容 = 直接冲击 Unity/Unreal 的 AI 工具链生态",
    ),
    (
        "阿里发布世界模型 HappyOyster，与 Google Genie3 正面竞争",
        "https://www.leiphone.com/category/industrynews/lgwQMCTn55AMCLBX.html",
        "阿里云在具身智能/世界模型赛道与 Google 直接叫板，影响国内 AI 基础设施演进方向",
    ),
    (
        "阿里喊出 AI 云五年目标 1000 亿美元：底气还是画饼？",
        "https://www.leiphone.com/banner/homepageUrl/id/3422",
        "阿里云的 AI 战略规模直接影响国内 AI coding 工具本地化部署的投入力度与节奏",
    ),
]
china_md = build_section("**🏢 国内 AI 动态**", china_items)
elements.append({"tag": "div", "text": {"tag": "lark_md", "content": china_md}})
elements.append({"tag": "hr"})

# Topic 4: Spec 驱动开发
spec_items = [
    (
        "Gate Zero：在 prompt 变成 spec 前阻断不可证伪的需求描述",
        "https://dev.to/amanbhandari/gate-zero-stop-unfalsifiable-prompts-before-they-canonicalize-as-specs-29n2",
        "直击 SDD 最关键的质量门控问题，与面试中被问到的「如何保证 AI 生成代码质量」高度相关",
    ),
    (
        "Understanding SDD: Kiro, Spec-Kit, and Tessl（Martin Fowler）",
        "https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html",
        "Martin Fowler 出品，HN 128 points，目前最系统的 SDD 工具链横评，面试必备背景知识",
    ),
]
spec_md = build_section("**📐 Spec 驱动开发**", spec_items)
elements.append({"tag": "div", "text": {"tag": "lark_md", "content": spec_md}})
elements.append({"tag": "hr"})

# Footer note
note_text = (
    "数据来源：GitHub Releases · GitHub Trending · GitHub Search · HN Algolia"
    " · Anthropic News · 雷峰网 · DEV Community"
    "｜修改关注维度：编辑仓库中的 watch.yaml\n"
    "⚠️ 跳过：GitHub API Releases（403，未配置 GITHUB_TOKEN）"
    " · smarthey.com（未抓取）· HN（24h 内无命中条目）"
    " · 注：Claude 生态与国内 AI 动态部分内容来自 72h 内（Apr 17–18），超出严格 24h 窗口"
)
elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note_text}]})

card_data = {
    "msg_type": "interactive",
    "card": {
        "header": {
            "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {today}"},
            "template": "blue",
        },
        "elements": elements,
    },
}

os.makedirs(".ai-news-bot", exist_ok=True)
with open(".ai-news-bot/latest-report.json", "w", encoding="utf-8") as f:
    json.dump(card_data, f, ensure_ascii=False, indent=2)

# Validate
with open(".ai-news-bot/latest-report.json", "r", encoding="utf-8") as f:
    json.load(f)

print("JSON written and validated successfully")
print(f"Topics: {len([e for e in elements if e.get('tag') == 'div'])}")
print(f"File size: {os.path.getsize('.ai-news-bot/latest-report.json')} bytes")
