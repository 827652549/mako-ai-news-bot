#!/usr/bin/env python3
"""
AI News Bot — daily tech-news fetcher that posts a Feishu card.

Steps:
  1. Load watch.yaml + .ai-news-bot/seen.json
  2. Fetch GitHub Releases / Trending / Search, HN, Web, DEV Community
  3. Filter (time window, exclude keywords) + deduplicate
  4. Build Feishu interactive-card via Python dicts → json.dump
  5. Write files, push to report/YYYY-MM-DD branch
     → triggers .github/workflows/notify-feishu.yml
"""

import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse

import yaml

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed.  Run: pip install requests PyYAML")

# ---------------------------------------------------------------------------
# Paths & global constants
# ---------------------------------------------------------------------------

WATCH_YAML = "watch.yaml"
SEEN_PATH = ".ai-news-bot/seen.json"
REPORT_PATH = ".ai-news-bot/latest-report.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_HEADERS: dict[str, str] = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

TOPIC_LABELS: dict[str, str] = {
    "claude_ecosystem":    "🔧 Claude 生态",
    "ai_tools_discovery":  "🛠️ AI 工具发现",
    "china_ai_trends":     "🏢 国内 AI 动态",
    "mcp_ecosystem":       "🔌 MCP 生态",
    "spec_driven_dev":     "📐 Spec 驱动开发",
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: int = 15) -> "requests.Response | None":
    try:
        return requests.get(url, headers=headers or {}, timeout=timeout)
    except Exception as exc:
        print(f"  WARN  GET {url[:80]}: {exc}")
        return None


def _web(url: str, use_jina: bool = False) -> str:
    """Fetch a URL; fall back to Jina Reader if direct fetch is thin/blocked."""
    if not use_jina:
        r = _get(url, {"User-Agent": UA})
        if r and r.status_code == 200 and len(r.text) > 500:
            return r.text
        print(f"  INFO  falling back to Jina for {url[:70]}")
    r = _get(f"https://r.jina.ai/{url}", {"User-Agent": UA})
    return r.text if r and r.status_code == 200 else ""


# ---------------------------------------------------------------------------
# Step 1 — Config & seen.json
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(WATCH_YAML):
        sys.exit("ERROR: watch.yaml not found")
    with open(WATCH_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_seen(ttl_days: int) -> dict:
    if not os.path.exists(SEEN_PATH):
        return {}
    with open(SEEN_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    urls: dict = data.get("urls", {})
    cutoff = str(datetime.now(timezone.utc).date() - timedelta(days=ttl_days))
    stale = [u for u, v in urls.items() if v.get("last_seen", "9999") < cutoff]
    for u in stale:
        del urls[u]
    return urls


def save_seen(seen_urls: dict) -> None:
    os.makedirs(".ai-news-bot", exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump({"urls": seen_urls}, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Step 2 — Fetchers
# ---------------------------------------------------------------------------

def _parse_dt(s: str | None) -> "datetime | None":
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ── A. GitHub Releases ───────────────────────────────────────────────────────

def fetch_releases(repos: list[str], since: datetime) -> list[dict]:
    items: list[dict] = []
    for repo in repos:
        r = _get(f"https://api.github.com/repos/{repo}/releases?per_page=5", GH_HEADERS)
        if not r or r.status_code != 200:
            print(f"  WARN  releases {repo}: {r.status_code if r else 'no response'}")
            continue
        for rel in r.json():
            pub = _parse_dt(rel.get("published_at"))
            if not pub or pub < since:
                continue
            body = (rel.get("body") or "")[:300].replace("\r\n", " ").replace("\n", " ").strip()
            items.append({
                "title":        f"{repo} {rel['tag_name']}",
                "url":          rel.get("html_url", ""),
                "published_at": rel.get("published_at", ""),
                "summary":      body,
                "source":       "github_releases",
                "repo":         repo,
                "tag":          rel["tag_name"],
            })
    return items


# ── B. GitHub Trending ───────────────────────────────────────────────────────

def fetch_trending(kws: list[str]) -> list[dict]:
    items: list[dict] = []
    seen_repo_urls: set[str] = set()
    urls = [
        "https://github.com/trending?since=daily",
        "https://github.com/trending?since=daily&spoken_language_code=zh",
    ]
    for page_url in urls:
        html = _web(page_url)
        if not html:
            continue
        blocks = re.findall(
            r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
            html, re.DOTALL,
        )
        for block in blocks:
            m = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', block)
            if not m:
                continue
            repo_path = m.group(1)
            repo_url = f"https://github.com/{repo_path}"
            if repo_url in seen_repo_urls:
                continue

            dm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            desc = html_lib.unescape(re.sub(r'<[^>]+>', '', dm.group(1))).strip() if dm else ""

            sm = re.search(r'([\d,]+)\s*stars?\s*today', block, re.IGNORECASE)
            today_stars = int(sm.group(1).replace(",", "")) if sm else 0

            if kws and not any(kw.lower() in f"{repo_path} {desc}".lower() for kw in kws):
                continue

            seen_repo_urls.add(repo_url)
            items.append({
                "title":        repo_path,
                "url":          repo_url,
                "description":  desc,
                "stars_today":  today_stars,
                "published_at": None,
                "source":       "github_trending",
            })
    return items


# ── C. GitHub Search ─────────────────────────────────────────────────────────

def fetch_search(queries: list[str], since: datetime, skipped: list[str]) -> list[dict]:
    items: list[dict] = []
    for q in queries:
        url = (
            f"https://api.github.com/search/repositories"
            f"?q={quote(q)}&sort=stars&order=desc&per_page=10"
        )
        r = _get(url, GH_HEADERS)
        if not r:
            skipped.append("GitHub Search（网络错误）")
            continue
        if r.headers.get("X-RateLimit-Remaining") == "0":
            skipped.append("GitHub Search（Rate Limit 耗尽）")
            continue
        if r.status_code != 200:
            skipped.append(f"GitHub Search（{r.status_code}）")
            continue
        for repo in r.json().get("items", []):
            created = _parse_dt(repo.get("created_at"))
            updated = _parse_dt(repo.get("updated_at"))
            if not ((created and created >= since) or (updated and updated >= since)):
                continue
            items.append({
                "title":        repo.get("full_name", ""),
                "url":          repo.get("html_url", ""),
                "description":  repo.get("description") or "",
                "stars":        repo.get("stargazers_count", 0),
                "published_at": repo.get("updated_at"),
                "source":       "github_search",
            })
    return items


# ── D. HN Algolia ────────────────────────────────────────────────────────────

def fetch_hn(kws: list[str], min_pts: int, since: datetime) -> list[dict]:
    ts = int(since.timestamp())
    items: list[dict] = []
    seen_ids: set[str] = set()
    for kw in kws:
        url = (
            f"https://hn.algolia.com/api/v1/search?query={quote(kw)}&tags=story"
            f"&numericFilters=created_at_i>{ts},points>{min_pts}&hitsPerPage=5"
        )
        r = _get(url)
        if not r or r.status_code != 200:
            continue
        for hit in r.json().get("hits", []):
            oid = hit.get("objectID", "")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            pub_ts = hit.get("created_at_i", 0)
            items.append({
                "title":        hit.get("title", ""),
                "url":          hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "points":       hit.get("points", 0),
                "published_at": datetime.utcfromtimestamp(pub_ts)
                                        .replace(tzinfo=timezone.utc).isoformat(),
                "source":       "hn",
            })
    return items


# ── E. Web (direct or Jina) ──────────────────────────────────────────────────

def fetch_web_source(cfg: dict, since: datetime) -> list[dict]:
    url       = cfg["url"]
    use_jina  = cfg.get("use_jina", False)
    must_kws  = cfg.get("must_match_any", [])

    text = _web(url, use_jina=use_jina)
    if not text:
        return []

    base = urlparse(url)
    items: list[dict] = []
    seen_urls: set[str] = set()

    for href, anchor in re.findall(r'<a[^>]+href="([^"#]{5,})"[^>]*>([^<]{8,200})</a>', text):
        anchor = html_lib.unescape(re.sub(r'<[^>]+>', '', anchor)).strip()
        if len(anchor) < 8:
            continue
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            full = f"{base.scheme}://{base.netloc}{href}"
        else:
            continue
        if full in seen_urls:
            continue
        if must_kws:
            if not any(kw.lower() in f"{anchor} {full}".lower() for kw in must_kws):
                continue
        seen_urls.add(full)
        items.append({
            "title":        anchor,
            "url":          full,
            "published_at": None,
            "source":       "web",
        })

    return items[:15]


# ── F. DEV Community ─────────────────────────────────────────────────────────

def fetch_dev(tags: list[str], since: datetime, skipped: list[str]) -> list[dict]:
    items: list[dict] = []
    seen_ids: set[str] = set()
    for tag in tags:
        r = _get(f"https://dev.to/api/articles?tag={tag}&per_page=10&top=1", {"User-Agent": UA})
        if not r or r.status_code != 200:
            skipped.append(f"DEV Community/{tag}（{r.status_code if r else 'err'}）")
            continue
        for art in r.json():
            pub = _parse_dt(art.get("published_at"))
            if pub and pub < since:
                continue
            aid = art.get("id", "")
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            items.append({
                "title":        art.get("title", ""),
                "url":          art.get("url", ""),
                "published_at": art.get("published_at"),
                "reactions":    art.get("positive_reactions_count", 0),
                "source":       "dev_community",
            })
    return items


# ---------------------------------------------------------------------------
# Step 3 — Filter & deduplicate
# ---------------------------------------------------------------------------

def _in_window(item: dict, since: datetime) -> bool:
    s = item.get("published_at")
    if s is None:
        return True
    dt = _parse_dt(s) if isinstance(s, str) else s
    return dt is None or dt >= since


def _has_excluded(item: dict, exc: list[str]) -> bool:
    t = f"{item.get('title', '')} {item.get('description', '')}".lower()
    return any(k.lower() in t for k in exc)


def dedup_releases(items: list[dict]) -> list[dict]:
    """Per-repo keep only the newest release in current batch."""
    best: dict[str, dict] = {}
    rest: list[dict] = []
    for item in items:
        if item.get("source") != "github_releases":
            rest.append(item)
            continue
        repo = item.get("repo", "")
        pub = _parse_dt(item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)
        if repo not in best:
            best[repo] = item
        else:
            ex_pub = _parse_dt(best[repo].get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)
            if pub > ex_pub:
                best[repo] = item
    return rest + list(best.values())


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _score(item: dict) -> int:
    return item.get("points", 0) or item.get("stars", 0) or item.get("stars_today", 0)


def dedup_within_run(items: list[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    result: list[dict] = []
    for item in items:
        url = item.get("url", "")
        if url and url in seen_urls:
            continue
        title = item.get("title", "")
        dup = False
        for ex in result:
            if _sim(title, ex.get("title", "")) > 0.80:
                if _score(item) > _score(ex):
                    result.remove(ex)
                    seen_urls.discard(ex.get("url", ""))
                else:
                    dup = True
                break
        if not dup:
            if url:
                seen_urls.add(url)
            result.append(item)
    return result


def apply_seen(
    items: list[dict], seen_urls: dict, today: str
) -> tuple[list[dict], list[dict]]:
    new_items: list[dict] = []
    hot_items: list[dict] = []
    for item in items:
        url = item.get("url", "")
        if not url:
            new_items.append(item)
            continue
        if url in seen_urls:
            e = seen_urls[url]
            e["last_seen"] = today
            e["count"] = e.get("count", 1) + 1
            if e["count"] >= 3:
                hot_items.append({**item, "count": e["count"]})
        else:
            seen_urls[url] = {
                "title":      item.get("title", "")[:80],
                "url":        url,
                "first_seen": today,
                "last_seen":  today,
                "count":      1,
            }
            new_items.append(item)
    return new_items, hot_items


# ---------------------------------------------------------------------------
# Step 4 — "Why care" annotation + card builder
# ---------------------------------------------------------------------------

def generate_why(item: dict, topic_id: str) -> str:
    title    = item.get("title", "")
    desc     = item.get("description", "") or item.get("summary", "") or ""
    url      = item.get("url", "").lower()
    src      = item.get("source", "")
    combined = f"{title} {desc} {url}".lower()

    # Claude Code
    if "claude-code" in url or "claude code" in combined or "claude_code" in combined:
        if src == "github_releases":
            tag = item.get("tag", "")
            return f"{tag} 发布，直接影响 AI 编程工作流，关注新功能和 breaking changes"
        if src == "hn":
            pts = item.get("points", 0)
            return f"HN {pts}pts 热讨，了解工程师社区对 Claude Code 新功能的真实使用评价"
        if src == "github_trending":
            s = item.get("stars_today", 0)
            return f"Claude Code 相关工具，今日 +{s}⭐，评估能否提升 AI 编程工作流效率"
        return "Claude Code 相关内容，直接关系日常 AI 编程工作流效率"

    # Anthropic SDK
    if "anthropic-sdk" in url or "anthropic sdk" in combined:
        if src == "github_releases":
            tag = item.get("tag", "")
            return f"SDK {tag} 更新，检查新模型支持和 API 变更，影响前端 AI 功能接入代码"
        return "Anthropic SDK 动态，关注 API 兼容性变化和新模型支持情况"

    # MCP
    if "mcp" in combined or "model context protocol" in combined or "model-context-protocol" in url:
        if "server" in combined:
            return "MCP 服务器工具，关注协议扩展能力和与 Claude Code 的集成方式"
        return "MCP 协议动态，是 AI 工具链互操作的核心协议，直接影响工具链选型"

    # Agent + coding context
    if "agent" in combined and any(k in combined for k in ("spec", "coding", "ai", "claude")):
        return "AI Agent 开发工具，了解 multi-agent 协作模式有助于面试中阐述 Agent 架构设计"

    # Spec-driven
    if any(k in combined for k in ("spec-driven", "openspec", "spec-kit", " sdd ")):
        return "Spec 驱动开发工具，与 Claude Code 任务规范化工作流直接相关，值得试用"

    # China AI
    if topic_id == "china_ai_trends":
        for vendor in ("阿里云", "通义", "腾讯", "CodeBuddy", "字节", "豆包", "文心", "混元", "华为", "百度"):
            if vendor in title or vendor in desc:
                return f"国内 {vendor} AI 产品动态，了解大厂 AI 编码工具竞争格局和云服务策略"
        return "国内大厂 AI 产品动态，关注 AI 工具链和云服务策略变化"

    # Source-specific fallbacks
    if src == "github_trending":
        s = item.get("stars_today", 0)
        return f"今日 Trending +{s}⭐，AI 工具方向快速增长项目，评估是否适合集成到工作流"
    if src == "hn":
        pts = item.get("points", 0)
        return f"HN {pts}pts，工程师社区热点，关注技术讨论中暴露的工程化挑战"
    if src == "dev_community":
        return "DEV 社区实战文章，可直接参考工程化实践经验和踩坑总结"

    return "相关技术动态，关注对 AI 编程工作流的潜在影响"


def _trunc(s: str, n: int = 30) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    for sep in (" ", "·", "|", "：", "，", "-"):
        i = cut.rfind(sep)
        if i > n // 2:
            return cut[:i] + "…"
    return cut + "…"


def build_card(
    results: dict, hot_items: list, skipped: list[str], today: str
) -> dict:
    elements: list[dict] = []

    for topic_id, items in results.items():
        if not items:
            continue
        label = TOPIC_LABELS.get(topic_id, topic_id)
        lines = [f"**{label}**"]
        for item in items:
            t   = _trunc(item.get("title", ""))
            url = item.get("url", "")
            why = item.get("why", "")
            lines.append(f"· [{t}]({url})")
            if why:
                lines.append(f"  _{why}_")
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    if hot_items:
        lines = ["**🔥 持续热点（连续多日高热）**"]
        for item in sorted(hot_items, key=lambda x: -x.get("count", 0))[:5]:
            t   = _trunc(item.get("title", ""))
            cnt = item.get("count", 0)
            lines.append(f"· [{t}]({item.get('url', '')}) · 已连续 {cnt} 天")
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})

    # Deduplicate skip messages (same source may report multiple rate-limit errors)
    skip_msg = "、".join(dict.fromkeys(skipped)) if skipped else "无"
    elements.append({
        "tag": "note",
        "elements": [{
            "tag":     "plain_text",
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
                "title":    {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {today}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


# ---------------------------------------------------------------------------
# Step 5 — Write files & git push
# ---------------------------------------------------------------------------

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def git_push(branch: str) -> None:
    _run(["git", "config", "user.email", "routine-bot@ai-news"])
    _run(["git", "config", "user.name",  "AI News Routine"])

    _run(["git", "checkout", "-B", branch])  # create or reset branch
    _run(["git", "add", REPORT_PATH, SEEN_PATH])

    status = _run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("[INFO] Nothing staged — skipping commit & push")
        return

    _run(["git", "commit", "-m", f"chore: daily report {branch.split('/')[-1]}"])

    # Push with exponential back-off (4 retries: 2 s, 4 s, 8 s, 16 s)
    delays = [2, 4, 8, 16]
    for attempt, delay in enumerate(delays, 1):
        result = _run(["git", "push", "-u", "origin", branch], check=False)
        if result.returncode == 0:
            print(f"[OK]  Pushed to origin/{branch}")
            return
        print(f"  WARN  push attempt {attempt}/{len(delays)}: {result.stderr.strip()[:120]}")
        if attempt < len(delays):
            time.sleep(delay)

    print("[ERROR] Push failed after all retries — printing report to log:")
    with open(REPORT_PATH, encoding="utf-8") as fh:
        print(fh.read())
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[Start] AI News Bot  {today}")

    # ── Step 1 ──────────────────────────────────────────────────────────────
    print("[1] Loading watch.yaml + seen.json")
    cfg          = load_config()
    filters      = cfg.get("filters", {})
    ttl_days     = filters.get("seen_ttl_days",        7)
    lookback_h   = filters.get("lookback_hours",       24)
    max_per_topic= filters.get("max_items_per_topic",   5)
    exc_kws      = filters.get("exclude_keywords",     [])
    since        = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
    seen_urls    = load_seen(ttl_days)
    topics       = cfg.get("topics", [])

    # ── Steps 2–4 (per topic) ───────────────────────────────────────────────
    print("[2] Fetching sources")
    results: dict[str, list[dict]] = {}
    skipped: list[str] = []
    all_hot: list[dict] = []

    for topic in topics:
        tid  = topic["id"]
        srcs = topic.get("sources", {})
        raw:  list[dict] = []

        if "github_releases" in srcs:
            raw += fetch_releases(srcs["github_releases"], since)

        if "github_trending" in srcs:
            gt   = srcs["github_trending"]
            raw += fetch_trending(gt.get("must_match_any", []))

        if "github_search" in srcs:
            raw += fetch_search(srcs["github_search"].get("queries", []), since, skipped)

        if "hn" in srcs:
            hn   = srcs["hn"]
            raw += fetch_hn(hn.get("keywords", []), hn.get("min_points", 30), since)

        if "web" in srcs:
            for sc in srcs["web"]:
                raw += fetch_web_source(sc, since)

        if "dev_community" in srcs:
            raw += fetch_dev(srcs["dev_community"].get("tags", []), since, skipped)

        # Step 3: filter + dedup
        raw = [i for i in raw if _in_window(i, since)]
        raw = [i for i in raw if not _has_excluded(i, exc_kws)]
        raw = dedup_releases(raw)
        raw = dedup_within_run(raw)
        new_items, hot = apply_seen(raw, seen_urls, today)
        all_hot += hot
        new_items = new_items[:max_per_topic]

        # Step 4: annotate
        for item in new_items:
            item["why"] = generate_why(item, tid)

        results[tid] = new_items
        print(f"  [Topic] {tid}: {len(new_items)} new, {len(hot)} hot")

    # ── Empty-output guard ───────────────────────────────────────────────────
    total = sum(len(v) for v in results.values())
    if total == 0 and cfg.get("output", {}).get("skip_if_empty", True):
        print("[INFO] No new items; skip_if_empty=true — exiting cleanly")
        save_seen(seen_urls)
        sys.exit(0)

    if total == 0 and skipped:
        print("[ERROR] All sources failed:")
        print("  " + ", ".join(skipped))
        sys.exit(1)

    # ── Step 5: build + write + push ────────────────────────────────────────
    print("[5] Building card, writing files, pushing")
    card = build_card(results, all_hot, skipped, today)

    os.makedirs(".ai-news-bot", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(card, fh, ensure_ascii=False, indent=2)

    # Validate immediately — json.load raises on bad JSON and stops the push
    with open(REPORT_PATH, encoding="utf-8") as fh:
        json.load(fh)
    print(f"[OK]  Report written: {REPORT_PATH}")

    save_seen(seen_urls)
    git_push(f"report/{today}")


if __name__ == "__main__":
    main()
