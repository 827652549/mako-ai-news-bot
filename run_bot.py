#!/usr/bin/env python3
"""AI News Bot - full pipeline runner for 2026-05-18."""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

# ── constants ──────────────────────────────────────────────────────
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
LOOKBACK_HOURS = 24
WINDOW_START = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
SEEN_TTL_DAYS = 7
MAX_ITEMS = 5
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(BASE_DIR, ".ai-news-bot", "seen.json")
REPORT_PATH = os.path.join(BASE_DIR, ".ai-news-bot", "latest-report.json")

skipped_sources = []
seen_urls = {}

# ── helpers ────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def truncate_title(title, max_len=30):
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [' ', '·', '|', '：', '，', '-']:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + '…'
    return cut + '…'


def http_get(url, headers=None, timeout=15):
    """Simple GET, returns (status_code, body_str). Never raises."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log(f"  HTTP error for {url}: {e}")
        return 0, ""


def gh_headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "mako-ai-news-bot/1.0"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def parse_iso(s):
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        if "." in s:
            s = s[:s.index(".")]
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def in_window(dt):
    if dt is None:
        return False
    return dt >= WINDOW_START


def exclude_check(text):
    bad = ["sponsored", "advertisement", "广告", "招聘", "裁员", "财报", "股价"]
    t = text.lower()
    return any(b in t for b in bad)


def jina_get(url):
    return http_get(f"https://r.jina.ai/{url}",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"})


# ── Step 1: load seen.json ─────────────────────────────────────────

def load_seen():
    global seen_urls
    if not os.path.exists(SEEN_PATH):
        log("seen.json not found, starting fresh")
        seen_urls = {}
        return
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    seen_urls = data.get("urls", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
    expired = [u for u, v in seen_urls.items() if v.get("last_seen", "9999") < cutoff]
    for u in expired:
        del seen_urls[u]
    log(f"Loaded seen.json: {len(seen_urls)} entries, removed {len(expired)} expired")


def is_seen(url):
    return url in seen_urls


def mark_seen(url, title):
    if url in seen_urls:
        entry = seen_urls[url]
        # Only increment count when seen on a new day (cross-day dedup)
        # Within the same run, multiple topics may encounter the same URL — don't double-count
        if entry.get("last_seen", "") < TODAY:
            entry["last_seen"] = TODAY
            entry["count"] = entry.get("count", 1) + 1
        return True   # already seen → skip from topic output
    seen_urls[url] = {
        "title": title[:80],
        "url": url,
        "first_seen": TODAY,
        "last_seen": TODAY,
        "count": 1
    }
    return False      # new item → include in topic output


def get_hot_items():
    # Only count items that were seen on previous days (not first-seen today)
    return [v for v in seen_urls.values()
            if v.get("count", 0) >= 3 and v.get("first_seen", TODAY) < TODAY]


# ── Step 2A: github_releases ───────────────────────────────────────

RELEASE_REPOS = [
    "anthropics/claude-code",
    "anthropics/anthropic-sdk-typescript",
    "anthropics/anthropic-sdk-python",
]


def fetch_github_releases():
    items = []
    seen_repos = {}  # repo -> best item (latest published_at)
    for repo in RELEASE_REPOS:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
        status, body = http_get(url, headers=gh_headers())
        if status != 200:
            log(f"  Releases {repo}: HTTP {status}, skipping")
            skipped_sources.append(f"GitHub Releases/{repo}({status})")
            continue
        try:
            releases = json.loads(body)
        except Exception:
            continue
        for r in releases:
            pub = parse_iso(r.get("published_at"))
            if not in_window(pub):
                continue
            tag = r.get("tag_name", "")
            name = r.get("name") or tag
            html_url = r.get("html_url", "")
            body_text = (r.get("body") or "")[:300]
            item = {
                "title": f"{repo.split('/')[-1]} {tag} · {name[:40]}",
                "url": html_url,
                "published_at": pub,
                "summary": body_text,
                "source": "github_releases",
                "repo": repo,
            }
            # same-day dedup: keep latest per repo
            prev = seen_repos.get(repo)
            if prev is None or pub > prev["published_at"]:
                seen_repos[repo] = item

    for repo, item in seen_repos.items():
        items.append(item)
        log(f"  Releases: {item['title'][:60]}")
    return items


# ── Step 2B: github_trending ──────────────────────────────────────

MUST_MATCH = [
    "claude", "mcp", "ai coding", "agent", "spec-driven",
    "openspec", "cc-switch", "claude code",
]


def fetch_github_trending():
    items = []
    seen_names = set()
    for lang_param in ["", "&spoken_language_code=zh"]:
        url = f"https://github.com/trending?since=daily{lang_param}"
        status, body = http_get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        if status != 200 or len(body) < 500:
            status2, body = jina_get(url)
        if not body:
            skipped_sources.append(f"GitHub Trending({status})")
            continue

        import re
        # Look for repo articles in HTML
        # Pattern: href="/owner/repo" in article tags
        repo_blocks = re.findall(
            r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
            body, re.DOTALL
        )
        if not repo_blocks:
            # Try Jina-style plain text
            for line in body.split("\n"):
                line = line.strip()
                if line.startswith("##") or "/" in line:
                    pass  # simplified parse below

        for block in repo_blocks:
            # repo name
            m = re.search(r'href="/([^/"]+/[^/"]+)"', block)
            if not m:
                continue
            repo_path = m.group(1)
            if repo_path in seen_names:
                continue
            # description
            desc_m = re.search(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', block, re.DOTALL)
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            # today's stars
            star_m = re.search(r'([\d,]+)\s*stars today', block)
            stars_today = int(star_m.group(1).replace(",", "")) if star_m else 0

            combined = (repo_path + " " + desc).lower()
            if not any(kw.lower() in combined for kw in MUST_MATCH):
                continue
            if exclude_check(desc):
                continue

            seen_names.add(repo_path)
            html_url = f"https://github.com/{repo_path}"
            items.append({
                "title": f"{repo_path} · {desc[:50]}" if desc else repo_path,
                "url": html_url,
                "published_at": datetime.now(timezone.utc),
                "stars_today": stars_today,
                "source": "github_trending",
            })
            log(f"  Trending: {repo_path} (+{stars_today}⭐)")

    return items


# ── Step 2C: github_search ────────────────────────────────────────

SEARCH_QUERIES_TOOLS = [
    "claude code stars:>50 created:>2026-05-04",
    "claude agent stars:>30 created:>2026-05-04",
]
SEARCH_QUERIES_MCP = [
    "MCP server stars:>100 pushed:>2026-05-11",
    "model-context-protocol stars:>50 created:>2026-05-04",
]
SEARCH_QUERIES_SPEC = [
    "spec-driven stars:>200 pushed:>2026-04-18",
    "openspec OR spec-kit stars:>100 pushed:>2026-05-04",
]


def _run_search_queries(queries):
    """Execute a list of GitHub search queries, return deduplicated items."""
    if not GITHUB_TOKEN:
        return []
    items = []
    seen_repos = set()
    for q in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&order=desc&per_page=10"
        status, body = http_get(url, headers=gh_headers())
        if status == 403 or status == 429:
            skipped_sources.append(f"GitHub Search (Rate Limit)")
            break
        if status != 200:
            log(f"  Search '{q}': HTTP {status}")
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        for repo in data.get("items", []):
            full_name = repo.get("full_name", "")
            if full_name in seen_repos:
                continue
            updated = parse_iso(repo.get("updated_at")) or parse_iso(repo.get("pushed_at"))
            created = parse_iso(repo.get("created_at"))
            if not (in_window(updated) or in_window(created)):
                continue
            desc = repo.get("description") or ""
            if exclude_check(desc):
                continue
            stars = repo.get("stargazers_count", 0)
            html_url = repo.get("html_url", "")
            seen_repos.add(full_name)
            items.append({
                "title": f"{full_name} · {desc[:50]}" if desc else full_name,
                "url": html_url,
                "published_at": updated or created or datetime.now(timezone.utc),
                "stars": stars,
                "source": "github_search",
            })
            log(f"  Search: {full_name} ⭐{stars}")
        time.sleep(0.5)
    return items


def fetch_github_search():
    if not GITHUB_TOKEN:
        skipped_sources.append("GitHub Search (no token)")
        return [], [], []
    tools = _run_search_queries(SEARCH_QUERIES_TOOLS)
    mcp = _run_search_queries(SEARCH_QUERIES_MCP)
    spec = _run_search_queries(SEARCH_QUERIES_SPEC)
    return tools, mcp, spec


# ── Step 2D: HN Algolia ───────────────────────────────────────────

HN_KEYWORDS = {
    "ai_tools_discovery": [
        "claude code", "spec kit", "openspec", "MCP server",
        "AI coding", "spec driven", "cc-switch",
    ],
    "mcp_ecosystem": [
        "MCP server", "model context protocol", "MCP tool",
    ],
    "spec_driven_dev": [
        "spec driven", "spec kit", "openspec", "SDD", "spec-driven development",
    ],
}
HN_MIN_POINTS = {"ai_tools_discovery": 30, "mcp_ecosystem": 20, "spec_driven_dev": 20}


def fetch_hn(topic_id):
    since_ts = int(WINDOW_START.timestamp())
    keywords = HN_KEYWORDS.get(topic_id, [])
    min_pts = HN_MIN_POINTS.get(topic_id, 20)
    seen_ids = set()
    items = []
    for kw in keywords:
        encoded = urllib.parse.quote(kw)
        # numericFilters uses comma-separated conditions; encode '>' as %3E
        num_filters = urllib.parse.quote(f"created_at_i>{since_ts},points>{min_pts}")
        url = (
            f"https://hn.algolia.com/api/v1/search?query={encoded}"
            f"&tags=story&numericFilters={num_filters}"
            f"&hitsPerPage=5"
        )
        status, body = http_get(url)
        if status != 200:
            log(f"  HN '{kw}': HTTP {status}")
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        for hit in data.get("hits", []):
            oid = hit.get("objectID")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            title = hit.get("title") or hit.get("story_title") or ""
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            pts = hit.get("points", 0)
            created = parse_iso(hit.get("created_at"))
            if not in_window(created):
                continue
            if exclude_check(title):
                continue
            items.append({
                "title": title,
                "url": story_url,
                "published_at": created,
                "points": pts,
                "source": "hn",
            })
            log(f"  HN: {title[:60]} ({pts}pts)")
        time.sleep(0.3)
    return items


# ── Step 2E: Web scraping ─────────────────────────────────────────

def fetch_web_anthropic():
    """Fetch Anthropic blog / news via Jina Reader for reliable parsing."""
    items = []
    status, body = jina_get("https://www.anthropic.com/news")
    if status != 200 or len(body) < 300:
        skipped_sources.append(f"Anthropic News({status})")
        return items

    import re
    seen_local = set()
    # Jina renders markdown-style: [Title](url)
    # Match only /news/<slug> links (not partner pages, economic-futures, etc.)
    for m in re.finditer(r'\[([^\]]{10,100})\]\((https?://www\.anthropic\.com/news/[a-z0-9-]+)\)', body):
        title, link = m.group(1).strip(), m.group(2)
        if link in seen_local:
            continue
        # skip navigation boilerplate, date-header artifacts, and image alts
        noise_patterns = ["Skip", "Image", "####", " 20", "Jan ", "Feb ", "Mar ",
                          "Apr ", "May ", "Jun ", "Jul ", "Aug ", "Sep ", "Oct ",
                          "Nov ", "Dec ", "Announcements", "Product ", "Research "]
        if any(title.startswith(p) or title == p for p in noise_patterns):
            continue
        # titles that are mostly a date (common Jina artifact for date-headers)
        if re.match(r'^[A-Z][a-z]+ \d{1,2}, 20\d\d', title):
            continue
        if exclude_check(title):
            continue
        seen_local.add(link)
        items.append({
            "title": title,
            "url": link,
            "published_at": datetime.now(timezone.utc),
            "source": "web_anthropic",
        })
        if len(items) >= 5:
            break
    log(f"  Anthropic news: {len(items)} items")
    return items


def fetch_web_leiphone():
    """Fetch Leiphone via Jina Reader."""
    must_match = [
        "阿里云", "腾讯云", "字节跳动", "百度", "华为云", "腾讯", "阿里", "字节",
        "国内", "中国", "CodeBuddy", "通义", "文心", "混元", "豆包",
    ]
    url = "https://www.leiphone.com/"
    status, body = jina_get(url)
    if status != 200 or len(body) < 300:
        skipped_sources.append(f"Leiphone({status})")
        return []

    import re
    items = []
    # Jina returns markdown-like text; look for links
    link_pattern = re.compile(r'\[([^\]]{5,})\]\((https?://[^\)]+)\)')
    seen_urls_local = set()
    for m in link_pattern.finditer(body):
        title, link = m.group(1).strip(), m.group(2)
        if link in seen_urls_local:
            continue
        combined = title
        if not any(kw in combined for kw in must_match):
            continue
        if exclude_check(title):
            continue
        seen_urls_local.add(link)
        items.append({
            "title": title,
            "url": link,
            "published_at": datetime.now(timezone.utc),
            "source": "web_leiphone",
        })
        log(f"  Leiphone: {title[:50]}")
        if len(items) >= 6:
            break
    if not items:
        skipped_sources.append("Leiphone(24h内无匹配)")
    return items


def fetch_web_smarthey():
    must_match = [
        "阿里云", "腾讯云", "字节跳动", "百度", "华为", "国内", "中国",
        "大模型", "CodeBuddy", "通义", "文心", "混元", "豆包",
    ]
    url = "https://www.smarthey.com/"
    status, body = http_get(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })
    if status != 200 or len(body) < 500:
        status, body = jina_get(url)
    if not body:
        skipped_sources.append("Smarthey(fetch failed)")
        return []

    import re
    items = []
    seen_local = set()
    for m in re.finditer(r'href="(https?://www\.smarthey\.com/[^"]+)"[^>]*>([^<]{5,})<', body):
        link, title = m.group(1), m.group(2).strip()
        title = re.sub(r'\s+', ' ', title)
        if link in seen_local or len(title) < 5:
            continue
        if not any(kw in title for kw in must_match):
            continue
        if exclude_check(title):
            continue
        seen_local.add(link)
        items.append({
            "title": title,
            "url": link,
            "published_at": datetime.now(timezone.utc),
            "source": "web_smarthey",
        })
        log(f"  Smarthey: {title[:50]}")
        if len(items) >= 6:
            break
    if not items:
        skipped_sources.append("Smarthey(无匹配内容)")
    return items


# ── Step 2F: DEV Community ────────────────────────────────────────

DEV_TAGS = {
    "ai_tools_discovery": ["claudecode", "claude", "ai-tools", "mcp"],
    "mcp_ecosystem": ["mcp", "modelcontextprotocol"],
}


def fetch_dev_community(topic_id):
    tags = DEV_TAGS.get(topic_id, [])
    items = []
    seen_local = set()
    for tag in tags:
        url = f"https://dev.to/api/articles?tag={tag}&per_page=10&top=1"
        status, body = http_get(url)
        if status != 200:
            log(f"  DEV Community tag={tag}: HTTP {status}")
            if status in (403, 429):
                skipped_sources.append(f"DEV Community({status})")
                break
            continue
        try:
            articles = json.loads(body)
        except Exception:
            continue
        for art in articles:
            pub = parse_iso(art.get("published_at"))
            if not in_window(pub):
                continue
            link = art.get("url") or art.get("canonical_url") or ""
            if link in seen_local:
                continue
            title = art.get("title") or ""
            if exclude_check(title):
                continue
            seen_local.add(link)
            items.append({
                "title": title,
                "url": link,
                "published_at": pub,
                "source": "dev_community",
            })
            log(f"  DEV: {title[:60]}")
        time.sleep(0.2)
    return items


# ── Step 3+4: assemble per-topic results ──────────────────────────

def dedup_within_run(items):
    """URL-dedup and fuzzy title dedup within a single list."""
    seen_u = {}
    out = []
    for item in items:
        u = item["url"]
        if u in seen_u:
            # keep higher stars/points
            prev = seen_u[u]
            if item.get("stars", 0) + item.get("points", 0) > prev.get("stars", 0) + prev.get("points", 0):
                out = [x for x in out if x["url"] != u]
                seen_u[u] = item
                out.append(item)
            continue
        seen_u[u] = item
        out.append(item)
    return out


WHY_MAP = {
    # Will be filled by AI-style reasoning below per item
}


def build_why(item, topic_id):
    """Generate a concise '为什么关注' string based on item metadata."""
    title = item.get("title", "")
    url = item.get("url", "")
    source = item.get("source", "")
    pts = item.get("points", 0)
    stars = item.get("stars", item.get("stars_today", 0))

    t = title.lower()

    # Claude Code releases
    if "claude-code" in url or "claude code" in t:
        if "claude-code" in url and "releases" in url:
            summary = item.get("summary", "")
            # Extract key feature from release notes
            first_line = summary.split("\n")[0][:100] if summary else ""
            return f"Claude Code 新版本，{first_line or '含工具链改进'}，直接影响日常 AI 编程工作流"
        return f"Claude Code 生态更新，{title[20:60] or '工具链演进'}，影响前端 AI 辅助编程效率"

    if "anthropic-sdk-typescript" in url or "anthropic-sdk-python" in url:
        return "Anthropic SDK 版本更新，影响前端/Node 项目中 Claude API 调用方式，需关注破坏性变更"

    if "larksuite" in url or "lark" in url.lower():
        return "飞书 CLI/SDK 更新，与 Lark 机器人集成场景直接相关"

    if "anthropic.com/news" in url or "anthropic.com/blog" in url:
        return "Anthropic 官方公告，可能涉及模型能力、API 变更或产品策略，影响 Claude Code 工具链规划"

    if source == "hn":
        return f"HN 社区热议 {pts}pts，工程师圈验证度高，可作为面试聊 AI 工程化趋势的佐证"

    if "mcp" in t or "model context protocol" in t or "mcp_ecosystem" == topic_id:
        return f"MCP 协议相关{'工具' if 'server' in t or 'tool' in t else '内容'}，直接影响 Claude Code 工具扩展与 agent 工作流设计"

    if "spec" in t or "spec_driven" == topic_id:
        return "Spec 驱动开发实践，是 AI 编程向可验证、可审查方向演进的核心方法论，面试中高频考点"

    if source in ("web_leiphone", "web_smarthey"):
        for kw in ["阿里云", "腾讯云", "华为云", "字节", "百度", "CodeBuddy", "通义", "文心", "豆包", "混元"]:
            if kw in title:
                return f"国内 AI 产品动态：{kw} 相关，与国内云厂商 AI coding 工具竞争格局直接相关"
        return "国内 AI 生态动态，跟踪大厂产品演进与 Claude 的竞争差异"

    if source in ("github_trending", "github_search"):
        return f"{'快速增长' if stars > 100 else '新兴'} GitHub 项目（⭐{stars}），在 AI 工具链方向有参考价值"

    if source == "dev_community":
        return "DEV Community 实战文章，展示 AI 工具在真实工程项目中的落地方式"

    return "与 AI 工程化工具链相关，值得关注"


def process_topic(topic_id, raw_items):
    """Filter, dedup against seen.json, truncate to MAX_ITEMS, add 'why'."""
    items = dedup_within_run(raw_items)
    result = []
    for item in items:
        url = item["url"]
        title = item.get("title", "")
        if exclude_check(title):
            continue
        already_seen = mark_seen(url, title)
        if already_seen:
            continue  # will appear in hot_items if count >= 3
        item["why"] = build_why(item, topic_id)
        result.append(item)

    # sort by points/stars desc
    result.sort(key=lambda x: -(x.get("points", 0) + x.get("stars", 0) + x.get("stars_today", 0)))
    return result[:MAX_ITEMS]


# ── main ──────────────────────────────────────────────────────────

def main():
    log(f"=== AI News Bot starting, date={TODAY} ===")

    # Step 1
    load_seen()

    # Step 2: fetch all sources
    log("--- Fetching GitHub Releases ---")
    releases = fetch_github_releases()

    log("--- Fetching GitHub Trending ---")
    trending = fetch_github_trending()

    log("--- Fetching GitHub Search ---")
    gh_tools, gh_mcp, gh_spec = fetch_github_search()

    log("--- Fetching HN (ai_tools_discovery) ---")
    hn_tools = fetch_hn("ai_tools_discovery")

    log("--- Fetching HN (mcp_ecosystem) ---")
    hn_mcp = fetch_hn("mcp_ecosystem")

    log("--- Fetching HN (spec_driven_dev) ---")
    hn_spec = fetch_hn("spec_driven_dev")

    log("--- Fetching Anthropic News ---")
    anthropic_web = fetch_web_anthropic()

    log("--- Fetching Leiphone ---")
    leiphone = fetch_web_leiphone()

    log("--- Fetching Smarthey ---")
    smarthey = fetch_web_smarthey()

    log("--- Fetching DEV Community (ai_tools) ---")
    dev_tools = fetch_dev_community("ai_tools_discovery")

    log("--- Fetching DEV Community (mcp) ---")
    dev_mcp = fetch_dev_community("mcp_ecosystem")

    # Step 3+4: assemble per topic — each topic uses its own search results to avoid cross-dedup
    topic_raw = {
        "claude_ecosystem": releases + anthropic_web,
        "ai_tools_discovery": trending + gh_tools + hn_tools + dev_tools,
        "china_ai_trends": leiphone + smarthey,
        "mcp_ecosystem": gh_mcp + hn_mcp + dev_mcp,
        "spec_driven_dev": gh_spec + hn_spec,
    }

    topic_labels = {
        "claude_ecosystem": "🔧 Claude 生态",
        "ai_tools_discovery": "🛠️ AI 工具发现",
        "china_ai_trends": "🏢 国内 AI 动态",
        "mcp_ecosystem": "🔌 MCP 生态",
        "spec_driven_dev": "📐 Spec 驱动开发",
    }

    results = {}
    for tid, raw in topic_raw.items():
        results[tid] = process_topic(tid, raw)
        log(f"Topic {tid}: {len(results[tid])} items after filter")

    total_items = sum(len(v) for v in results.values())
    if total_items == 0:
        log("All topics empty — printing summary and exiting (skip_if_empty=true)")
        for url, v in seen_urls.items():
            print(v)
        sys.exit(0)

    # Step 5: build card
    hot_items = get_hot_items()
    elements = []

    for topic_id in ["claude_ecosystem", "ai_tools_discovery", "china_ai_trends",
                     "mcp_ecosystem", "spec_driven_dev"]:
        items = results.get(topic_id, [])
        if not items:
            continue
        lines = [f"**{topic_labels[topic_id]}**"]
        for item in items:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']})")
            lines.append(f"  _{item['why']}_")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)}
        })
        elements.append({"tag": "hr"})

    if hot_items:
        lines = ["**🔥 持续热点（连续多日高热）**"]
        for item in sorted(hot_items, key=lambda x: -x["count"])[:5]:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']}) · 已连续 {item['count']} 天")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)}
        })
        elements.append({"tag": "hr"})

    skip_msg = "、".join(skipped_sources) if skipped_sources else "无"
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": (
                f"数据来源：GitHub · HN · 雷峰网 · DEV Community"
                f"｜修改关注维度：编辑 watch.yaml\n⚠️ 跳过：{skip_msg}"
            )
        }]
    })

    card_data = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {TODAY}"},
                "template": "blue"
            },
            "elements": elements
        }
    }

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(card_data, f, ensure_ascii=False, indent=2)

    # Validate
    with open(REPORT_PATH, "r", encoding="utf-8") as f:
        json.load(f)
    log("JSON validation passed")

    # Write seen.json
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)
    log(f"seen.json written: {len(seen_urls)} entries")

    # Git commit + push on feature branch
    branch = f"claude/wizardly-allen-KhfeY"
    cmds = [
        ["git", "config", "user.email", "routine-bot@ai-news"],
        ["git", "config", "user.name", "AI News Routine"],
        ["git", "add", REPORT_PATH, SEEN_PATH],
        ["git", "commit", "-m", f"chore: daily report {TODAY}"],
        ["git", "push", "-u", "origin", branch],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"git cmd failed: {' '.join(cmd)}")
            log(f"  stdout: {r.stdout[:200]}")
            log(f"  stderr: {r.stderr[:200]}")
            if "nothing to commit" in r.stderr or "nothing to commit" in r.stdout:
                log("Nothing new to commit, continuing")
                continue
            if cmd[1] == "push":
                log("Push failed — printing report to stdout as fallback:")
                print(json.dumps(card_data, ensure_ascii=False, indent=2))
                sys.exit(1)
        else:
            log(f"OK: {' '.join(cmd[:3])}")

    log("=== Done ===")


if __name__ == "__main__":
    main()
