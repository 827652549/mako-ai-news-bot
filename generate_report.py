#!/usr/bin/env python3
"""AI 资讯日报生成脚本 — 2026-05-15 运行结果"""

import json
import os
from datetime import datetime, timezone

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
elements = []
skipped_sources = []
hot_items = []  # count >= 3 的持续热点


def truncate_title(title, max_len=30):
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


# ── 收集所有 topic 数据 ──────────────────────────────────────────

results = {}
topic_labels = {
    "claude_ecosystem": "🔧 Claude 生态",
    "ai_tools_discovery": "🛠️ AI 工具发现",
    "china_ai_trends": "🏢 国内 AI 动态",
    "mcp_ecosystem": "🔌 MCP 生态",
    "spec_driven_dev": "📐 Spec 驱动开发",
}

# ── Claude 生态 ─────────────────────────────────────────────────
results["claude_ecosystem"] = [
    {
        "title": "Claude Code v2.1.142 · agents 多参数 · Fast→Opus 4.7",
        "url": "https://github.com/anthropics/claude-code/releases/tag/v2.1.142",
        "why": "claude agents 新增 8 个子命令参数（--add-dir/--mcp-config/--model 等）让多 agent 并发会话精确可控；Fast 模式默认切 Opus 4.7 直接影响响应质量与成本",
    },
    {
        "title": "Anthropic 与盖茨基金会签署 2 亿美元合作",
        "url": "https://www.anthropic.com/news/gates-foundation-partnership",
        "why": "2 亿美元 + Claude Credits 投向医疗/教育 AI，标志 Claude 从工具层向行业方案层扩张，agent 在垂直领域的商业化路径加速",
    },
    {
        "title": "anthropic-sdk-typescript v0.96.0 · Managed Agents 类型入库",
        "url": "https://github.com/anthropics/anthropic-sdk-typescript/releases/tag/sdk-v0.96.0",
        "why": "BetaManagedAgentsSearchResultBlock 进入 TS 类型系统，多 agent 搜索结果处理有了官方类型约束，是前端接入 Managed Agents 的关键 API 变更",
    },
]

# ── AI 工具发现 ─────────────────────────────────────────────────
results["ai_tools_discovery"] = [
    {
        "title": "mattpocock/skills · TS 大 V 的 Claude Code 技能集",
        "url": "https://github.com/mattpocock/skills",
        "why": "Matt Pocock 整理的 15+ 工程级技能（TDD/架构审查/调试），npx 一键安装直接融入 Claude Code 工作流，今日 trending +2987⭐",
    },
    {
        "title": "rohitg00/agentmemory · AI coding agent 持久记忆 #1",
        "url": "https://github.com/rohitg00/agentmemory",
        "why": "基准第一的 agent 持久化记忆方案，解决 Claude Code 大项目跨会话上下文丢失，支持 MCP/hook/REST 三种接入方式，今日 +1879⭐",
    },
    {
        "title": "garrytan/gstack · YC 总裁的 23 工具 Claude Code 配置",
        "url": "https://github.com/garrytan/gstack",
        "why": "Garry Tan 公开的 6 角色工具链（CEO/设计/工程管理/QA 等），可直接参考其 prompt 结构和角色分工，今日 trending +915⭐",
    },
    {
        "title": "obra/superpowers · 覆盖全链路的 Agent 技能框架",
        "url": "https://github.com/obra/superpowers",
        "why": "需求→任务→实现全链路的 Agent 方法论框架，今日 +1780⭐；面试中阐述 agentic workflow 架构设计的重要参考材料",
    },
    {
        "title": "Claude Code vs Cursor — 90 天生产对比 2026",
        "url": "https://dev.to/muhammad_moeed/claude-code-vs-cursor-90-days-with-both-in-2026-2dha",
        "why": "3 个月生产环境真实对比数据，AI 编程工具选型是高级工程师面试高频考点，实测结论比主观判断更具说服力",
    },
]

# ── 国内 AI 动态 ────────────────────────────────────────────────
results["china_ai_trends"] = [
    {
        "title": "马化腾：腾讯 AI 不急于抢地盘 · 下半年加码算力",
        "url": "https://www.leiphone.com/category/zaobao/eyVXHz0PsnnP82iT.html",
        "why": "腾讯 CEO「不抢存量」策略 + 下半年大幅扩大算力投入，直接影响混元模型和 CodeBuddy 的迭代节奏，是判断国内 AI 编码工具竞争格局的关键信号",
    },
    {
        "title": "腾讯辟谣 AI 一号人物离职 · 姚顺雨任首席 AI 科学家",
        "url": "https://www.smarthey.com/detail/667150602514.html",
        "why": "腾讯 AI 核心人事稳定确认（姚顺雨），混元/CodeBuddy 研发主线不变；大厂 AI 科学家流动是判断工具链可持续性的重要维度",
    },
    {
        "title": "字节跳动等入股自变量机器人",
        "url": "https://www.smarthey.com/detail/617121102524.html",
        "why": "字节在 AI 编码工具（Trae/Coze）之外布局具身 agent，国内大厂 AI 战略从编程工具向物理世界 agent 延伸的早期信号",
    },
]

# ── MCP 生态 ────────────────────────────────────────────────────
results["mcp_ecosystem"] = [
    {
        "title": "Gemini CLI 官方 MCP 实战：从零搭建 Google Drive LINE Bot",
        "url": "https://dev.to/evanlin/workshopgemini-cli-building-with-ai-2026-hands-on-with-gemini-cli-and-official-mcp-to-launch-a-296d",
        "why": "Gemini CLI 官方 MCP 集成教程证明 MCP 已成跨厂商 AI 接口标准，Claude Code 的 MCP 技能可低成本迁移，工具链间隔阂持续降低",
    },
]

# ── Spec 驱动开发 ───────────────────────────────────────────────
results["spec_driven_dev"] = [
    {
        "title": "github/spec-kit · GitHub 官方 Spec 驱动开发工具包",
        "url": "https://github.com/github/spec-kit",
        "why": "GitHub 官方出品，支持 30+ AI agent，今日 trending 爆发（+1232⭐）；Spec 驱动开发已从社区方法论升级为平台级工具链，是 AI 辅助开发范式面试的核心考点",
    },
]

# 跳过的信源
skipped_sources = [
    "HN Algolia（24h 内无符合条件内容）",
    "GitHub Search（需要 GITHUB_TOKEN）",
    "GitHub Releases API（WebFetch 403，改用 HTML 页面）",
    "DEV Community modelcontextprotocol（无新内容）",
]

# ── seen.json 处理（Step 1：读取并清理过期；Step 3：更新） ────────
seen_path = ".ai-news-bot/seen.json"
today_dt = datetime.strptime(today, "%Y-%m-%d")
seen_ttl_days = 7

if os.path.exists(seen_path):
    with open(seen_path, "r", encoding="utf-8") as f:
        seen_data = json.load(f)
    seen_urls = seen_data.get("urls", {})
    # 清理过期条目
    expired = [
        url
        for url, meta in seen_urls.items()
        if (today_dt - datetime.strptime(meta["last_seen"], "%Y-%m-%d")).days
        > seen_ttl_days
    ]
    for url in expired:
        del seen_urls[url]
else:
    seen_urls = {}

# 去重：将当次新条目写入 seen_urls，检测持续热点
all_items = (
    [(item, "claude_ecosystem") for item in results["claude_ecosystem"]]
    + [(item, "ai_tools_discovery") for item in results["ai_tools_discovery"]]
    + [(item, "china_ai_trends") for item in results["china_ai_trends"]]
    + [(item, "mcp_ecosystem") for item in results["mcp_ecosystem"]]
    + [(item, "spec_driven_dev") for item in results["spec_driven_dev"]]
)

for item, topic in all_items:
    url = item["url"]
    if url in seen_urls:
        # 已见过：更新 last_seen 和 count，不放入正文
        seen_urls[url]["last_seen"] = today
        seen_urls[url]["count"] += 1
        count = seen_urls[url]["count"]
        if count >= 3:
            hot_items.append(
                {
                    "title": item["title"],
                    "url": url,
                    "count": count,
                }
            )
        # 从 results 中移除已见条目
        results[topic] = [i for i in results[topic] if i["url"] != url]
    else:
        # 首次出现：写入 seen_urls
        seen_urls[url] = {
            "title": item["title"][:50],
            "url": url,
            "first_seen": today,
            "last_seen": today,
            "count": 1,
        }

# ── 构建 card elements ─────────────────────────────────────────
topic_order = [
    "claude_ecosystem",
    "ai_tools_discovery",
    "china_ai_trends",
    "mcp_ecosystem",
    "spec_driven_dev",
]

for topic_id in topic_order:
    items = results.get(topic_id, [])
    if not items:
        continue
    lines = [f"**{topic_labels[topic_id]}**"]
    for item in items:
        t = truncate_title(item["title"])
        lines.append(f"· [{t}]({item['url']})")
        lines.append(f"  _{item['why']}_")
    elements.append(
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    )
    elements.append({"tag": "hr"})

# 持续热点板块
if hot_items:
    lines = ["**🔥 持续热点（连续多日高热）**"]
    for item in sorted(hot_items, key=lambda x: -x["count"])[:5]:
        t = truncate_title(item["title"])
        lines.append(f"· [{t}]({item['url']}) · 已连续 {item['count']} 天")
    elements.append(
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    )
    elements.append({"tag": "hr"})

# 末尾 note
skip_msg = "、".join(skipped_sources) if skipped_sources else "无"
elements.append(
    {
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": (
                    f"数据来源：GitHub Releases · GitHub Trending · Anthropic Blog · "
                    f"雷峰网 · Smarthey · DEV Community"
                    f"｜修改关注维度：编辑 watch.yaml\n⚠️ 跳过：{skip_msg}"
                ),
            }
        ],
    }
)

# ── 构建完整 card ───────────────────────────────────────────────
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
report_path = ".ai-news-bot/latest-report.json"

with open(report_path, "w", encoding="utf-8") as f:
    json.dump(card_data, f, ensure_ascii=False, indent=2)

# 验证可解析
with open(report_path, "r", encoding="utf-8") as f:
    json.load(f)

print(f"✅ 报告已写入 {report_path}")

# 回写 seen.json
with open(seen_path, "w", encoding="utf-8") as f:
    json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)

print(f"✅ seen.json 已更新（{len(seen_urls)} 条记录）")
print(f"📊 各 topic 条目数：")
for tid in topic_order:
    print(f"   {topic_labels[tid]}: {len(results.get(tid, []))} 条")
if hot_items:
    print(f"🔥 持续热点: {len(hot_items)} 条")
