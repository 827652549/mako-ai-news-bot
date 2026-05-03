#!/usr/bin/env python3
"""AI News Monitoring Agent — executes all 6 steps from routine_prompt.md."""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
import yaml

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
NOW = datetime.now(timezone.utc)

# ─── Step 1: Read watch.yaml ─────────────────────────────────────────────────

with open("watch.yaml", "r") as f:
    config = yaml.safe_load(f)

topics_cfg = config["topics"]
filters = config["filters"]
output_cfg = config["output"]

lookback_hours = filters.get("lookback_hours", 24)
max_items_per_topic = filters.get("max_items_per_topic", 5)
seen_ttl_days = filters.get("seen_ttl_days", 7)
exclude_kws = [k.lower() for k in filters.get("exclude_keywords", [])]
CUTOFF = NOW - timedelta(hours=lookback_hours)

seen_path = ".ai-news-bot/seen.json"
seen_urls: dict = {}

if os.path.exists(seen_path):
    with open(seen_path, "r") as f:
        seen_urls = json.load(f).get("urls", {})

# Expire old entries
expire_cutoff = (NOW - timedelta(days=seen_ttl_days)).strftime("%Y-%m-%d")
seen_urls = {k: v for k, v in seen_urls.items()
             if v.get("last_seen", "9999") >= expire_cutoff}

# ─── HTTP helpers ────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ai-news-bot/1.0"}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

WEB_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
WEB_HEADERS = {"User-Agent": WEB_UA}

skipped_sources: list[str] = []


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def in_window(dt):
    return dt is not None and dt >= CUTOFF


def is_excluded(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in exclude_kws)


def truncate_title(title: str, max_len: int = 30) -> str:
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


def convert_query_dates(query: str) -> str:
    """Replace '>Ndays' shorthand with actual ISO dates for GitHub Search API."""
    def repl(m):
        qualifier = m.group(1)   # e.g. "created" or "pushed"
        op = m.group(2)          # ">" or "<"
        n = int(m.group(3))
        dt = (NOW - timedelta(days=n)).strftime("%Y-%m-%d")
        return f"{qualifier}:{op}{dt}"
    return re.sub(r'(created|pushed|updated):([><])(\d+)days', repl, query)


# ─── Step 2A: GitHub Releases ────────────────────────────────────────────────

def fetch_github_releases(repo: str, window_hours: int | None = None) -> list[dict]:
    try:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
        r = requests.get(url, headers=GH_HEADERS, timeout=10)
        if r.status_code != 200:
            skipped_sources.append(f"GitHub Releases {repo}（{r.status_code}）")
            return []
        cutoff = NOW - timedelta(hours=window_hours or lookback_hours)
        items = []
        for rel in r.json():
            pub = parse_dt(rel.get("published_at"))
            if pub is None or pub < cutoff:
                continue
            raw_body = (rel.get("body") or "")
            # Strip markdown headings and leading noise from preview
            clean_body = re.sub(r'^#+\s*', '', raw_body, flags=re.MULTILINE)
            clean_body = re.sub(r'\*\*([^*]+)\*\*', r'\1', clean_body)
            body_preview = " ".join(clean_body.split())[:80]
            items.append({
                "url": rel.get("html_url", ""),
                "title": f"{repo.split('/')[-1]} {rel.get('tag_name', '')} · {body_preview[:60]}",
                "body": body_preview,
                "published_at": pub,
                "tag": rel.get("tag_name", ""),
                "source": "github_releases",
                "repo": repo,
                "sort_score": 1000,
            })
        return items
    except Exception as e:
        skipped_sources.append(f"GitHub Releases {repo}（{e}）")
        return []


# ─── Step 2B: GitHub Trending ────────────────────────────────────────────────

def fetch_github_trending(must_match: list[str], languages: list[str]) -> list[dict]:
    items = []
    tried_langs = [""] + [l.lower() for l in languages]
    seen_repos: set[str] = set()

    for lang in tried_langs[:3]:  # cap requests
        try:
            params = {"since": "daily"}
            if lang:
                params["l"] = lang
            r = requests.get("https://github.com/trending", headers=WEB_HEADERS,
                             params=params, timeout=20)
            if r.status_code != 200:
                continue

            html = r.text
            # Each trending entry is wrapped in an <article class="Box-row"> block
            for art in re.findall(
                r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
                html, re.DOTALL
            ):
                # Repo slug
                slug_m = re.search(
                    r'<h2[^>]*>\s*<a[^>]*href="(/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"',
                    art)
                if not slug_m:
                    continue
                slug = slug_m.group(1).strip("/")
                if slug in seen_repos:
                    continue

                # Description
                desc_m = re.search(
                    r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>\s*(.*?)\s*</p>',
                    art, re.DOTALL)
                desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else ""

                # Stars today
                stars_m = re.search(r"([\d,]+)\s+stars today", art)
                stars_today = int(stars_m.group(1).replace(",", "")) if stars_m else 0

                combined = (slug + " " + desc).lower()
                if not any(kw.lower() in combined for kw in must_match):
                    continue
                if is_excluded(slug + " " + desc):
                    continue

                seen_repos.add(slug)
                items.append({
                    "url": f"https://github.com/{slug}",
                    "title": f"{slug} · {stars_today}⭐今日",
                    "body": desc,
                    "published_at": NOW,
                    "stars_today": stars_today,
                    "source": "github_trending",
                    "sort_score": stars_today,
                })
        except Exception as e:
            skipped_sources.append(f"GitHub Trending（{e}）")

    return sorted(items, key=lambda x: -x.get("stars_today", 0))


# ─── Step 2C: GitHub Search ──────────────────────────────────────────────────

_gh_search_rate_exhausted = False


def fetch_github_search(query: str) -> list[dict]:
    global _gh_search_rate_exhausted
    if _gh_search_rate_exhausted:
        return []
    try:
        q = convert_query_dates(query)
        params = {"q": q, "sort": "stars", "order": "desc", "per_page": 10}
        r = requests.get("https://api.github.com/search/repositories",
                         headers=GH_HEADERS, params=params, timeout=12)
        remaining = r.headers.get("X-RateLimit-Remaining", "1")
        if remaining == "0":
            _gh_search_rate_exhausted = True
            skipped_sources.append("GitHub Search（Rate Limit 耗尽）")
            return []
        if r.status_code != 200:
            return []

        items = []
        for repo in r.json().get("items", []):
            updated = parse_dt(repo.get("updated_at"))
            created = parse_dt(repo.get("created_at"))
            # Accept if updated recently (within 14 days) or created within window
            fourteen_ago = NOW - timedelta(days=14)
            if not ((updated and updated >= fourteen_ago) or in_window(created)):
                continue
            desc = repo.get("description") or ""
            if is_excluded(repo.get("name", "") + " " + desc):
                continue
            stars = repo.get("stargazers_count", 0)
            items.append({
                "url": repo.get("html_url", ""),
                "title": f"{repo.get('full_name', '')} · {stars}⭐",
                "body": desc,
                "published_at": updated or created or NOW,
                "stars": stars,
                "source": "github_search",
                "sort_score": stars,
            })
        return items
    except Exception as e:
        return []


# ─── Step 2D: HN Algolia ─────────────────────────────────────────────────────

def fetch_hn(keyword: str, min_points: int) -> list[dict]:
    try:
        since = int((NOW - timedelta(hours=lookback_hours)).timestamp())
        params = {
            "query": keyword,
            "tags": "story",
            "numericFilters": f"created_at_i>{since},points>{min_points}",
            "hitsPerPage": 5,
        }
        r = requests.get("https://hn.algolia.com/api/v1/search",
                         params=params, timeout=10)
        if r.status_code != 200:
            return []
        items = []
        for hit in r.json().get("hits", []):
            pub = datetime.fromtimestamp(hit.get("created_at_i", 0), tz=timezone.utc)
            if not in_window(pub):
                continue
            title = hit.get("title", "")
            if is_excluded(title):
                continue
            pts = hit.get("points", 0)
            story_url = hit.get("url") or \
                f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            items.append({
                "url": story_url,
                "title": title,
                "body": f"HN {pts}pts",
                "published_at": pub,
                "points": pts,
                "hn_id": hit.get("objectID"),
                "source": "hn",
                "sort_score": pts,
            })
        return items
    except Exception as e:
        return []


# ─── Step 2E: Web (direct / Jina) ────────────────────────────────────────────

# Titles that are definitely generic navigation/chrome, not articles
_NAV_TITLE_RE = re.compile(
    r'(go back|首页|登录|注册|搜索|更多|全部|分类|标签|关于|联系|'
    r'订阅|RSS|privacy policy|terms of|cookie|copyright|all rights reserved|'
    r'sign in|sign up|log in|log out|learn more|read more|click here|'
    r'view all|see all|load more|中国计算机|学会|协会|基金会|研究院|大学|高校)',
    re.IGNORECASE,
)
# AI-related keywords for china_ai_trends relevance check
_AI_KEYWORDS_ZH = [
    "AI", "人工智能", "大模型", "模型", "算法", "智能", "云服务", "云计算",
    "芯片", "编程", "代码", "开发者", "开源", "inference", "LLM", "GPT",
    "语言模型", "机器学习", "深度学习", "计算机视觉", "自然语言",
]


def _is_nav_link(title: str, url: str) -> bool:
    """Return True if this looks like a navigation/footer link rather than an article."""
    title_stripped = title.strip()
    if len(title_stripped) < 6:
        return True
    if _NAV_TITLE_RE.search(title_stripped):
        return True
    # URL with no meaningful path (just a domain or single shallow segment like /en)
    path = re.sub(r'^https?://[^/]+', '', url).rstrip('/')
    path_depth = len([p for p in path.split('/') if p])
    if path_depth <= 1:
        return True
    return False


def fetch_web(url: str, use_jina: bool = False, must_match: list | None = None,
              require_ai_relevance: bool = False) -> list[dict]:
    def _get(fetch_url):
        return requests.get(fetch_url, headers=WEB_HEADERS, timeout=20)

    content = ""
    try:
        if use_jina:
            r = _get(f"https://r.jina.ai/{url}")
        else:
            r = _get(url)
            if r.status_code != 200 or len(r.text) < 500:
                r = _get(f"https://r.jina.ai/{url}")
        if r.status_code != 200:
            skipped_sources.append(f"{url}（{r.status_code}）")
            return []
        content = r.text
    except Exception as e:
        skipped_sources.append(f"{url}（连接失败）")
        return []

    items = []
    seen_links: set[str] = set()

    # Markdown-style links (Jina output)
    for title, link in re.findall(r'\[([^\]]{5,120})\]\((https?://[^\)\s]+)\)', content):
        title = title.strip()
        if link in seen_links:
            continue
        if _is_nav_link(title, link):
            continue
        if is_excluded(title):
            continue
        if must_match and not any(kw.lower() in title.lower() for kw in must_match):
            continue
        if require_ai_relevance:
            if not any(kw.lower() in title.lower() for kw in _AI_KEYWORDS_ZH):
                continue
        # Extract date from URL if present (e.g. /2026-04-22-title or /2026/04/22/)
        pub_dt = NOW
        url_date_m = re.search(r'(\d{4})-(\d{2})-(\d{2})', link)
        if url_date_m:
            try:
                pub_dt = datetime(
                    int(url_date_m.group(1)),
                    int(url_date_m.group(2)),
                    int(url_date_m.group(3)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass

        seen_links.add(link)
        items.append({
            "url": link,
            "title": title,
            "body": "",
            "published_at": pub_dt,
            "source": "web",
            "sort_score": 0,
        })

    return items[:10]


# ─── Step 2F: DEV Community ──────────────────────────────────────────────────

def fetch_dev_community(tag: str) -> list[dict]:
    try:
        r = requests.get("https://dev.to/api/articles",
                         params={"tag": tag, "per_page": 10, "top": 1}, timeout=10)
        if r.status_code != 200:
            return []
        items = []
        for art in r.json():
            pub = parse_dt(art.get("published_at"))
            if not in_window(pub):
                continue
            title = art.get("title", "")
            if is_excluded(title):
                continue
            reactions = art.get("public_reactions_count", 0)
            items.append({
                "url": art.get("url", ""),
                "title": title,
                "body": art.get("description", ""),
                "published_at": pub,
                "reactions": reactions,
                "source": "dev_community",
                "sort_score": reactions,
            })
        return items
    except Exception:
        return []


# ─── Step 2: Run all fetches per topic ───────────────────────────────────────

raw_results: dict[str, list[dict]] = {}

for topic in topics_cfg:
    tid = topic["id"]
    sources = topic.get("sources", {})
    bucket: list[dict] = []

    # GitHub Releases — same-repo dedup: keep only latest per repo.
    # If nothing found in the 24h window, extend to 72h (catch-up for missed days).
    if "github_releases" in sources:
        repo_latest: dict[str, dict] = {}
        for repo in sources["github_releases"]:
            rels = fetch_github_releases(repo)
            extended = False
            if not rels:
                rels = fetch_github_releases(repo, window_hours=72)
                extended = True
            for rel in rels:
                if extended:
                    rel["force_include"] = True  # bypass Step-3 window check
                r_pub = rel["published_at"]
                if repo not in repo_latest or r_pub > repo_latest[repo]["published_at"]:
                    repo_latest[repo] = rel
        bucket.extend(repo_latest.values())

    # GitHub Trending
    if "github_trending" in sources:
        gt_cfg = sources["github_trending"]
        must = gt_cfg.get("must_match_any", [])
        langs = gt_cfg.get("languages", [])
        bucket.extend(fetch_github_trending(must, langs))

    # GitHub Search
    if "github_search" in sources:
        for q in sources["github_search"].get("queries", []):
            bucket.extend(fetch_github_search(q))

    # HN
    if "hn" in sources:
        hn_cfg = sources["hn"]
        min_pts = hn_cfg.get("min_points", 30)
        seen_hn_ids: set = set()
        for kw in hn_cfg.get("keywords", []):
            for hit in fetch_hn(kw, min_pts):
                hn_id = hit.get("hn_id", hit["url"])
                if hn_id not in seen_hn_ids:
                    seen_hn_ids.add(hn_id)
                    bucket.append(hit)

    # Web
    if "web" in sources:
        # china_ai_trends needs extra AI-relevance filter to avoid finance/social news
        require_ai = (tid == "china_ai_trends")
        for ws in sources["web"]:
            web_items = fetch_web(
                ws["url"],
                use_jina=ws.get("use_jina", False),
                must_match=ws.get("must_match_any"),
                require_ai_relevance=require_ai,
            )
            bucket.extend(web_items)

    # DEV Community
    if "dev_community" in sources:
        seen_dev_urls: set[str] = set()
        for tag in sources["dev_community"].get("tags", []):
            for item in fetch_dev_community(tag):
                if item["url"] not in seen_dev_urls:
                    seen_dev_urls.add(item["url"])
                    bucket.append(item)

    raw_results[tid] = bucket

print(f"Raw counts: { {k: len(v) for k, v in raw_results.items()} }")


# ─── Step 3: Filter & deduplicate ────────────────────────────────────────────

global_seen_this_run: dict[str, str] = {}  # url → topic_id first seen
hot_items: list[dict] = []
filtered_results: dict[str, list[dict]] = {}

for topic in topics_cfg:
    tid = topic["id"]
    out: list[dict] = []
    for item in raw_results.get(tid, []):
        url = item.get("url", "")
        title = item.get("title", "")

        # Step 3.1: discard items outside the time window (unless force_include)
        pub = item.get("published_at")
        if pub and not in_window(pub) and not item.get("force_include"):
            continue

        # Exclude filter
        if is_excluded(title + " " + item.get("body", "")):
            continue

        # Cross-topic dedup (keep first occurrence)
        if url and url in global_seen_this_run:
            continue

        # seen.json check
        if url in seen_urls:
            seen_urls[url]["last_seen"] = TODAY
            seen_urls[url]["count"] += 1
            count = seen_urls[url]["count"]
            if count >= 3:
                hot_items.append({**item, "count": count})
            continue  # skip from main body

        # New item
        if url:
            global_seen_this_run[url] = tid
            seen_urls[url] = {
                "title": title[:80],
                "url": url,
                "first_seen": TODAY,
                "last_seen": TODAY,
                "count": 1,
            }
        out.append(item)

    # Sort by sort_score desc, then recency
    out.sort(key=lambda x: (-x.get("sort_score", 0),
                             -(x["published_at"].timestamp() if x.get("published_at") else 0)))
    filtered_results[tid] = out[:max_items_per_topic]

total_items = sum(len(v) for v in filtered_results.values())
print(f"After filter: { {k: len(v) for k, v in filtered_results.items()} }")
print(f"Total items: {total_items}, skipped: {skipped_sources}")


# ─── Step 4: Generate "why" + build card ─────────────────────────────────────

def generate_why(item: dict, tid: str) -> str:
    title = item.get("title", "")
    body = item.get("body", "")
    combined = (title + " " + body).lower()
    source = item.get("source", "")

    if tid == "claude_ecosystem":
        tag = item.get("tag", "")
        if "claude-code" in combined or "claude code" in combined:
            # Try to extract key features from body
            features = []
            b = item.get("body", "")
            if "cache" in b.lower():
                features.append("缓存策略变化")
            if "mcp" in b.lower():
                features.append("MCP 支持更新")
            if "/recap" in b.lower() or "recap" in b.lower():
                features.append("/recap 上下文恢复")
            if "tui" in b.lower():
                features.append("/tui 终端渲染优化")
            if features:
                return f"Claude Code 新版本（{tag}），{', '.join(features[:2])}，直接影响日常编程工作流"
            return f"Claude Code 新版本（{tag}），关注工具链变化对 MCP/agent 工作流的影响"
        if "anthropic-sdk" in combined or "sdk" in combined:
            if "deprecat" in combined:
                return "SDK 标记旧模型为 deprecated，现有项目应迁移到 Claude 4.x 系列，避免废弃 API 风险"
            return "Anthropic SDK 更新，影响 Claude API 接入方式，新特性和 breaking change 需跟进"
        if "larksuite" in combined or "lark" in combined or "larkcli" in combined:
            return "飞书 CLI/SDK 更新，与本 bot 飞书集成工作流直接相关，需检查兼容性"
        if "anthropic.com/news" in item.get("url", "") or "blog" in combined:
            return "Anthropic 官方公告，产品路线图和模型能力边界一手信息，影响 Claude API 工程化决策"
        return "Claude 生态核心更新，与 AI 编程工作流工具链直接相关"

    if tid == "ai_tools_discovery":
        if "mcp" in combined:
            return "MCP 相关新工具，可直接扩展 Claude Code 工具调用边界，评估是否集成到开发工作流"
        if "spec" in combined and ("kit" in combined or "driven" in combined):
            return "Spec 驱动开发工具，与 AI 编程规范化工作流设计模式高度相关，面试架构话题亮点"
        if "agent" in combined:
            pts = item.get("points", 0)
            stars = item.get("stars", item.get("stars_today", 0))
            heat = f"{pts}pts" if pts else f"{stars}⭐"
            return f"Agent 框架新工具（{heat}），与 Claude Code 多 agent 协作方向契合，关注设计模式差异"
        if "hn" in source:
            pts = item.get("points", 0)
            return f"HN {pts}pts 社区热点，AI 编程工具圈关注焦点，了解业界认知现状有助于面试技术视野展示"
        if "trending" in source:
            st = item.get("stars_today", 0)
            return f"GitHub 今日 trending（+{st}⭐），快速增长的 AI 编程工具，评估是否纳入工具链"
        return "AI 编程工具新项目，潜在可集成到 Claude Code 工作流的工具"

    if tid == "china_ai_trends":
        for kw, vendor in [("阿里云", "阿里云"), ("通义", "通义/阿里云"), ("阿里", "阿里")]:
            if kw in title + body:
                return f"{vendor} AI 动态，国内最大云厂商 AI 服务演进，影响国内 Claude 替代方案选型"
        for kw, vendor in [("腾讯云", "腾讯云"), ("混元", "混元/腾讯"), ("codebuddy", "CodeBuddy/腾讯"), ("腾讯", "腾讯")]:
            if kw in combined:
                return f"{vendor} 最新动态，CodeBuddy 是国内 Claude Code 主要竞品，横向对比有助于判断工具选型"
        for kw, vendor in [("字节跳动", "字节跳动"), ("豆包", "豆包/字节"), ("字节", "字节")]:
            if kw in combined:
                return f"{vendor} AI 产品动态，豆包编程助手在国内 AI coding 赛道快速追赶，竞争格局参考"
        for kw, vendor in [("百度", "百度"), ("文心", "文心/百度")]:
            if kw in combined:
                return f"{vendor} AI 进展，国内大模型格局变化，影响 AI 工程化工具生态判断"
        for kw in ["华为云", "华为"]:
            if kw in combined:
                return "华为云 AI 更新，自研芯片+大模型方向，影响国内云算力格局和 AI 工具部署选择"
        return "国内 AI 厂商动态，了解国内 AI 工具生态与 Claude 的竞争/互补关系"

    if tid == "mcp_ecosystem":
        if "github_search" in source or "github_trending" in source:
            stars = item.get("stars", item.get("stars_today", 0))
            repo_name = item.get("url", "").split("github.com/")[-1]
            body_lc = body.lower()
            use_case = ""
            if "browser" in body_lc or "playwright" in body_lc:
                use_case = "浏览器自动化场景"
            elif "database" in body_lc or "sql" in body_lc or "postgres" in body_lc:
                use_case = "数据库查询场景"
            elif "file" in body_lc or "filesystem" in body_lc:
                use_case = "文件系统操作场景"
            elif "search" in body_lc or "web" in body_lc:
                use_case = "Web 搜索场景"
            if use_case:
                return f"MCP Server 新实现（{stars}⭐，{use_case}），可直接为 Claude Code 增加该能力，评估是否集成"
            return f"MCP Server 新实现（{stars}⭐），扩展 Claude Code 工具调用能力边界，可直接评估集成价值"
        if "protocol" in combined or "model context" in combined:
            return "MCP 协议层动态，理解工具调用标准化方向，是面试系统设计和工具架构的重要背景知识"
        if "tdd" in combined or "test" in combined:
            return "MCP + TDD 实践，前端工程化与 AI 工具链结合的具体落地案例，面试「如何保证 AI 辅助代码质量」话题参考"
        if "skill" in combined and "matt" in combined:
            return "mattpocock（ts-reset 作者）发布 Claude Code Skills + TDD 实践，TypeScript 社区高权威，工具链方法论参考"
        if "observ" in combined or "可观测" in combined:
            return "MCP 用于可观测性场景，拓展 MCP 在系统监控工具链中的应用边界，前端全链路追踪参考"
        if "hn" in source:
            pts = item.get("points", 0)
            return f"HN {pts}pts MCP 社区讨论，协议采用率和生态成熟度判断依据"
        if "web" in source:
            return "MCP 生态博客文章，关注 MCP 协议在实际工程场景中的应用案例"
        return "MCP 生态新进展，与 Claude Code 工具集成能力直接相关"

    if tid == "spec_driven_dev":
        if "openspec" in combined or "spec-kit" in combined:
            return "Spec 驱动开发核心工具，与 AI 辅助编程规范化工作流设计模式直接相关，面试架构题亮点"
        if "sdd" in combined or "spec-driven" in combined:
            return "Spec 驱动开发方法论，前端工程化与 AI 协作的重要模式，P7 面试系统设计必备视角"
        return "Spec 驱动开发新动态，AI 辅助编程规范化实践参考"

    return "AI 工具链相关动态，与前端工程化和 AI 编程工作流有潜在关联"


# Topic display config
topic_icons = {
    "claude_ecosystem": "🔧",
    "ai_tools_discovery": "🛠️",
    "china_ai_trends": "🏢",
    "mcp_ecosystem": "🔌",
    "spec_driven_dev": "📐",
}
topic_priority = {"high": 0, "medium": 1, "low": 2}
sorted_topics = sorted(topics_cfg,
                       key=lambda t: topic_priority.get(t.get("priority", "medium"), 1))

elements: list[dict] = []

for topic in sorted_topics:
    tid = topic["id"]
    items = filtered_results.get(tid, [])
    if not items:
        continue

    icon = topic_icons.get(tid, "📌")
    label = topic["name"]
    lines = [f"**{icon} {label}**"]
    for item in items:
        t = truncate_title(item["title"])
        why = generate_why(item, tid)
        lines.append(f"· [{t}]({item['url']})")
        lines.append(f"  _{why}_")

    elements.append({"tag": "div",
                     "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    elements.append({"tag": "hr"})

# Hot items (count >= 3)
deduped_hot: dict[str, dict] = {}
for item in hot_items:
    u = item.get("url", "")
    if u not in deduped_hot or item["count"] > deduped_hot[u]["count"]:
        deduped_hot[u] = item

if deduped_hot:
    lines = ["**🔥 持续热点（连续多日高热）**"]
    for item in sorted(deduped_hot.values(), key=lambda x: -x["count"])[:5]:
        t = truncate_title(item["title"])
        lines.append(f"· [{t}]({item['url']}) · 已连续 {item['count']} 天")
    elements.append({"tag": "div",
                     "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    elements.append({"tag": "hr"})

skip_msg = "、".join(dict.fromkeys(skipped_sources)) if skipped_sources else "无"
elements.append({
    "tag": "note",
    "elements": [{
        "tag": "plain_text",
        "content": (
            f"数据来源：GitHub · HN · 雷峰网 · DEV Community｜"
            f"修改关注维度：编辑 watch.yaml\n⚠️ 跳过：{skip_msg}"
        ),
    }],
})

# ─── Step 5: Write files ──────────────────────────────────────────────────────

if total_items == 0 and output_cfg.get("skip_if_empty", True):
    print("No items found in any topic — skip_if_empty=true, exiting.")
    sys.exit(0)

card_data = {
    "msg_type": "interactive",
    "card": {
        "header": {
            "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {TODAY}"},
            "template": "blue",
        },
        "elements": elements,
    },
}

os.makedirs(".ai-news-bot", exist_ok=True)
report_path = ".ai-news-bot/latest-report.json"

with open(report_path, "w", encoding="utf-8") as f:
    json.dump(card_data, f, ensure_ascii=False, indent=2)

# Validate JSON is parseable
with open(report_path, "r", encoding="utf-8") as f:
    json.load(f)

print(f"✅ Report written to {report_path}")

with open(seen_path, "w", encoding="utf-8") as f:
    json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)

print(f"✅ seen.json updated ({len(seen_urls)} entries)")
print("Done.")
