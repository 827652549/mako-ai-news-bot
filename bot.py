#!/usr/bin/env python3
"""
AI News Bot — daily monitoring agent.

Steps:
  1. Read watch.yaml + seen.json
  2. Fetch from: github_releases, github_trending, github_search, HN, web, dev_community
  3. Filter, deduplicate
  4. Build Feishu interactive card (Python dict → json.dump, never manual JSON strings)
  5. Write .ai-news-bot/latest-report.json + seen.json, push to report/YYYY-MM-DD branch
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests
import yaml

# ── Paths ──────────────────────────────────────────────────────────
REPO_DIR   = os.path.dirname(os.path.abspath(__file__))
WATCH_YAML = os.path.join(REPO_DIR, "watch.yaml")
BOT_DIR    = os.path.join(REPO_DIR, ".ai-news-bot")
SEEN_JSON  = os.path.join(BOT_DIR, "seen.json")
REPORT_JSON = os.path.join(BOT_DIR, "latest-report.json")

NOW   = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "ai-news-bot/1.0",
}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

WEB_UA = {"User-Agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)}


# ═══════════════════════════════════════════════════════════════════
# STEP 1 — Config & seen.json
# ═══════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not os.path.exists(WATCH_YAML):
        print("ERROR: watch.yaml not found", file=sys.stderr)
        sys.exit(1)
    with open(WATCH_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> dict:
    if not os.path.exists(SEEN_JSON):
        return {}
    with open(SEEN_JSON, "r", encoding="utf-8") as f:
        return json.load(f).get("urls", {})


def clean_seen(seen: dict, ttl_days: int) -> dict:
    cutoff = (NOW - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
    return {u: v for u, v in seen.items() if v.get("last_seen", "0") >= cutoff}


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def truncate_title(title: str, max_len: int = 30) -> str:
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-", "/"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


def within_window(dt_str: str | None, hours: int) -> bool:
    if not dt_str:
        return True  # unknown → include
    try:
        s = dt_str.rstrip("Z").split("+")[0].split(".")[0]
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt >= (NOW - timedelta(hours=hours))
    except Exception:
        return True


def has_exclude(text: str, kws: list[str]) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in kws)


def has_any(text: str, kws: list[str]) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in kws)


def relative_date_q(query: str) -> str:
    """Replace 'pushed:>Ndays' / 'created:>Ndays' with absolute ISO dates."""
    def replace_rel(m):
        field, days = m.group(1), int(m.group(2))
        date = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
        return f"{field}:>{date}"
    return re.sub(r"(pushed|created):>(\d+)days", replace_rel, query)


def jina_fetch(url: str) -> str:
    try:
        r = requests.get(f"https://r.jina.ai/{url}", headers=WEB_UA, timeout=25)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def web_fetch(url: str, use_jina: bool = False) -> str:
    if use_jina:
        return jina_fetch(url)
    try:
        r = requests.get(url, headers=WEB_UA, timeout=20, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except Exception:
        pass
    return jina_fetch(url)


# ═══════════════════════════════════════════════════════════════════
# STEP 2 — Source fetchers
# ═══════════════════════════════════════════════════════════════════

# ── A: GitHub Releases ─────────────────────────────────────────────
RELEASE_LOOKBACK_HOURS = 72   # releases don't ship daily; use wider window

def fetch_releases(repo: str, hours: int, excl: list, skipped: list) -> list[dict]:
    items = []
    effective_hours = max(hours, RELEASE_LOOKBACK_HOURS)
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases?per_page=5",
            headers=GH_HEADERS, timeout=15
        )
        if r.status_code != 200:
            skipped.append(f"GH Releases/{repo}({r.status_code})")
            return items
        candidates = [
            rel for rel in r.json()
            if not rel.get("draft")
            and not rel.get("prerelease")
            and within_window(rel.get("published_at"), effective_hours)
        ]
        if not candidates:
            return items
        # Same-repo same-day dedup: keep latest published_at only
        best = max(candidates, key=lambda x: x.get("published_at", ""))
        tag   = best.get("tag_name", "")
        name  = best.get("name", "") or tag
        title = f"{repo.split('/')[1]} {tag} · {name}".strip(" ·")
        body  = (best.get("body") or "")[:600]
        if has_exclude(title + body, excl):
            return items
        items.append({
            "title":  title,
            "url":    best.get("html_url", ""),
            "body":   body,
            "pub":    best.get("published_at", ""),
            "source": "github_releases",
            "repo":   repo,
        })
    except Exception as e:
        skipped.append(f"GH Releases/{repo}({type(e).__name__})")
    return items


# ── B: GitHub Trending ─────────────────────────────────────────────
def fetch_trending(cfg: dict, excl: list, skipped: list) -> list[dict]:
    must = cfg.get("must_match_any", [])
    items: list[dict] = []
    seen_urls: set[str] = set()

    for lang in ["", "TypeScript", "JavaScript", "Python"]:
        params = "since=daily" + (f"&l={lang}" if lang else "")
        try:
            r = requests.get(
                f"https://github.com/trending?{params}", headers=WEB_UA, timeout=20
            )
            if r.status_code != 200:
                continue
            html = r.text

            # Parse each <article class="Box-row"> block
            for block in re.split(r'<article[^>]+class="Box-row"', html)[1:]:
                # Repo path lives in the <h2> heading, NOT the sponsor button
                path_m = re.search(
                    r'<h2[^>]*>.*?href="/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"',
                    block, re.DOTALL
                )
                if not path_m:
                    continue
                repo_path = path_m.group(1)
                repo_url  = f"https://github.com/{repo_path}"
                if repo_url in seen_urls:
                    continue

                # description
                desc_m = re.search(r'<p[^>]*col-9[^>]*>(.*?)</p>', block, re.DOTALL)
                desc   = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else ""

                # stars today
                st_m       = re.search(r"([\d,]+)\s+stars today", block)
                stars_today = int(st_m.group(1).replace(",", "")) if st_m else 0

                combined = f"{repo_path} {desc}".lower()
                if not has_any(combined, must):
                    continue
                if has_exclude(combined, excl):
                    continue

                seen_urls.add(repo_url)
                items.append({
                    "title":      f"{repo_path} · {stars_today}⭐ today",
                    "url":        repo_url,
                    "body":       desc,
                    "source":     "github_trending",
                    "stars_today": stars_today,
                })
        except Exception:
            pass

    if not items:
        skipped.append("GitHub Trending(no keyword match)")
    return items


# ── C: GitHub Search ───────────────────────────────────────────────
def fetch_search(queries: list[str], excl: list, skipped: list) -> list[dict]:
    items: list[dict] = []
    seen_repos: set[str] = set()

    for raw_q in queries:
        q = relative_date_q(raw_q)
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 10},
                headers=GH_HEADERS, timeout=15,
            )
            remaining = int(r.headers.get("X-RateLimit-Remaining", 1))
            if remaining == 0:
                skipped.append("GitHub Search(Rate Limit exhausted)")
                break
            if r.status_code != 200:
                skipped.append(f"GitHub Search({r.status_code})")
                continue

            for repo in r.json().get("items", []):
                full_name = repo.get("full_name", "")
                if full_name in seen_repos:
                    continue
                seen_repos.add(full_name)

                desc  = repo.get("description") or ""
                stars = repo.get("stargazers_count", 0)
                url   = repo.get("html_url", "")
                if has_exclude(full_name + desc, excl):
                    continue
                items.append({
                    "title":  f"{full_name} · {stars}⭐",
                    "url":    url,
                    "body":   desc,
                    "pub":    repo.get("pushed_at", ""),
                    "source": "github_search",
                    "stars":  stars,
                })
        except Exception as e:
            skipped.append(f"GitHub Search({type(e).__name__})")
    return items


# ── D: HN Algolia ──────────────────────────────────────────────────
def fetch_hn(keywords: list[str], min_pts: int, hours: int,
             excl: list, skipped: list) -> list[dict]:
    since = int((NOW - timedelta(hours=hours)).timestamp())
    items: list[dict] = []
    seen_ids: set[str] = set()

    for kw in keywords:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "query": kw,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since},points>{min_pts}",
                    "hitsPerPage": 5,
                },
                timeout=15,
            )
            if r.status_code != 200:
                continue
            for hit in r.json().get("hits", []):
                oid = hit.get("objectID")
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)

                title  = hit.get("title", "")
                url    = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
                points = hit.get("points", 0)
                if has_exclude(title, excl):
                    continue
                items.append({
                    "title":  title,
                    "url":    url,
                    "body":   f"HN {points}pts",
                    "pub":    hit.get("created_at", ""),
                    "source": "hn",
                    "points": points,
                })
        except Exception:
            pass
    return items


# Navigation / boilerplate phrases to reject from web scrapes
_NAV_REJECT = re.compile(
    r"^(skip to|cookie|privacy|terms|sign in|log in|subscribe|newsletter|"
    r"read more|learn more|view all|see all|back to|go to|download|"
    r"try claude|claude api|press kit|press inquir|non-media|media assets|"
    r"beian|icp|\d{5,}|©)",
    re.IGNORECASE
)


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_CAT   = r"(?:Product|Research|Announcements?|Policy|News|企业服务|行业)"


def _clean_web_title(raw: str) -> str:
    """Remove category/date prefixes from Jina-extracted article titles."""
    t = re.sub(r"^#+\s*", "", raw.strip())
    # Strip date+category prefix — Anthropic articles appear as:
    #   "May 14, 2026 Announcements Actual Title"
    #   "Product Apr 16, 2026 Actual Title"
    t = re.sub(
        rf"^(?:{_CAT}\s+)?{_MONTH}\s+\d{{1,2}},?\s*\d{{4}}\s*(?:{_CAT}\s+)?",
        "", t, flags=re.IGNORECASE
    )
    # Strip leading "#### " heading artifacts
    t = re.sub(r"^#+\s*", "", t)
    # Trim trailing repetition ("Today, we’re launching X" after the actual title)
    t = re.split(r"\bToday,?\s+we[‘’]?re\b|\bToday,?\s+we\b", t)[0]
    return t.strip()


def _is_nav_link(title: str, url: str) -> bool:
    """Return True if this link looks like navigation/boilerplate."""
    if len(title) < 12:
        return True
    if _NAV_REJECT.search(title.strip()):
        return True
    # ICP registration numbers (Chinese government filing links)
    if re.search(r"[京沪粤]?ICP备\d+号", title) or "beian.miit.gov.cn" in url:
        return True
    # Pure URL paths that ended up as titles
    if title.startswith("http") or title.startswith("/"):
        return True
    return False


# ── E: Web scraping ────────────────────────────────────────────────
def fetch_web_source(src: dict, hours: int, excl: list, skipped: list) -> list[dict]:
    url       = src.get("url", "")
    use_jina  = src.get("use_jina", False)
    must      = src.get("must_match_any", [])
    items: list[dict] = []

    try:
        content = web_fetch(url, use_jina)
        if not content:
            skipped.append(f"Web/{url}(empty response)")
            return items

        # Anthropic news: always use Jina to avoid product-nav pollution in HTML
        if "anthropic.com/news" in url and not use_jina:
            content = jina_fetch(url) or content

        # Detect if content is markdown (Jina output) or raw HTML
        is_markdown = use_jina or content.lstrip().startswith("Title:")

        # URL allow-pattern: for article sites only accept links from the same
        # path prefix so we don't pick up header/footer/nav links to other domains.
        url_filter: str | None = None
        if "anthropic.com/news" in url:
            url_filter = "anthropic.com/news/"

        if is_markdown:
            if url_filter:
                raw_links = re.findall(
                    r'\[([^\]]{10,200})\]\((https?://[^\)]{15,300})\)', content
                )
                links = [
                    (_clean_web_title(t), h)
                    for t, h in raw_links
                    if url_filter in h
                ]
            else:
                raw_links = re.findall(
                    r'\[([^\]]{10,200})\]\((https?://[^\)]{15,300})\)', content
                )
                links = [(_clean_web_title(t), h) for t, h in raw_links]
        else:
            raw = re.findall(
                r'<a[^>]+href="(https?://[^"]{15,300})"[^>]*>\s*([^<]{10,200})\s*</a>',
                content
            )
            links_raw = [(_clean_web_title(t.strip()), h) for h, t in raw]
            if url_filter:
                links = [(t, h) for t, h in links_raw if url_filter in h]
            else:
                links = links_raw

        seen_in_src: set[str] = set()
        for title, link_url in links[:50]:
            if not title or link_url in seen_in_src:
                continue
            seen_in_src.add(link_url)
            if _is_nav_link(title, link_url):
                continue
            # must_match: check title + surrounding content snippet
            ctx = title.lower() + " " + content[:5000].lower()
            if must and not has_any(ctx, must):
                continue
            if has_exclude(title, excl):
                continue
            items.append({
                "title":  title,
                "url":    link_url,
                "body":   "",
                "source": "web",
            })

        if not items:
            skipped.append(f"Web/{url}(no keyword match)")
    except Exception as e:
        skipped.append(f"Web/{url}({type(e).__name__})")
    return items


# ── F: DEV Community ───────────────────────────────────────────────
def fetch_dev(tags: list[str], hours: int, excl: list, skipped: list) -> list[dict]:
    items: list[dict] = []
    seen_ids: set[int] = set()

    for tag in tags:
        try:
            r = requests.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "per_page": 10, "top": 1},
                headers=WEB_UA, timeout=15,
            )
            if r.status_code != 200:
                skipped.append(f"DEV/{tag}({r.status_code})")
                continue
            for art in r.json():
                aid = art.get("id")
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                title = art.get("title", "")
                pub   = art.get("published_at", "")
                if not within_window(pub, hours):
                    continue
                if has_exclude(title, excl):
                    continue
                url = art.get("url") or art.get("canonical_url", "")
                items.append({
                    "title":  title,
                    "url":    url,
                    "body":   art.get("description", ""),
                    "pub":    pub,
                    "source": "dev_community",
                })
        except Exception as e:
            skipped.append(f"DEV/{tag}({type(e).__name__})")
    return items


# ═══════════════════════════════════════════════════════════════════
# STEP 3 — Filter & deduplicate
# ═══════════════════════════════════════════════════════════════════

def _norm_url(url: str) -> str:
    return url.rstrip("/").lower().split("?")[0].split("#")[0]


def _jaccard(a: str, b: str) -> float:
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def process_topic(raw: list[dict], seen: dict, hours: int,
                  excl: list, max_items: int) -> tuple[list[dict], list[dict]]:
    """
    Returns (new_items, hot_items).
    Updates `seen` in-place.
    """
    hot: list[dict] = []
    new: list[dict] = []
    run_norm: dict[str, int] = {}   # norm_url → index in `new`

    for item in raw:
        url = item.get("url", "")
        if not url:
            continue

        norm = _norm_url(url)

        # ── cross-day dedup via seen.json ──
        matched_key = url if url in seen else (norm if norm in seen else None)
        if matched_key:
            rec = seen[matched_key]
            if rec.get("last_seen") != TODAY:
                rec["last_seen"] = TODAY
                rec["count"] = rec.get("count", 1) + 1
            if rec["count"] >= 3:
                hot.append({**item, "count": rec["count"]})
            continue

        # New URL → register
        seen[url] = {
            "title":      item.get("title", ""),
            "url":        url,
            "first_seen": TODAY,
            "last_seen":  TODAY,
            "count":      1,
        }

        # ── within-run dedup by norm URL ──
        if norm in run_norm:
            idx = run_norm[norm]
            existing = new[idx]
            if (item.get("points", 0) > existing.get("points", 0) or
                    item.get("stars", 0) > existing.get("stars", 0) or
                    item.get("stars_today", 0) > existing.get("stars_today", 0)):
                new[idx] = item
            continue

        # ── within-run dedup by title similarity ──
        similar = False
        for i, ex in enumerate(new):
            if _jaccard(item.get("title", ""), ex.get("title", "")) > 0.80:
                if (item.get("points", 0) > ex.get("points", 0) or
                        item.get("stars", 0) > ex.get("stars", 0)):
                    new[i] = item
                    run_norm[norm] = i
                similar = True
                break
        if not similar:
            run_norm[norm] = len(new)
            new.append(item)

    return new[:max_items], hot


# ═══════════════════════════════════════════════════════════════════
# STEP 4 — Relevance "why" text
# ═══════════════════════════════════════════════════════════════════

def _kw_in(text: str, kws: list[str]) -> bool:
    tl = text.lower()
    return any(k.lower() in tl for k in kws)


def generate_why(item: dict, topic_id: str) -> str:
    title  = item.get("title", "")
    body   = item.get("body", "")
    source = item.get("source", "")
    url    = item.get("url", "")
    pts    = item.get("points", 0)
    stars  = item.get("stars", 0)
    st_day = item.get("stars_today", 0)
    combo  = (title + " " + body).lower()

    if topic_id == "claude_ecosystem":
        if "claude-code" in url.lower() or _kw_in(combo, ["claude code", "claude-code"]):
            # Extract first bullet from release body for specificity
            bullets = re.findall(r"[-•]\s+(.+)", body)
            first = bullets[0][:40].rstrip(".,") if bullets else ""
            feats = []
            if _kw_in(combo, ["plugin"]):              feats.append("插件系统")
            if _kw_in(combo, ["mcp"]):                 feats.append("MCP集成")
            if _kw_in(combo, ["agent", "--agents"]):   feats.append("agents命令")
            if _kw_in(combo, ["cache", "缓存"]):        feats.append("缓存优化")
            if _kw_in(combo, ["hook", "slash"]):        feats.append("hooks扩展")
            if _kw_in(combo, ["fix", "bug", "修复"]):   feats.append("稳定性修复")
            feat_str = "、".join(feats[:2]) if feats else "多项更新"
            note = f"（{first}…）" if first else ""
            return f"Claude Code 新版本，{feat_str}{note}直接影响日常工作流"
        if _kw_in(combo, ["sdk", "typescript", "python", "anthropic-sdk"]):
            return "SDK 版本变更影响 Claude API 调用兼容性，前端项目需关注迁移风险"
        if _kw_in(combo, ["opus", "sonnet", "haiku", "claude 4", "claude opus"]):
            return "新模型发布，关注能力边界变化对 AI 编程工具选型的影响"
        if _kw_in(combo, ["anthropic", "claude", "blog", "news"]):
            return "Anthropic 官方动态，关注模型能力演进与 API 策略变化方向"
        return "Claude 生态官方更新，直接影响 AI 工具链选型决策"

    if topic_id == "ai_tools_discovery":
        if source == "hn":
            return f"HN {pts}pts 社区热议，验证过的 AI 工具实践，面试技术广度参考"
        if source == "github_trending" and st_day:
            return f"GitHub 今日 {st_day}⭐ 快速增长，AI 编码工具新趋势信号"
        if _kw_in(combo, ["mcp", "model context protocol"]):
            return "MCP 协议新工具，扩展 Claude 工具链能力边界，架构面试考点"
        if _kw_in(combo, ["agent", "agentic", "multi-agent"]):
            return "Agent 框架快速增长，与 Claude Code 工作流高度契合"
        if _kw_in(combo, ["spec", "sdd"]):
            return "Spec 驱动工具，与 Claude Code 规格化开发方向一致"
        return f"新 AI 编码工具，{stars or st_day}⭐ 社区认可，值得纳入工具链评估"

    if topic_id == "china_ai_trends":
        domains = [
            kw for kw in ["阿里云", "腾讯云", "字节", "百度", "华为",
                          "CodeBuddy", "通义", "文心", "豆包", "混元"]
            if kw in title + body
        ]
        domain_str = "、".join(domains[:2]) if domains else "国内大厂"
        return f"{domain_str} AI 产品动态，掌握国内 AI coding 工具竞争格局"

    if topic_id == "mcp_ecosystem":
        if source == "hn":
            return f"HN {pts}pts 热议 MCP，掌握协议落地方向，是面试 AI 架构考点"
        if _kw_in(combo, ["server", "服务器"]):
            return "MCP Server 实现案例，了解协议扩展边界和工具集成模式"
        return "MCP 生态扩张信号，Model Context Protocol 是当前 AI 工程化核心协议"

    if topic_id == "spec_driven_dev":
        if source == "hn":
            return f"HN {pts}pts，Spec 驱动开发社区认可度，面试可讲方法论优势"
        return "Spec 驱动开发工具/方法论，结合 Claude Code 是前沿工程实践方向"

    return "AI 工具链前沿动态，关注技术趋势演进方向"


# ═══════════════════════════════════════════════════════════════════
# STEP 5 — Build card & push
# ═══════════════════════════════════════════════════════════════════

def build_card(results: dict, topic_labels: dict,
               hot_items: list, skipped: list) -> dict:
    elements: list[dict] = []

    for topic_id, items in results.items():
        if not items:
            continue
        label = topic_labels.get(topic_id, topic_id)
        lines = [f"**{label}**"]
        for item in items:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']})")
            lines.append(f"  _{item['why']}_")
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

    skip_msg = "、".join(dict.fromkeys(skipped)) if skipped else "无"
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
                "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {TODAY}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def git_push_report():
    """
    Commit the report files to the current dev branch, then force-push the HEAD
    to report/YYYY-MM-DD so GitHub Actions triggers the Feishu notification.
    This avoids branch-checkout conflicts and keeps seen.json on the dev branch
    so it persists across daily runs.
    """
    report_branch = f"report/{TODAY}"
    for cmd in [
        ["git", "config", "user.email", "routine-bot@ai-news"],
        ["git", "config", "user.name",  "AI News Routine"],
    ]:
        subprocess.run(cmd, check=True, cwd=REPO_DIR)

    subprocess.run(["git", "add", REPORT_JSON, SEEN_JSON], check=True, cwd=REPO_DIR)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", f"chore: daily report {TODAY}"],
        check=True, cwd=REPO_DIR,
    )

    # Push current HEAD to both: the report branch (triggers GitHub Actions)
    # and the dev branch (keeps seen.json for next run).
    targets = [
        f"HEAD:refs/heads/{report_branch}",   # report branch → triggers notify-feishu.yml
        "HEAD",                                # dev branch → persists seen.json
    ]
    for target in targets:
        for attempt, wait in enumerate([0, 2, 4, 8, 16], start=1):
            if wait:
                import time; time.sleep(wait)
            push = subprocess.run(
                ["git", "push", "--force", "-u", "origin", target],
                cwd=REPO_DIR, capture_output=True, text=True,
            )
            if push.returncode == 0:
                label = report_branch if "report" in target else "dev branch"
                print(f"✓ Pushed to origin/{label}")
                break
            print(f"  attempt {attempt} failed ({target}): {push.stderr.strip()[:120]}",
                  file=sys.stderr)
        else:
            print(f"All push attempts failed for {target} — report content:", file=sys.stderr)
            with open(REPORT_JSON) as f:
                print(f.read(), file=sys.stderr)
            sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    # ── Step 1 ──────────────────────────────────────────────────────
    cfg     = load_config()
    topics  = cfg.get("topics", [])
    filters = cfg.get("filters", {})
    out_cfg = cfg.get("output", {})

    hours      = filters.get("lookback_hours", 24)
    max_items  = filters.get("max_items_per_topic", 5)
    excl       = filters.get("exclude_keywords", [])
    ttl        = filters.get("seen_ttl_days", 7)
    skip_empty = out_cfg.get("skip_if_empty", True)

    seen = clean_seen(load_seen(), ttl)

    skipped: list[str] = []
    results:  dict[str, list] = {}
    labels:   dict[str, str]  = {}
    all_hot:  list[dict]       = []

    # ── Steps 2 & 3: per-topic fetch ────────────────────────────────
    priority_order = {"high": 0, "medium": 1, "low": 2}
    sorted_topics  = sorted(
        topics, key=lambda t: priority_order.get(t.get("priority", "low"), 2)
    )

    for topic in sorted_topics:
        tid   = topic.get("id")
        tname = topic.get("name", tid)
        srcs  = topic.get("sources", {})
        labels[tid] = tname

        raw: list[dict] = []

        for repo in srcs.get("github_releases", []):
            raw.extend(fetch_releases(repo, hours, excl, skipped))

        if gt := srcs.get("github_trending"):
            raw.extend(fetch_trending(gt, excl, skipped))

        if gs := srcs.get("github_search"):
            raw.extend(fetch_search(gs.get("queries", []), excl, skipped))

        if hn := srcs.get("hn"):
            raw.extend(fetch_hn(
                hn.get("keywords", []), hn.get("min_points", 30),
                hours, excl, skipped
            ))

        for ws in srcs.get("web", []):
            raw.extend(fetch_web_source(ws, hours, excl, skipped))

        if dc := srcs.get("dev_community"):
            raw.extend(fetch_dev(dc.get("tags", []) if isinstance(dc, dict)
                                 else list(dc), hours, excl, skipped))

        new, hot = process_topic(raw, seen, hours, excl, max_items)
        all_hot.extend(hot)

        for item in new:
            item["why"] = generate_why(item, tid)

        results[tid] = new
        print(f"[{tid}] {len(new)} new items, {len(hot)} hot")

    # ── Check empty ──────────────────────────────────────────────────
    total = sum(len(v) for v in results.values())
    if total == 0 and skip_empty:
        print("No new content — skip_if_empty=true. Writing seen.json and exiting.")
        os.makedirs(BOT_DIR, exist_ok=True)
        with open(SEEN_JSON, "w", encoding="utf-8") as f:
            json.dump({"urls": seen}, f, ensure_ascii=False, indent=2)
        return

    # ── Step 4: Build card ───────────────────────────────────────────
    card = build_card(results, labels, all_hot, skipped)

    # ── Step 5: Write & push ─────────────────────────────────────────
    os.makedirs(BOT_DIR, exist_ok=True)

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    # Validate
    with open(REPORT_JSON, "r", encoding="utf-8") as f:
        json.load(f)
    print(f"✓ Report valid: {REPORT_JSON}")

    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump({"urls": seen}, f, ensure_ascii=False, indent=2)
    print(f"✓ seen.json written ({len(seen)} entries)")

    print(f"\nSummary: {total} items across {len([v for v in results.values() if v])} topics")
    if skipped:
        print(f"Skipped sources: {', '.join(dict.fromkeys(skipped))}")

    git_push_report()


if __name__ == "__main__":
    main()
