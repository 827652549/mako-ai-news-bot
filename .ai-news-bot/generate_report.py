"""
AI 资讯速报生成脚本 — 2026-04-30
数据已通过 Agent 抓取并过滤，此脚本负责构建卡片并写入文件。
"""
import json
import os
from datetime import datetime, timezone

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def truncate_title(title, max_len=30):
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [' ', '·', '|', '：', '，', '-']:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + '…'
    return cut + '…'


elements = []

results = {
    "claude_ecosystem": [
        {
            "title": "Claude Code v2.1.123 · OAuth 认证修复",
            "url": "https://github.com/anthropics/claude-code/releases/tag/v2.1.123",
            "why": "修复 CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 时 OAuth 401 无限重试循环，影响所有禁用实验性 Beta 的用户，当日必更",
        },
        {
            "title": "飞书 CLI v1.0.22 · Task App 成员管理",
            "url": "https://github.com/larksuite/cli/releases/tag/v1.0.22",
            "why": "飞书官方 CLI 新增任务 App 成员管理 API，对接入飞书自动化工作流的开发者有直接价值",
        },
    ],
    "ai_tools_discovery": [
        {
            "title": "HERMES.md commit 消息触发超额计费",
            "url": "https://github.com/anthropics/claude-code/issues/53262",
            "why": "commit 消息含 HERMES.md 字符串即路由到额外计费，Max 20x 用户实测额外扣 $200+，所有 Claude Code 用户必读",
        },
        {
            "title": "warp · Agentic 终端 · 今日 12K⭐",
            "url": "https://github.com/warpdotdev/warp",
            "why": "面向 Agentic 开发的终端环境今日冲上 Trending 首位，是 Claude Code 工具链生态最受瞩目的竞品/补充工具",
        },
        {
            "title": "mattpocock/skills · 今日 7K⭐ 暴涨",
            "url": "https://github.com/mattpocock/skills",
            "why": "TypeScript 布道者 Matt Pocock 发布 .claude/skills 工程集合，直接可复用到 TS 项目，Claude Code Skills 的 KOL 首次大规模落地",
        },
        {
            "title": "obra/superpowers · 今日 1.6K⭐",
            "url": "https://github.com/obra/superpowers",
            "why": "持续受关注的 Agentic 开发方法论框架，与 Claude Code Skills 体系互补，面试讨论 Agent 架构时的有力参考",
        },
        {
            "title": "Anthropic Champion Kit 企业推广指南",
            "url": "https://code.claude.com/docs/en/champion-kit",
            "why": "Anthropic 官方企业推广 Claude Code 配套资料，含说服技巧和最佳实践，适合向团队推介 AI 编程工具时参考",
        },
    ],
    "china_ai_trends": [
        {
            "title": "丰e足食×阿里云千问 无人零售标杆",
            "url": "https://www.leiphone.com/category/industrynews/lERHmOr6zFZwPr73.html",
            "why": "阿里云千问首个无人零售行业标杆落地（18 万货柜），是阿里云 AI 商业化在垂直场景深度整合的重要案例",
        },
        {
            "title": "荣威×火山引擎 全球首款 AI 原生汽车",
            "url": "https://www.leiphone.com/category/transportation/K7ZQKtmfjAKjmf9X.html",
            "why": "字节豆包大模型延伸至汽车车机场景，火山引擎 AI 商业化从内容生态向硬件终端拓展的关键节点",
        },
        {
            "title": "WorkBuddy 接入腾讯文档 AI 工作流闭环",
            "url": "https://www.smarthey.com/detail/486194004772.html",
            "why": "腾讯文档实现 AI「找-用-存」工作流闭环，是国内办公 SaaS AI-native 改造的典型案例，可与飞书策略对比参考",
        },
        {
            "title": "阶跃星辰 Step Image Edit 2 · 3.5B 登顶",
            "url": "https://www.smarthey.com/detail/917123402289.html",
            "why": "国内 3.5B 轻量大模型 0.5s 出图登顶轻量榜，是面试「AI 推理效率与小参数模型」话题的直接佐证",
        },
    ],
    "mcp_ecosystem": [
        {
            "title": "modelcontextprotocol/registry 官方注册表",
            "url": "https://github.com/modelcontextprotocol/registry",
            "why": "MCP 官方社区服务器注册表，是发现生产级 MCP 服务器最权威的入口，工程化集成前必查资源",
        },
        {
            "title": "anything-analyzer · 抓包+MCP+AI 分析",
            "url": "https://github.com/Mouseww/anything-analyzer",
            "why": "18 天 2K⭐ 新项目，将 MITM 抓包和 AI 协议分析通过 MCP 接入 IDE Agent，是 MCP 在开发工具链中的新颖应用",
        },
        {
            "title": "Flowise MCP RCE CVE-2026-40933 分析",
            "url": "https://dev.to/tokenmixai/flowise-mcp-rce-what-cve-2026-40933-teaches-about-agent-security-1p6g",
            "why": "Flowise MCP 服务器 RCE 漏洞揭示 Agent 工具链安全盲区，MCP 工程化落地必须考虑的执行边界问题",
        },
        {
            "title": "task-orchestrator · Schema 强制 Agent 输出",
            "url": "https://github.com/jpicklyk/task-orchestrator",
            "why": "用 MCP 服务端 Schema 约束 Agent 输出格式、依赖图和质量门控，是 spec-driven AI 开发的工具化落地实践",
        },
        {
            "title": "MCP server 替代 Agent 自检的设计思路",
            "url": "https://dev.to/tomfweb/why-we-built-an-mcp-server-for-website-health-data-instead-of-letting-agents-run-the-checks-42jo",
            "why": "说明为何用 MCP 封装数据接口比让 Agent 自主执行检查更安全可控，对 MCP 架构设计决策有直接参考价值",
        },
    ],
    "spec_driven_dev": [
        {
            "title": "94% SKILL.md 实现缺失 spec 核心模式",
            "url": "https://dev.to/moonrunnerkc/94-of-published-skillmd-files-skip-the-specs-two-most-basic-patterns-oo0",
            "why": "分析公开 SKILL.md 规范符合度，指出大多数实现缺失两个核心 spec 模式，是理解 Claude Code Skills 规范设计质量的重要参考",
        },
    ],
}

topic_labels = {
    "claude_ecosystem": "🔧 Claude 生态",
    "ai_tools_discovery": "🛠️ AI 工具发现",
    "china_ai_trends": "🏢 国内 AI 动态",
    "mcp_ecosystem": "🔌 MCP 生态",
    "spec_driven_dev": "📐 Spec 驱动开发",
}

for topic_id, items in results.items():
    if not items:
        continue
    lines = [f"**{topic_labels[topic_id]}**"]
    for item in items:
        t = truncate_title(item["title"])
        lines.append(f"· [{t}]({item['url']})")
        lines.append(f"  _{item['why']}_")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    elements.append({"tag": "hr"})

skip_msg = "claude.ai/blog（未抓取）、openclawapi.org（未抓取）、HN MCP 关键词（无结果）"
elements.append({
    "tag": "note",
    "elements": [{
        "tag": "plain_text",
        "content": (
            "数据来源：GitHub Releases · GitHub Trending · HN Algolia · "
            "DEV Community · 雷峰网 · Smarthey｜修改关注维度：编辑 watch.yaml\n"
            f"⚠️ 跳过：{skip_msg}"
        ),
    }],
})

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
report_path = ".ai-news-bot/latest-report.json"

with open(report_path, "w", encoding="utf-8") as f:
    json.dump(card_data, f, ensure_ascii=False, indent=2)

# Verify it parses cleanly
with open(report_path, "r", encoding="utf-8") as f:
    json.load(f)

print(f"✅ Report written to {report_path}")

# Build seen.json from all output URLs
seen_urls = {}
for topic_id, items in results.items():
    for item in items:
        url = item["url"]
        seen_urls[url] = {
            "title": item["title"],
            "url": url,
            "first_seen": today,
            "last_seen": today,
            "count": 1,
        }

seen_path = ".ai-news-bot/seen.json"
with open(seen_path, "w", encoding="utf-8") as f:
    json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)

print(f"✅ seen.json written with {len(seen_urls)} entries")
