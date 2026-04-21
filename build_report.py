#!/usr/bin/env python3
"""Build daily AI news Feishu card report."""
import json
import os

today = "2026-04-21"

# ── 1. Claude 生态 ──────────────────────────────────────────────
claude_items = [
    {
        "title": "Claude Code v2.1.116 发布",
        "url": "https://github.com/anthropics/claude-code/releases/tag/v2.1.116",
        "why": "单日更新：提升大 session /resume 速度与全屏滚动体验，新增内联 thinking spinner 进度和 /doctor 并发打开，可立即改善日常开发流畅度",
    },
    {
        "title": "Claude Token Counter, now with model comparisons",
        "url": "https://simonwillison.net/2026/Apr/20/claude-token-counts/",
        "why": "Simon Willison 新工具：支持跨模型 token 用量对比，HN 207 分热议，帮助工程师精确评估 context 消耗并优化多模型切换成本",
    },
    {
        "title": "66.5% of My Claude Code Tokens Were Wasted — A 200-Line...",
        "url": "https://dev.to/ji_ai/665-of-my-claude-code-tokens-were-wasted-a-200-line-wrapper-got-them-back-5bg9",
        "why": "实测 Claude Code token 浪费率达 66.5%，200 行封装层可显著回收浪费，对降低 AI coding 运行成本有直接参考价值",
    },
    {
        "title": "A Claude Code hook that warns you before calling a low-trust MCP...",
        "url": "https://dev.to/xkumakichi/a-claude-code-hook-that-warns-you-before-calling-a-low-trust-mcp-server-ckk",
        "why": "MCP 安全实践：低信任 MCP server 调用前触发告警 hook，正值 MCP 生态扩张期，此类安全配置可直接引用于生产环境",
    },
    {
        "title": "How I Run a 15-Repo Studio From One CLAUDE.md File",
        "url": "https://dev.to/raxxostudios/how-i-run-a-15-repo-studio-from-one-claudemd-file-4em3",
        "why": "单一 CLAUDE.md 统一管理 15 个仓库的工程实践，展示 Claude Code 多仓库 studio 模式配置方法，高级工程师必备工程素养",
    },
]

# ── 2. AI 工具发现 ──────────────────────────────────────────────
tools_items = [
    {
        "title": "openai/openai-agents-python · 905 stars today",
        "url": "https://github.com/openai/openai-agents-python",
        "why": "OpenAI 官方 multi-agent Python 框架单日涨 905 star，是 Claude Agent SDK 的直接竞品，理解其设计有助于 agent 方向面试对比答题",
    },
    {
        "title": "EvoMap/evolver · 585 stars today",
        "url": "https://github.com/EvoMap/evolver",
        "why": "基于基因进化协议 (GEP) 的 AI Agent 自进化引擎，单日 585 star，代表 agent 自优化演化方向，是前沿 agent 架构趋势的观察样本",
    },
    {
        "title": "zilliztech/claude-context · 74 stars today (MCP for Claude Code)",
        "url": "https://github.com/zilliztech/claude-context",
        "why": "Zilliz 出品 Claude Code MCP 插件，将完整代码库向量化为 agent context，直接解决大型仓库 context window 瓶颈，实用性极强",
    },
    {
        "title": "coreyhaines31/marketingskills · 354 stars today",
        "url": "https://github.com/coreyhaines31/marketingskills",
        "why": "面向 Claude Code 和 AI agent 的营销技能包（CRO/SEO/增长工程），说明 AI agent 工具链正在快速垂直细分，可关注生态演进方向",
    },
    {
        "title": "What Building with MCP Taught Me About Its Biggest Gap",
        "url": "https://dev.to/lovestaco/what-building-with-mcp-taught-me-about-its-biggest-gap-idl",
        "why": "MCP 实战踩坑总结（16 reactions），揭示协议落地最大痛点，是深度理解 MCP 局限性并在面试中展开讨论的第一手素材",
    },
]

# ── 3. 国内 AI 动态 ─────────────────────────────────────────────
china_items = [
    {
        "title": "国内云厂商涨价潮背后：有人提价，有人降价，各有盘算",
        "url": "https://www.leiphone.com/banner/homepageUrl/id/3423",
        "why": "国内云厂商价格策略分化，直接影响 AI 基础设施选型判断，了解阿里云/腾讯云定价博弈格局的重要背景",
    },
    {
        "title": "阿里喊出AI云五年干1000亿美元：底气还是画饼？",
        "url": "https://www.leiphone.com/banner/homepageUrl/id/3422",
        "why": "阿里云五年千亿美元目标，体现国内大厂 AI 基础设施军备竞赛烈度，是讨论国内 AI 生态格局时的关键数据点",
    },
    {
        "title": "郭达雅加入巨头背后：顶尖AI人才为何向大厂「回流」？",
        "url": "https://www.leiphone.com/category/industrynews/kBm2mpA7F6sJ65wA.html",
        "why": "AI 顶尖人才从创业公司向大厂回流趋势分析，反映国内 AI 人才市场竞争格局，与 AI 工程师职业规划方向直接相关",
    },
    {
        "title": "智元邓泰华宣布：具身智能行业进入「部署态」",
        "url": "https://www.leiphone.com/category/industrynews/wkokSPs28IPOhSAc.html",
        "why": "具身智能从研究进入规模化部署阶段，智能体技术边界向机器人延伸，了解 AI 应用形态演进方向的重要信号",
    },
]

# ── 构建 elements ───────────────────────────────────────────────
def format_item(item):
    title = item["title"]
    if len(title) > 40:
        title = title[:39] + "…"
    return f"· [{title}]({item['url']})\n  _为什么关注：{item['why']}_"


def build_section(emoji, name, items):
    lines = [f"**{emoji} {name}**"]
    for item in items:
        lines.append(format_item(item))
    return "\n".join(lines)


elements = []

# Claude 生态
elements.append({
    "tag": "div",
    "text": {
        "tag": "lark_md",
        "content": build_section("🔧", "Claude 生态", claude_items),
    },
})
elements.append({"tag": "hr"})

# AI 工具发现
elements.append({
    "tag": "div",
    "text": {
        "tag": "lark_md",
        "content": build_section("🛠️", "AI 工具发现", tools_items),
    },
})
elements.append({"tag": "hr"})

# 国内 AI 动态
elements.append({
    "tag": "div",
    "text": {
        "tag": "lark_md",
        "content": build_section("🏢", "国内 AI 动态", china_items),
    },
})
elements.append({"tag": "hr"})

# 底部 note（含跳过的信源说明）
elements.append({
    "tag": "note",
    "elements": [
        {
            "tag": "plain_text",
            "content": (
                "数据来源：GitHub Releases · GitHub Trending · HN Algolia · DEV Community · 雷峰网"
                "｜修改关注维度：编辑仓库中的 watch.yaml"
                "｜⚠️ 跳过：GitHub Search API（403 无 GITHUB_TOKEN）· "
                "Anthropic Blog（最新 Apr 17，超出 24h 窗口）· "
                "SDK Releases（最新 Apr 16，超出窗口）· "
                "Spec 驱动开发（24h 内 0 结果）"
            ),
        }
    ],
})

# ── 组装完整卡片 ────────────────────────────────────────────────
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

# ── 写入文件 ────────────────────────────────────────────────────
os.makedirs(".ai-news-bot", exist_ok=True)
output_path = ".ai-news-bot/latest-report.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(card_data, f, ensure_ascii=False, indent=2)

# 验证 JSON 可被正确解析
with open(output_path, "r", encoding="utf-8") as f:
    json.load(f)

print(f"✅ Report written to {output_path}")
print(f"   Topics: Claude 生态({len(claude_items)}) · AI工具发现({len(tools_items)}) · 国内AI动态({len(china_items)})")
