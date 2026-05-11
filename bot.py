#!/usr/bin/env python3
"""AI 资讯监控 Agent

按 watch.yaml 中的约束抓取各信源，过滤去重后生成飞书卡片 JSON，
写入 .ai-news-bot/latest-report.json，并推送到 report/<date> 分支触发
GitHub Actions 发送飞书消息。

用法:
    python3 bot.py [--dry-run]   # --dry-run 仅打印卡片，不 git push
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser

import requests
import yaml

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCH_YAML = os.path.join(SCRIPT_DIR, "watch.yaml")
SEEN_JSON = os.path.join(SCRIPT_DIR, ".ai-news-bot", "seen.json")
REPORT_PATH = os.path.join(SCRIPT_DIR, ".ai-news-bot", "latest-report.json")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def truncate_title(title: str, max_len: int = 30) -> str:
    """在最近词语边界截断标题，保留版本号和核心描述。"""
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def safe_get(url: str, params=None, headers=None, timeout: int = 15) -> requests.Response | None:
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
        return r
    except Exception as e:
        log(f"  GET 失败 {url}: {e}")
        return None


def jina_fetch(url: str) -> str | None:
    """通过 Jina Reader 抓取页面内容。"""
    jina_url = f"https://r.jina.ai/{url}"
    r = safe_get(jina_url, headers={"User-Agent": UA}, timeout=20)
    if r and len(r.text) > 300:
        return r.text
    return None


def web_fetch(url: str, use_jina: bool = False) -> str | None:
    """直连优先，失败或内容过短则自动切换 Jina Reader。"""
    if use_jina:
        return jina_fetch(url)
    r = safe_get(url, headers={"User-Agent": UA}, timeout=15)
    if r and r.status_code == 200 and len(r.text) >= 500:
        return r.text
    log(f"  直连失败（status={getattr(r,'status_code','N/A')} len={len(getattr(r,'text',''))}），切换 Jina")
    return jina_fetch(url)


# ---------------------------------------------------------------------------
# Step 1：读取配置与去重表
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(WATCH_YAML):
        sys.exit(f"[ERROR] watch.yaml 不存在：{WATCH_YAML}")
    with open(WATCH_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen(ttl_days: int) -> dict:
    if not os.path.exists(SEEN_JSON):
        return {}
    with open(SEEN_JSON, encoding="utf-8") as f:
        data = json.load(f)
    urls = data.get("urls", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
    pruned = {u: v for u, v in urls.items() if v.get("last_seen", "9999") >= cutoff}
    log(f"seen.json 加载 {len(urls)} 条，清理过期后剩 {len(pruned)} 条")
    return pruned


# ---------------------------------------------------------------------------
# Step 2A：GitHub Releases
# ---------------------------------------------------------------------------

def fetch_github_releases(repos: list[str], since: datetime) -> list[dict]:
    results = []
    for repo in repos:
        log(f"  GitHub Releases: {repo}")
        r = safe_get(
            f"https://api.github.com/repos/{repo}/releases?per_page=5",
            headers=gh_headers(),
        )
        if not r or r.status_code != 200:
            log(f"    跳过（status={getattr(r,'status_code','N/A')}）")
            continue
        for rel in r.json():
            pub = rel.get("published_at", "")
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt < since:
                continue
            body = rel.get("body") or ""
            # 只取前 200 字的摘要
            summary = body.strip()[:200].replace("\r\n", " ").replace("\n", " ")
            results.append({
                "source": "github_releases",
                "repo": repo,
                "title": f"{repo.split('/')[-1]} {rel['tag_name']}",
                "url": rel["html_url"],
                "summary": summary,
                "published_at": pub,
                "stars": 0,
                "points": 0,
            })
    return results


# ---------------------------------------------------------------------------
# Step 2B：GitHub Trending
# ---------------------------------------------------------------------------

class _TrendingParser(HTMLParser):
    """轻量 HTML 解析器，提取 github.com/trending 仓库卡片。"""

    def __init__(self):
        super().__init__()
        self.repos: list[dict] = []
        self._in_article = False
        self._in_h2 = False
        self._in_p = False
        self._in_stars_span = False
        self._current: dict = {}
        self._tag_stack: list[str] = []
        self._article_depth = 0
        self._h2_buf = ""
        self._p_buf = ""
        self._stars_buf = ""
        self._star_span_class = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        self._tag_stack.append(tag)
        if tag == "article":
            self._in_article = True
            self._article_depth = len(self._tag_stack)
            self._current = {"title": "", "description": "", "stars_today": 0}
        if self._in_article:
            if tag == "h2":
                self._in_h2 = True
                self._h2_buf = ""
            if tag == "p" and "col-9" in attrs_d.get("class", ""):
                self._in_p = True
                self._p_buf = ""
            if tag == "span" and "d-inline-block" in attrs_d.get("class", ""):
                self._in_stars_span = True
                self._stars_buf = ""

    def handle_endtag(self, tag):
        if tag == "article" and self._in_article:
            if self._current.get("title"):
                self.repos.append(self._current)
            self._in_article = False
            self._current = {}
        if tag == "h2":
            self._in_h2 = False
            # Extract owner/repo from h2 text like "\n  owner\n  /\n  repo\n"
            raw = self._h2_buf.strip()
            # e.g. "owner / repo" or "owner/repo"
            clean = re.sub(r"\s+", "", raw)  # remove all whitespace
            self._current["title"] = clean
        if tag == "p" and self._in_p:
            self._in_p = False
            self._current["description"] = self._p_buf.strip()
        if tag == "span" and self._in_stars_span:
            self._in_stars_span = False
            raw = self._stars_buf.strip().replace(",", "").replace(" ", "")
            try:
                self._current["stars_today"] = int(raw)
            except ValueError:
                pass
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data):
        if self._in_h2:
            self._h2_buf += data
        if self._in_p:
            self._p_buf += data
        if self._in_stars_span:
            self._stars_buf += data


def fetch_github_trending(config: dict) -> list[dict]:
    """抓取 GitHub Trending 日榜，按关键词过滤。"""
    must_match = [k.lower() for k in config.get("must_match_any", [])]
    results = []
    for lang_param in [None, "zh"]:
        url = "https://github.com/trending"
        params = {"since": "daily"}
        if lang_param:
            params["spoken_language_code"] = lang_param
        log(f"  GitHub Trending (lang={lang_param or 'all'})")
        r = safe_get(url, params=params, headers={"User-Agent": UA})
        if not r or r.status_code != 200 or len(r.text) < 500:
            log("    直连失败，尝试 Jina")
            text = jina_fetch(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
            if not text:
                continue
            # 从 Jina 纯文本中解析
            for line in text.splitlines():
                m = re.match(r"\*\s+\[([^\]]+)\]\(https://github\.com/([^)]+)\)", line)
                if m:
                    full_name = m.group(2).strip("/")
                    desc = ""
                    combo = (full_name + " " + desc).lower()
                    if must_match and not any(k in combo for k in must_match):
                        continue
                    results.append({
                        "source": "github_trending",
                        "repo": full_name,
                        "title": full_name,
                        "url": f"https://github.com/{full_name}",
                        "summary": desc,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "stars": 0,
                        "points": 0,
                    })
            continue

        parser = _TrendingParser()
        parser.feed(r.text)
        for repo in parser.repos:
            name = repo["title"]
            desc = repo.get("description", "")
            combo = (name + " " + desc).lower()
            if must_match and not any(k in combo for k in must_match):
                continue
            url_repo = f"https://github.com/{name}"
            results.append({
                "source": "github_trending",
                "repo": name,
                "title": name,
                "url": url_repo,
                "summary": desc,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "stars": repo.get("stars_today", 0),
                "points": repo.get("stars_today", 0),
            })
    # 去重
    seen_urls: set[str] = set()
    deduped = []
    for item in results:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# Step 2C：GitHub Search
# ---------------------------------------------------------------------------

def fetch_github_search(queries: list[str], since: datetime) -> tuple[list[dict], bool]:
    """返回 (items, rate_limited)。"""
    results = []
    rate_limited = False
    for query in queries:
        log(f"  GitHub Search: {query!r}")
        r = safe_get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": 10},
            headers=gh_headers(),
        )
        if not r:
            continue
        remaining = int(r.headers.get("X-RateLimit-Remaining", 1))
        if remaining == 0 or r.status_code == 403:
            log("    Rate Limit 耗尽，跳过")
            rate_limited = True
            break
        if r.status_code != 200:
            log(f"    跳过（status={r.status_code}）")
            continue
        for repo in r.json().get("items", []):
            created = repo.get("created_at", "")
            updated = repo.get("updated_at", "")
            check_dt = created or updated
            if check_dt:
                dt = datetime.fromisoformat(check_dt.replace("Z", "+00:00"))
                if dt < since:
                    continue
            results.append({
                "source": "github_search",
                "repo": repo["full_name"],
                "title": repo["full_name"],
                "url": repo["html_url"],
                "summary": repo.get("description") or "",
                "published_at": created or updated,
                "stars": repo.get("stargazers_count", 0),
                "points": repo.get("stargazers_count", 0),
            })
    return results, rate_limited


# ---------------------------------------------------------------------------
# Step 2D：HN Algolia
# ---------------------------------------------------------------------------

def fetch_hn(keywords: list[str], min_points: int, since: datetime) -> list[dict]:
    since_ts = int(since.timestamp())
    results: dict[str, dict] = {}
    for kw in keywords:
        log(f"  HN: {kw!r}")
        r = safe_get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": kw,
                "tags": "story",
                "numericFilters": f"created_at_i>{since_ts},points>{min_points}",
                "hitsPerPage": 5,
            },
        )
        if not r or r.status_code != 200:
            continue
        for hit in r.json().get("hits", []):
            oid = hit.get("objectID", "")
            if oid in results:
                continue
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            results[oid] = {
                "source": "hn",
                "repo": "",
                "title": hit.get("title", ""),
                "url": url,
                "summary": "",
                "published_at": datetime.fromtimestamp(
                    hit.get("created_at_i", 0), tz=timezone.utc
                ).isoformat(),
                "stars": 0,
                "points": hit.get("points", 0),
            }
    return list(results.values())


# ---------------------------------------------------------------------------
# Step 2E：Web 抓取（Anthropic Blog、雷峰网等）
# ---------------------------------------------------------------------------

def _extract_articles_from_text(text: str, must_match: list[str], since: datetime) -> list[dict]:
    """从纯文本（Jina Reader 输出）中提取文章列表。"""
    articles = []
    lines = text.splitlines()
    # 常见日期格式
    date_pattern = re.compile(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s+\w+\s+\d{4}|May \d{1,2},? \d{4}|Apr \d{1,2},? \d{4}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    )
    url_pattern = re.compile(r"https?://[^\s)\"']+")
    title_pattern = re.compile(r"^#+\s+(.+)$|^\*\*(.+)\*\*$|^\[(.+)\]\(https?://")

    current_title = ""
    current_url = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配 Markdown 链接 [title](url)
        m = re.match(r"#+\s+\[(.+?)\]\((https?://[^\)]+)\)", line)
        if not m:
            m = re.match(r"\*?\s*\[(.+?)\]\((https?://[^\)]+)\)", line)
        if m:
            current_title = m.group(1).strip()
            current_url = m.group(2).strip()
        else:
            # 纯标题行
            hm = re.match(r"#+\s+(.+)", line)
            if hm:
                current_title = hm.group(1).strip()
                current_url = ""

        if current_title and len(current_title) > 5:
            combo = current_title.lower() + line.lower()
            if must_match and not any(k.lower() in combo for k in must_match):
                continue
            if current_url and current_url not in [a["url"] for a in articles]:
                articles.append({
                    "source": "web",
                    "repo": "",
                    "title": current_title,
                    "url": current_url,
                    "summary": "",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "stars": 0,
                    "points": 0,
                })
    return articles[:10]


def fetch_web_source(src: dict, since: datetime) -> tuple[list[dict], str | None]:
    """抓取一个 web source，返回 (items, skip_reason)。"""
    url = src["url"]
    use_jina = src.get("use_jina", False)
    must_match = [k.lower() for k in src.get("must_match_any", [])]
    log(f"  Web: {url} (jina={use_jina})")

    text = web_fetch(url, use_jina=use_jina)
    if not text:
        return [], f"Web({url})（无法抓取）"

    articles = _extract_articles_from_text(text, must_match, since)
    log(f"    提取到 {len(articles)} 篇文章")
    return articles, None


# ---------------------------------------------------------------------------
# Step 2F：DEV Community
# ---------------------------------------------------------------------------

def fetch_dev_community(tags: list[str], since: datetime) -> list[dict]:
    results: dict[str, dict] = {}
    for tag in tags:
        log(f"  DEV: tag={tag!r}")
        r = safe_get(
            "https://dev.to/api/articles",
            params={"tag": tag, "per_page": 10, "top": 1},
        )
        if not r or r.status_code != 200:
            log(f"    跳过（status={getattr(r,'status_code','N/A')}）")
            continue
        for art in r.json():
            pub = art.get("published_at") or art.get("created_at", "")
            if pub:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt < since:
                    continue
            url = art.get("url", "")
            if not url or url in results:
                continue
            results[url] = {
                "source": "dev_community",
                "repo": "",
                "title": art.get("title", ""),
                "url": url,
                "summary": art.get("description") or "",
                "published_at": pub,
                "stars": 0,
                "points": art.get("positive_reactions_count", 0),
            }
    return list(results.values())


# ---------------------------------------------------------------------------
# Step 3：过滤与去重
# ---------------------------------------------------------------------------

def filter_items(
    items: list[dict],
    exclude_kws: list[str],
    since: datetime,
    seen_urls: dict,
    today: str,
    max_items: int,
) -> tuple[list[dict], list[dict]]:
    """
    返回 (new_items, hot_items)
    - new_items: 未见过的条目（正常输出到 topic 正文）
    - hot_items: count >= 3 的持续热点
    """
    exclude_lower = [k.lower() for k in exclude_kws]

    # 1. 时间窗口过滤
    valid = []
    for it in items:
        pub = it.get("published_at", "")
        if pub:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt < since:
                    continue
            except Exception:
                pass
        valid.append(it)

    # 2. 排除关键词
    def has_excluded(it: dict) -> bool:
        combo = (it.get("title", "") + it.get("summary", "")).lower()
        return any(k in combo for k in exclude_lower)

    valid = [it for it in valid if not has_excluded(it)]

    # 3. 同仓库同日版本去重（保留最新）
    repo_latest: dict[str, dict] = {}
    for it in valid:
        repo = it.get("repo", "")
        if not repo:
            continue
        pub = it.get("published_at", "")
        if repo not in repo_latest or pub > repo_latest[repo].get("published_at", ""):
            repo_latest[repo] = it

    no_repo = [it for it in valid if not it.get("repo", "")]
    has_repo = list(repo_latest.values())
    valid = no_repo + has_repo

    # 4. 跨信源去重（URL 相同 或 标题相似度 > 0.8）
    deduped: list[dict] = []
    for it in sorted(valid, key=lambda x: -(x.get("points", 0) + x.get("stars", 0))):
        dup = False
        for kept in deduped:
            if it["url"] == kept["url"]:
                dup = True
                break
            if similarity(it["title"], kept["title"]) > 0.8:
                dup = True
                break
        if not dup:
            deduped.append(it)

    # 5. seen.json 处理
    new_items = []
    hot_items = []
    for it in deduped:
        url = it["url"]
        if url in seen_urls:
            entry = seen_urls[url]
            entry["last_seen"] = today
            entry["count"] = entry.get("count", 1) + 1
            if entry["count"] >= 3:
                hot_items.append({**it, "count": entry["count"]})
        else:
            seen_urls[url] = {
                "title": it["title"][:60],
                "url": url,
                "first_seen": today,
                "last_seen": today,
                "count": 1,
            }
            new_items.append(it)

    # 6. 按热度降序，最多保留 max_items
    new_items.sort(key=lambda x: -(x.get("points", 0) + x.get("stars", 0)))
    return new_items[:max_items], hot_items


# ---------------------------------------------------------------------------
# "为什么关注" 生成（基于关键词规则 + 用户画像）
# ---------------------------------------------------------------------------

def generate_why(item: dict, topic_id: str) -> str:
    """规则驱动的 why 生成，面向 P6/P7 前端工程师 + AI 工具链方向。"""
    title = item.get("title", "")
    summary = item.get("summary", "")
    combo = (title + " " + summary).lower()
    source = item.get("source", "")

    # Claude Code 版本
    if "claude-code" in item.get("repo", "") or "claude code" in combo:
        if re.search(r"v\d+\.\d+\.\d+", title):
            ver = re.search(r"v\d+[\d.]+", title)
            ver_str = ver.group() if ver else "新版"
            return f"{ver_str} 更新直接影响 Claude Code 日常编码工作流，release notes 中的 Bug 修复和新特性值得速读"
        return "Claude Code 生态动态，直接影响 AI 辅助编程工具链的使用方式"

    # Anthropic SDK
    if "anthropic-sdk" in item.get("repo", ""):
        return "Anthropic SDK 升级可能包含 breaking change 或新 API，Node.js/Python 项目集成 Claude 时需同步跟进"

    # MCP
    if "mcp" in combo or "model context protocol" in combo or "modelcontextprotocol" in combo:
        return "MCP 生态持续扩展，了解新 MCP server 有助于扩展 Claude Code 工具能力，是 agent 架构面试的高频话题"

    # spec-driven / openspec
    if any(k in combo for k in ["spec-driven", "openspec", "spec kit", "spec-kit", "sdd"]):
        return "Spec 驱动开发是 AI 编程工作流的核心范式，此类工具直接影响与 Claude Code 协作时的需求描述质量"

    # agent / agentic
    if "agent" in combo:
        return "Agentic AI 工具趋势，与 Claude Code Subagent 架构相关，有助于理解多 agent 协作设计模式"

    # 国内 AI 动态
    if topic_id == "china_ai_trends":
        for brand in ["阿里云", "腾讯云", "字节", "百度", "华为", "通义", "文心", "混元", "豆包", "codebuddy"]:
            if brand in combo:
                return f"国内头部厂商 AI 编程工具动态，与海外 Claude Code 竞品对比研究的一手素材"
        return "国内 AI 产品动态，评估国内云厂商 AI 工具链成熟度的参考"

    # HN 高分讨论
    if source == "hn" and item.get("points", 0) >= 100:
        return f"HN 社区高热讨论（{item['points']}pts），英文社区对该方向的技术评价值得参考，可用于评估工具成熟度"

    # GitHub Trending
    if source == "github_trending":
        stars = item.get("stars", 0)
        return f"GitHub 日榜项目（今日+{stars}⭐），快速增长说明社区认可度高，值得评估是否纳入工具链"

    # GitHub Search 高 star
    if source == "github_search" and item.get("stars", 0) >= 200:
        return f"GitHub 高 star 项目（{item['stars']}⭐），已获社区验证，可作为 AI 工程化工具选型的参考"

    # DEV Community
    if source == "dev_community":
        return "DEV 社区实战文章，通常包含可直接参考的代码示例和最佳实践"

    # Anthropic blog / Claude blog
    if "anthropic" in item.get("url", "").lower() or "claude.ai" in item.get("url", "").lower():
        return "Anthropic 官方技术博客，包含模型能力和产品方向的一手信息"

    return "与 AI 工具链 / 前端工程化方向相关，值得关注"


def is_relevant(item: dict, topic_id: str) -> bool:
    """简单相关性过滤：与前端/AI工具/国内云厂商完全无关的条目丢弃。"""
    combo = (item.get("title", "") + " " + item.get("summary", "")).lower()
    url = item.get("url", "").lower()

    irrelevant_signals = [
        "stock market", "ipo", "earnings", "quarterly results",
        "tesla", "elon musk", "twitter",
        "gaming", "minecraft", "fortnite",
        "recipe", "cooking", "travel",
    ]
    if any(s in combo for s in irrelevant_signals):
        return False

    relevant_signals = [
        "claude", "anthropic", "ai", "mcp", "agent", "llm", "gpt",
        "typescript", "javascript", "python", "frontend", "react", "vue",
        "github", "developer", "coding", "programming", "tool", "sdk",
        "spec", "openspec", "阿里", "腾讯", "字节", "百度", "华为",
        "通义", "文心", "混元", "豆包", "大模型", "国内", "云厂商",
        "model", "context", "protocol", "api", "framework",
    ]
    return any(s in combo or s in url for s in relevant_signals)


# ---------------------------------------------------------------------------
# Step 4：构建飞书卡片
# ---------------------------------------------------------------------------

TOPIC_ICONS = {
    "claude_ecosystem": "🔧",
    "ai_tools_discovery": "🛠️",
    "china_ai_trends": "🏢",
    "mcp_ecosystem": "🔌",
    "spec_driven_dev": "📐",
}


def build_card(
    results: dict[str, list[dict]],
    topic_configs: list[dict],
    hot_items: list[dict],
    skipped_sources: list[str],
    today: str,
) -> dict:
    elements = []
    topic_labels = {
        t["id"]: f"{TOPIC_ICONS.get(t['id'], '📌')} {t['name']}"
        for t in topic_configs
    }

    for topic in topic_configs:
        tid = topic["id"]
        items = results.get(tid, [])
        if not items:
            continue
        lines = [f"**{topic_labels[tid]}**"]
        for item in items:
            t = truncate_title(item["title"])
            why = item.get("why", "")
            lines.append(f"· [{t}]({item['url']})")
            if why:
                lines.append(f"  _{why}_")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    if hot_items:
        lines = ["**🔥 持续热点（连续多日高热）**"]
        for item in sorted(hot_items, key=lambda x: -x.get("count", 0))[:5]:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']}) · 已连续 {item['count']} 天")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    skip_msg = "、".join(skipped_sources) if skipped_sources else "无"
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

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {today}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


# ---------------------------------------------------------------------------
# Step 5：写文件 + git push
# ---------------------------------------------------------------------------

def write_and_push(card_data: dict, seen_urls: dict, today: str, dry_run: bool):
    os.makedirs(os.path.join(SCRIPT_DIR, ".ai-news-bot"), exist_ok=True)

    # 写 report
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(card_data, f, ensure_ascii=False, indent=2)

    # 验证 JSON
    with open(REPORT_PATH, encoding="utf-8") as f:
        json.load(f)
    log(f"✅ JSON 验证通过：{REPORT_PATH}")

    # 回写 seen.json
    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)
    log(f"✅ seen.json 已回写（{len(seen_urls)} 条）")

    if dry_run:
        log("--dry-run 模式，跳过 git push")
        print(json.dumps(card_data, ensure_ascii=False, indent=2))
        return

    branch = f"report/{today}"
    try:
        subprocess.run(["git", "config", "user.email", "routine-bot@ai-news"], check=True, cwd=SCRIPT_DIR)
        subprocess.run(["git", "config", "user.name", "AI News Routine"], check=True, cwd=SCRIPT_DIR)
        subprocess.run(["git", "checkout", "-b", branch], check=True, cwd=SCRIPT_DIR)
        subprocess.run(["git", "add", REPORT_PATH, SEEN_JSON], check=True, cwd=SCRIPT_DIR)
        subprocess.run(["git", "commit", "-m", f"chore: daily report {today}"], check=True, cwd=SCRIPT_DIR)
        # push with retry + exponential backoff
        for attempt, wait in enumerate([0, 2, 4, 8, 16], 1):
            if wait:
                time.sleep(wait)
            result = subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=SCRIPT_DIR,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                log(f"✅ 推送成功：origin/{branch}")
                break
            log(f"  推送失败（第{attempt}次）: {result.stderr.strip()}")
        else:
            log("❌ 推送全部失败，打印卡片内容到日志：")
            print(json.dumps(card_data, ensure_ascii=False, indent=2))
    except subprocess.CalledProcessError as e:
        log(f"❌ git 操作失败：{e}")
        print(json.dumps(card_data, ensure_ascii=False, indent=2))
        sys.exit(1)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片，不 git push")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"=== AI 资讯监控 Agent 启动 · {today} ===")

    # Step 1
    config = load_config()
    filters = config.get("filters", {})
    lookback_hours = filters.get("lookback_hours", 24)
    max_items = filters.get("max_items_per_topic", 5)
    exclude_kws = filters.get("exclude_keywords", [])
    ttl_days = filters.get("seen_ttl_days", 7)
    skip_if_empty = config.get("output", {}).get("skip_if_empty", True)

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen_urls = load_seen(ttl_days)

    topics = config.get("topics", [])
    skipped_sources: list[str] = []
    results: dict[str, list[dict]] = {}
    all_hot_items: list[dict] = []

    # Step 2 & 3：按 topic 抓取
    for topic in topics:
        tid = topic["id"]
        sources = topic.get("sources", {})
        log(f"\n── Topic: {topic['name']} ──")
        raw_items: list[dict] = []

        # A. GitHub Releases
        if "github_releases" in sources:
            repos = sources["github_releases"]
            items = fetch_github_releases(repos, since)
            log(f"  Releases 共 {len(items)} 条")
            raw_items.extend(items)

        # B. GitHub Trending
        if "github_trending" in sources:
            items = fetch_github_trending(sources["github_trending"])
            log(f"  Trending 共 {len(items)} 条（过滤后）")
            raw_items.extend(items)

        # C. GitHub Search
        if "github_search" in sources:
            queries = sources["github_search"].get("queries", [])
            items, rate_limited = fetch_github_search(queries, since)
            if rate_limited:
                skipped_sources.append("GitHub Search（Rate Limit 耗尽）")
            log(f"  Search 共 {len(items)} 条")
            raw_items.extend(items)

        # D. HN
        if "hn" in sources:
            hn_cfg = sources["hn"]
            items = fetch_hn(
                hn_cfg.get("keywords", []),
                hn_cfg.get("min_points", 30),
                since,
            )
            log(f"  HN 共 {len(items)} 条")
            raw_items.extend(items)

        # E. Web
        if "web" in sources:
            for src in sources["web"]:
                items, skip_reason = fetch_web_source(src, since)
                if skip_reason:
                    skipped_sources.append(skip_reason)
                raw_items.extend(items)

        # F. DEV Community
        if "dev_community" in sources:
            dev_cfg = sources["dev_community"]
            tags = dev_cfg.get("tags", []) if isinstance(dev_cfg, dict) else dev_cfg
            items = fetch_dev_community(tags, since)
            log(f"  DEV 共 {len(items)} 条")
            raw_items.extend(items)

        # Step 3：过滤去重
        new_items, hot_items = filter_items(
            raw_items, exclude_kws, since, seen_urls, today, max_items
        )
        all_hot_items.extend(hot_items)

        # 相关性评估 + why 生成
        final_items = []
        for it in new_items:
            if not is_relevant(it, tid):
                log(f"  丢弃（相关性低）: {it['title'][:40]}")
                continue
            it["why"] = generate_why(it, tid)
            final_items.append(it)

        results[tid] = final_items
        log(f"  最终保留 {len(final_items)} 条")

    # 全局持续热点去重
    seen_hot: set[str] = set()
    deduped_hot = []
    for it in all_hot_items:
        if it["url"] not in seen_hot:
            seen_hot.add(it["url"])
            deduped_hot.append(it)

    total = sum(len(v) for v in results.values())
    log(f"\n合计 {total} 条新资讯，{len(deduped_hot)} 条持续热点")

    if total == 0 and skip_if_empty:
        log("所有 topic 均无内容且 skip_if_empty=true，退出")
        sys.exit(0)

    # Step 4：构建卡片
    card_data = build_card(results, topics, deduped_hot, list(set(skipped_sources)), today)

    # Step 5：写入并推送
    write_and_push(card_data, seen_urls, today, dry_run=args.dry_run)
    log("=== 完成 ===")


if __name__ == "__main__":
    main()
