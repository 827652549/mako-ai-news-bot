#!/usr/bin/env python3
"""AI News Monitoring Bot

Reads watch.yaml, fetches from multiple sources, builds a Feishu card,
writes .ai-news-bot/latest-report.json, and optionally pushes to a dated branch.

Usage:
  python run.py                # Full run: fetch + write + git push to report/YYYY-MM-DD
  python run.py --report-only  # Fetch + write only, skip git operations (for dev/testing)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urljoin, quote

import requests
import yaml

# ── Paths & Env ───────────────────────────────────────────────────────────────

WATCH_YAML = "watch.yaml"
SEEN_JSON = ".ai-news-bot/seen.json"
REPORT_JSON = ".ai-news-bot/latest-report.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GH_HEADERS: dict = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": BROWSER_UA})

# ── Utilities ─────────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def in_window(dt: Optional[datetime], lookback_hours: int) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= now_utc() - timedelta(hours=lookback_hours)


def title_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def truncate_title(title: str, max_len: int = 30) -> str:
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


def contains_any(text: str, keywords: list) -> bool:
    tl = text.lower()
    return any(kw.lower() in tl for kw in keywords)


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def safe_get(url: str, headers: dict = None, timeout: int = 20) -> Optional[requests.Response]:
    try:
        return SESSION.get(url, headers=headers or {}, timeout=timeout)
    except Exception as e:
        print(f"  [!] GET {url[:80]}: {e}", file=sys.stderr)
        return None


def jina_get(url: str) -> Optional[str]:
    resp = safe_get(f"https://r.jina.ai/{url}")
    if resp and resp.status_code == 200 and len(resp.text) > 500:
        return resp.text
    return None


def web_get(url: str, use_jina: bool = False) -> Optional[str]:
    if use_jina:
        return jina_get(url)
    resp = safe_get(url, headers={"User-Agent": BROWSER_UA})
    if resp and resp.status_code == 200 and len(resp.text) > 500:
        return resp.text
    # Fallback to Jina Reader
    return jina_get(url)


# ── Step 1: Config & Dedup State ──────────────────────────────────────────────


def load_config() -> dict:
    if not os.path.exists(WATCH_YAML):
        print(f"ERROR: {WATCH_YAML} not found", file=sys.stderr)
        sys.exit(1)
    with open(WATCH_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen(ttl_days: int) -> dict:
    if not os.path.exists(SEEN_JSON):
        return {}
    try:
        with open(SEEN_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        urls = data.get("urls", {})
        cutoff = str(now_utc().date() - timedelta(days=ttl_days))
        return {k: v for k, v in urls.items() if v.get("last_seen", "2000-01-01") >= cutoff}
    except Exception:
        return {}


def save_seen(seen: dict):
    os.makedirs(os.path.dirname(SEEN_JSON), exist_ok=True)
    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump({"urls": seen}, f, ensure_ascii=False, indent=2)


# ── Step 2A: GitHub Releases ──────────────────────────────────────────────────


def fetch_releases(repo: str, lookback_hours: int) -> list:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
    resp = safe_get(url, headers=GH_HEADERS)
    if not resp or resp.status_code != 200:
        return []
    out = []
    for r in resp.json():
        pub = parse_iso(r.get("published_at", ""))
        if not in_window(pub, lookback_hours):
            continue
        body = (r.get("body") or "").strip()[:300]
        out.append({
            "title": f"{repo} {r['tag_name']}",
            "url": r.get("html_url", ""),
            "source": "github_releases",
            "published_at": r.get("published_at", ""),
            "extra": {"repo": repo, "version": r["tag_name"], "body": body},
        })
    return out


# ── Step 2B: GitHub Trending ──────────────────────────────────────────────────


def fetch_trending(must_match_any: list, lookback_hours: int) -> list:
    urls = [
        "https://github.com/trending?since=daily",
        "https://github.com/trending?since=daily&spoken_language_code=zh",
    ]
    seen_repos: set = set()
    out = []

    for url in urls:
        html = web_get(url)
        if not html:
            continue

        # Try article blocks first (GitHub's current HTML)
        articles = re.findall(
            r'<article[^>]*class="Box-row"[^>]*>(.*?)</article>', html, re.DOTALL
        )

        if articles:
            for article in articles:
                repo_m = re.search(r'href="/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"', article)
                if not repo_m:
                    continue
                repo = repo_m.group(1).strip()
                if repo in seen_repos:
                    continue
                seen_repos.add(repo)

                desc_m = re.search(r'<p[^>]*>(.*?)</p>', article, re.DOTALL)
                desc = re.sub(r"\s+", " ", strip_html(desc_m.group(1))).strip() if desc_m else ""

                stars_m = re.search(r'([\d,]+)\s*stars?\s*today', article, re.IGNORECASE)
                stars_today = stars_m.group(1).replace(",", "") if stars_m else "0"

                combined = f"{repo} {desc}"
                if must_match_any and not contains_any(combined, must_match_any):
                    continue

                out.append({
                    "title": f"{repo} · 今日 {stars_today}⭐",
                    "url": f"https://github.com/{repo}",
                    "source": "github_trending",
                    "published_at": now_utc().isoformat(),
                    "extra": {"repo": repo, "description": desc, "stars_today": stars_today},
                })
        else:
            # Fallback: parse repo hrefs from raw HTML
            for m in re.finditer(r'href="/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"', html):
                repo = m.group(1).strip()
                if repo in seen_repos:
                    continue
                first_segment = repo.split("/")[0]
                if first_segment in ("trending", "login", "about", "features", "settings", "explore", "marketplace"):
                    continue
                seen_repos.add(repo)
                if must_match_any and not contains_any(repo, must_match_any):
                    continue
                out.append({
                    "title": f"{repo} · GitHub Trending",
                    "url": f"https://github.com/{repo}",
                    "source": "github_trending",
                    "published_at": now_utc().isoformat(),
                    "extra": {"repo": repo, "description": "", "stars_today": "?"},
                })

        if len(out) >= 15:
            break

    return out


# ── Step 2C: GitHub Search ────────────────────────────────────────────────────


def fetch_gh_search(query: str, lookback_hours: int, skipped: list) -> list:
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={quote(query)}&sort=stars&order=desc&per_page=10"
    )
    resp = safe_get(url, headers=GH_HEADERS)
    if not resp:
        return []
    if resp.headers.get("X-RateLimit-Remaining", "1") == "0":
        label = "GitHub Search（Rate Limit 耗尽）"
        if label not in skipped:
            skipped.append(label)
        return []
    if resp.status_code != 200:
        return []

    out = []
    for repo in resp.json().get("items", []):
        created = parse_iso(repo.get("created_at", ""))
        updated = parse_iso(repo.get("updated_at", ""))
        if not in_window(created, lookback_hours) and not in_window(updated, lookback_hours):
            continue
        out.append({
            "title": f"{repo['full_name']} · {repo.get('stargazers_count', 0)}⭐",
            "url": repo.get("html_url", ""),
            "source": "github_search",
            "published_at": repo.get("updated_at") or repo.get("created_at", ""),
            "extra": {
                "repo": repo["full_name"],
                "description": repo.get("description") or "",
                "stars": repo.get("stargazers_count", 0),
                "language": repo.get("language") or "",
            },
        })
    return out


# ── Step 2D: HN Algolia ───────────────────────────────────────────────────────


def fetch_hn(keywords: list, min_points: int, lookback_hours: int,
             title_must_match: list = None) -> list:
    """Fetch HN stories matching any of the given keywords.

    title_must_match: if provided, only keep stories whose title contains at
    least one of these terms. HN Algolia does full-text matching (title+body),
    so without this filter off-topic stories often slip through.
    """
    since = int((now_utc() - timedelta(hours=lookback_hours)).timestamp())
    seen_ids: set = set()
    out = []
    for kw in keywords:
        url = (
            f"https://hn.algolia.com/api/v1/search"
            f"?query={quote(kw)}&tags=story"
            f"&numericFilters=created_at_i>{since},points>{min_points}"
            f"&hitsPerPage=5"
        )
        resp = safe_get(url)
        if not resp or resp.status_code != 200:
            continue
        for hit in resp.json().get("hits", []):
            oid = hit.get("objectID", "")
            if oid in seen_ids:
                continue
            title = hit.get("title", "")
            # Apply optional title-level keyword gate to filter body-only matches
            if title_must_match and not contains_any(title, title_must_match):
                continue
            seen_ids.add(oid)
            hn_url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            out.append({
                "title": title,
                "url": hn_url,
                "source": "hn",
                "published_at": datetime.fromtimestamp(
                    hit.get("created_at_i", 0), tz=timezone.utc
                ).isoformat(),
                "extra": {
                    "points": hit.get("points", 0),
                    "num_comments": hit.get("num_comments", 0),
                    "hn_id": oid,
                },
            })
    return out


# ── Step 2E: Web Scraping ─────────────────────────────────────────────────────


def fetch_web_source(cfg: dict, exclude_keywords: list) -> list:
    url = cfg.get("url", "")
    use_jina = cfg.get("use_jina", False)
    must_match = cfg.get("must_match_any", [])

    html = web_get(url, use_jina=use_jina)
    if not html:
        return []

    # Navigation-style prefixes to skip (too generic to be articles)
    nav_prefixes = (
        "skip to", "sign in", "log in", "sign up", "try ", "get started",
        "learn more", "read more", "see all", "view all", "back to",
        "cookie", "privacy", "terms", "about us",
    )

    out = []
    for m in re.finditer(
        r'<a\s[^>]*href=["\']([^"\']{5,})["\'][^>]*>\s*([^<]{10,300})\s*</a>',
        html,
        re.IGNORECASE,
    ):
        href = m.group(1).strip()
        title = re.sub(r"\s+", " ", strip_html(m.group(2))).strip()

        # Require at least 15 chars and 3 words to look like an article title
        if len(title) < 15 or len(title) > 250:
            continue
        if len(title.split()) < 3:
            continue
        # Skip navigation-style titles
        if title.lower().startswith(nav_prefixes):
            continue
        # Skip titles that are just a single capitalized word (nav labels)
        if re.fullmatch(r"[A-Z][a-zA-Z]+", title):
            continue

        if not href.startswith(("http://", "https://")):
            href = urljoin(url, href)
        if href == url or href.rstrip("/") == url.rstrip("/"):
            continue

        if must_match and not contains_any(title, must_match):
            continue
        if contains_any(title, exclude_keywords):
            continue

        out.append({
            "title": title,
            "url": href,
            "source": "web",
            "published_at": now_utc().isoformat(),
            "extra": {"source_url": url},
        })
        if len(out) >= 15:
            break
    return out


# ── Step 2F: DEV Community ────────────────────────────────────────────────────


def fetch_dev(tags: list, lookback_hours: int, skipped: list) -> list:
    seen_ids: set = set()
    out = []
    for tag in tags:
        resp = safe_get(f"https://dev.to/api/articles?tag={tag}&per_page=10&top=1")
        if not resp:
            label = "DEV Community（网络错误）"
            if label not in skipped:
                skipped.append(label)
            continue
        if resp.status_code == 403:
            label = "DEV Community（403）"
            if label not in skipped:
                skipped.append(label)
            continue
        if resp.status_code != 200:
            continue
        for art in resp.json():
            aid = art.get("id")
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            pub = parse_iso(art.get("published_at", ""))
            if not in_window(pub, lookback_hours):
                continue
            out.append({
                "title": art.get("title", ""),
                "url": art.get("url", ""),
                "source": "dev_community",
                "published_at": art.get("published_at", ""),
                "extra": {
                    "reactions": art.get("positive_reactions_count", 0),
                    "comments": art.get("comments_count", 0),
                    "tags": art.get("tag_list", []),
                },
            })
    return out


# ── Step 3: Filter, Dedup, Limit ──────────────────────────────────────────────


def norm_url(url: str) -> str:
    return url.rstrip("/").split("?")[0].split("#")[0]


def dedup_filter_limit(
    items: list,
    seen: dict,
    exclude: list,
    lookback_hours: int,
    max_items: int,
    hot_items: list,
    today: str,
) -> list:
    # Same-repo same-day release dedup: keep only newest version per repo
    by_repo: dict = {}
    non_release = []
    for item in items:
        if item["source"] == "github_releases":
            repo = item["extra"].get("repo", "")
            existing = by_repo.get(repo)
            if not existing or item.get("published_at", "") > existing.get("published_at", ""):
                by_repo[repo] = item
        else:
            non_release.append(item)
    items = list(by_repo.values()) + non_release

    kept: dict = {}  # norm_url -> item

    for item in items:
        title = item.get("title", "").strip()
        url = item.get("url", "").strip()
        if not title or not url:
            continue

        # Time window check (web/trending use now() so always pass)
        source = item.get("source", "")
        if source not in ("web", "github_trending"):
            pub = parse_iso(item.get("published_at", ""))
            if pub and not in_window(pub, lookback_hours):
                continue

        # Exclude keywords
        if contains_any(title, exclude):
            continue

        nu = norm_url(url)

        # Cross-run dedup via seen.json → hot item tracking
        if nu in seen:
            seen[nu]["last_seen"] = today
            seen[nu]["count"] = seen[nu].get("count", 1) + 1
            if seen[nu]["count"] >= 3:
                entry = {**item, "count": seen[nu]["count"]}
                if not any(h["url"] == url for h in hot_items):
                    hot_items.append(entry)
            continue

        # Within-run URL dedup (keep higher quality)
        if nu in kept:
            e = kept[nu]["extra"]
            n = item["extra"]
            e_score = e.get("stars", e.get("points", e.get("reactions", 0))) or 0
            n_score = n.get("stars", n.get("points", n.get("reactions", 0))) or 0
            if n_score > e_score:
                kept[nu] = item
            continue

        # Within-run title similarity dedup >80%
        dup_key = None
        for k, existing in kept.items():
            if title_sim(title, existing["title"]) > 0.8:
                dup_key = k
                break
        if dup_key:
            e = kept[dup_key]["extra"]
            n = item["extra"]
            e_score = e.get("stars", e.get("points", 0)) or 0
            n_score = n.get("stars", n.get("points", 0)) or 0
            if n_score > e_score:
                kept[dup_key] = item
            continue

        kept[nu] = item

    # Register surviving items in seen
    output = list(kept.values())
    for item in output:
        nu = norm_url(item["url"])
        seen[nu] = {
            "title": truncate_title(item["title"], 60),
            "url": item["url"],
            "first_seen": today,
            "last_seen": today,
            "count": 1,
        }

    # Sort by score (stars / points / reactions) descending
    def score(item: dict) -> int:
        e = item["extra"]
        return e.get("stars", e.get("points", e.get("reactions", 0))) or 0

    output.sort(key=score, reverse=True)
    return output[:max_items]


# ── Step 4: "Why" Generation ──────────────────────────────────────────────────


def why_via_claude_api(items_by_topic: dict, topic_labels: dict) -> dict:
    """Call Claude Haiku to generate concise 'why关注' for all items in one batch."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        all_items = []
        for tid, items in items_by_topic.items():
            for item in items:
                all_items.append((tid, item))

        numbered = [
            f"[{i}] topic={topic_labels.get(tid, tid)} | title={item['title']} | "
            f"src={item['source']} | extra={json.dumps(item['extra'], ensure_ascii=False)[:80]}"
            for i, (tid, item) in enumerate(all_items)
        ]

        prompt = (
            "你是一名为P6/P7前端工程师撰写AI技术资讯摘要的编辑。\n\n"
            "用户画像：重点关注Claude Code工具链演进、AI编程工具(spec-driven/MCP/agent)、"
            "国内大厂AI产品动态，准备高级工程师面试。\n\n"
            "对每条资讯写一句30-50字的\"为什么关注\"：\n"
            "- 说明与前端/AI工具/国内厂商的具体关联\n"
            "- 优先：①日常编码工作流直接影响 ②面试高频考点 ③技术趋势判断依据\n"
            "- 禁止套话：不写\"建议关注\"\"建议收藏\"\"强烈推荐\"\n"
            "- 如与用户完全无关，输出 skip\n\n"
            "格式（每行一条）：\n[序号] 理由\n\n"
            "资讯：\n" + "\n".join(numbered)
        )

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        why_map: dict = {}
        for line in text.split("\n"):
            m = re.match(r"\[(\d+)\]\s+(.+)", line.strip())
            if m:
                why_map[int(m.group(1))] = m.group(2).strip()

        result: dict = {}
        global_i = 0
        for tid, items in items_by_topic.items():
            result[tid] = []
            for item in items:
                why = why_map.get(global_i, "")
                if why.lower() != "skip":
                    result[tid].append({**item, "why": why})
                global_i += 1
        return result

    except Exception as e:
        print(f"  [!] Claude API why-gen failed: {e}", file=sys.stderr)
        return {}


def why_template(item: dict) -> str:
    """Rule-based fallback for 'why关注' text."""
    src = item.get("source", "")
    ex = item.get("extra", {})
    title = item.get("title", "")

    if src == "github_releases":
        repo = ex.get("repo", "")
        version = ex.get("version", "")
        body = (ex.get("body") or "").strip()
        first_line = body.split("\n")[0][:60].strip() if body else ""
        if "claude-code" in repo.lower():
            suffix = f"，{first_line}" if first_line else ""
            return f"Claude Code {version} 更新{suffix}，直接影响 AI 编程工作流"
        elif "anthropic" in repo.lower() or "sdk" in repo.lower():
            return f"{repo} {version}，SDK 版本变更影响 Claude API 集成，注意 breaking changes"
        elif "lark" in repo.lower():
            return f"{repo} {version}，飞书 SDK 更新，关注 API 兼容性变化"
        else:
            return f"{repo} {version} 发布，关注其在 AI 工具链生态中的最新进展"

    elif src == "github_trending":
        stars = ex.get("stars_today", "?")
        desc = (ex.get("description") or "")[:40]
        return f"GitHub 今日热榜，{stars} 颗星增量，{desc or '关注技术方向与应用场景'}"

    elif src == "github_search":
        stars = ex.get("stars", 0)
        lang = ex.get("language") or "AI"
        return f"近期快速增长的 {lang} 项目，{stars}⭐，关注其在 AI 工具链中的应用场景"

    elif src == "hn":
        pts = ex.get("points", 0)
        return f"HN {pts} 分，英文技术社区热点讨论，反映 AI 工程师当前关注焦点"

    elif src == "dev_community":
        tags = ex.get("tags", [])
        reactions = ex.get("reactions", 0)
        tag_str = "/".join(tags[:3]) if tags else "AI"
        return f"DEV Community {tag_str} 热门，{reactions} 赞，关注实战技术经验"

    elif src == "web":
        source_url = ex.get("source_url", "")
        item_url = item.get("url", "")
        if "anthropic" in source_url or "anthropic" in item_url:
            return "Anthropic 官方博客更新，关注 Claude 功能与产品路线图最新动态"
        elif "claude.ai" in source_url or "claude.com" in source_url or "claude.com" in item_url:
            return "Claude 产品页面更新，关注 Claude Code 功能演进与企业级应用方向"
        elif "leiphone" in source_url:
            return "雷峰网报道，关注国内大厂 AI 产品与技术生态最新动向"
        elif "smarthey" in source_url:
            return "国内 AI 媒体报道，关注大厂 AI 技术动态与产品竞争格局"
        elif "dev.to" in source_url or "dev.to" in item_url:
            return "DEV 社区技术文章，关注 AI 工具实战应用与前端工程化最佳实践"
        else:
            return "技术资讯，关注 AI 工具链与前端工程化最新进展"

    return "AI 技术动态，关注工具链演进与工程化最佳实践"


# ── Step 5: Build Card ────────────────────────────────────────────────────────

TOPIC_ICONS = {
    "claude_ecosystem": "🔧",
    "ai_tools_discovery": "🛠️",
    "china_ai_trends": "🏢",
    "mcp_ecosystem": "🔌",
    "spec_driven_dev": "📐",
}


def build_card(
    results: dict,
    topic_labels: dict,
    hot_items: list,
    skipped: list,
    today: str,
) -> dict:
    elements: list = []

    for topic_id, items in results.items():
        if not items:
            continue
        icon = TOPIC_ICONS.get(topic_id, "📌")
        label = f"{icon} {topic_labels.get(topic_id, topic_id)}"
        lines = [f"**{label}**"]
        for item in items:
            t = truncate_title(item["title"])
            why = item.get("why") or why_template(item)
            lines.append(f"· [{t}]({item['url']})")
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
            lines.append(f"· [{t}]({item['url']}) · 已连续 {item.get('count', 0)} 天")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    skip_msg = "、".join(sorted(set(skipped))) if skipped else "无"
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": (
                "数据来源：GitHub · HN · 雷峰网 · DEV Community｜"
                "修改关注维度：编辑 watch.yaml\n"
                f"⚠️ 跳过：{skip_msg}"
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


# ── Write Files ───────────────────────────────────────────────────────────────


def write_report(card: dict, seen: dict):
    os.makedirs(".ai-news-bot", exist_ok=True)

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    # Validate parseable immediately after write
    with open(REPORT_JSON, "r", encoding="utf-8") as f:
        json.load(f)

    save_seen(seen)
    print(f"  ✓ {REPORT_JSON}")
    print(f"  ✓ {SEEN_JSON}")


def git_push_report(today: str):
    branch = f"report/{today}"
    subprocess.run(["git", "config", "user.email", "routine-bot@ai-news"], check=True)
    subprocess.run(["git", "config", "user.name", "AI News Routine"], check=True)

    existing = subprocess.run(
        ["git", "branch", "--list", branch], capture_output=True, text=True
    )
    if branch in existing.stdout:
        subprocess.run(["git", "checkout", branch], check=True)
    else:
        subprocess.run(["git", "checkout", "-b", branch], check=True)

    subprocess.run(["git", "add", REPORT_JSON, SEEN_JSON], check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    )
    if not status.stdout.strip():
        print("  ✓ No changes to commit")
        return

    subprocess.run(
        ["git", "commit", "-m", f"chore: daily report {today}"], check=True
    )
    result = subprocess.run(
        ["git", "push", "-u", "origin", branch], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [!] Push failed: {result.stderr}", file=sys.stderr)
        print("  [INFO] Report content saved to " + REPORT_JSON)
    else:
        print(f"  ✓ Pushed → origin/{branch}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AI News Bot")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write report files only, skip git branch/push operations",
    )
    args = parser.parse_args()

    today = now_utc().strftime("%Y-%m-%d")
    print(f"\n=== AI News Bot · {today} ===\n")

    config = load_config()
    filters = config.get("filters", {})
    lookback_hours: int = filters.get("lookback_hours", 24)
    max_items: int = filters.get("max_items_per_topic", 5)
    exclude_kw: list = filters.get("exclude_keywords", [])
    ttl_days: int = filters.get("seen_ttl_days", 7)
    skip_if_empty: bool = config.get("output", {}).get("skip_if_empty", True)

    seen = load_seen(ttl_days)
    topics = config.get("topics", [])
    topic_labels = {t["id"]: t["name"] for t in topics}

    skipped: list = []
    hot_items: list = []
    raw: dict = {}

    for topic in topics:
        tid = topic["id"]
        sources = topic.get("sources", {})
        items: list = []
        print(f"[{tid}]")

        if "github_releases" in sources:
            for repo in sources["github_releases"]:
                print(f"  → releases/{repo}")
                try:
                    items.extend(fetch_releases(repo, lookback_hours))
                except Exception as e:
                    print(f"  [!] releases/{repo}: {e}", file=sys.stderr)

        if "github_trending" in sources:
            cfg = sources["github_trending"]
            print("  → github_trending")
            try:
                items.extend(fetch_trending(cfg.get("must_match_any", []), lookback_hours))
            except Exception as e:
                print(f"  [!] trending: {e}", file=sys.stderr)
                skipped.append("GitHub Trending")

        if "github_search" in sources:
            for q in sources["github_search"].get("queries", []):
                print(f"  → search: {q[:60]}")
                try:
                    items.extend(fetch_gh_search(q, lookback_hours, skipped))
                except Exception as e:
                    print(f"  [!] search: {e}", file=sys.stderr)

        if "hn" in sources:
            cfg = sources["hn"]
            hn_kws = cfg.get("keywords", [])
            print(f"  → hn ({len(hn_kws)} keywords, min_pts={cfg.get('min_points', 20)})")
            try:
                items.extend(
                    fetch_hn(
                        hn_kws,
                        cfg.get("min_points", 20),
                        lookback_hours,
                        # Title-gate: filter out body-only matches
                        title_must_match=hn_kws,
                    )
                )
            except Exception as e:
                print(f"  [!] hn: {e}", file=sys.stderr)
                skipped.append("HN Algolia")

        if "web" in sources:
            for src in sources["web"]:
                u = src.get("url", "")
                print(f"  → web: {u}")
                try:
                    items.extend(fetch_web_source(src, exclude_kw))
                except Exception as e:
                    domain = u.split("/")[2] if "//" in u else u
                    print(f"  [!] web/{domain}: {e}", file=sys.stderr)
                    skipped.append(f"Web({domain})")

        if "dev_community" in sources:
            cfg = sources["dev_community"]
            print(f"  → dev_community: {cfg.get('tags', [])}")
            try:
                items.extend(fetch_dev(cfg.get("tags", []), lookback_hours, skipped))
            except Exception as e:
                print(f"  [!] dev: {e}", file=sys.stderr)

        filtered = dedup_filter_limit(
            items, seen, exclude_kw, lookback_hours, max_items, hot_items, today
        )
        print(f"  → kept {len(filtered)} items\n")
        raw[tid] = filtered

    total = sum(len(v) for v in raw.values())
    if total == 0 and skip_if_empty:
        print("[INFO] All topics empty and skip_if_empty=true → exiting without writing")
        return

    # Generate "why" texts via Claude API if configured, otherwise use templates
    results = raw
    if ANTHROPIC_API_KEY and total > 0:
        print("→ Generating 'why' texts via Claude API...")
        enriched = why_via_claude_api(raw, topic_labels)
        if enriched:
            results = enriched
            print("  ✓ Why texts generated\n")

    card = build_card(results, topic_labels, hot_items, skipped, today)

    print("→ Writing report files:")
    write_report(card, seen)

    if args.report_only:
        print("\n[--report-only] Skipped git operations.")
    else:
        print("\n→ Pushing to git:")
        git_push_report(today)

    print("\n=== Done ===\n")


if __name__ == "__main__":
    main()
