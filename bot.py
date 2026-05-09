#!/usr/bin/env python3
"""
AI News Bot — daily tech news monitoring agent.

Steps:
  1. Read watch.yaml + .ai-news-bot/seen.json
  2. Fetch all sources (GitHub releases, trending, search, HN, web, DEV)
  3. Filter, deduplicate, score relevance
  4. Build Feishu card via Python dicts → json.dump (never hand-written JSON)
  5. Write .ai-news-bot/latest-report.json + seen.json, push to report/{date} branch

Run:
  python3 bot.py

Env vars:
  GITHUB_TOKEN       — optional but strongly recommended (raises rate limit to 5000/hr)
  ANTHROPIC_API_KEY  — optional; enables AI-generated "why" fields via Claude API
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import Any

import requests
import yaml

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

WATCH_YAML = "watch.yaml"
SEEN_JSON = ".ai-news-bot/seen.json"
REPORT_JSON = ".ai-news-bot/latest-report.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GITHUB_HEADERS: dict[str, str] = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# User persona context for AI-generated "why" fields
USER_PERSONA = (
    "用户是 P6/P7 前端工程师，重点关注 Claude Code 工具链演进、AI 编程工具"
    "（spec-driven / MCP / agent 方向）、国内大厂 AI 产品动态，准备高级工程师面试。"
)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


def truncate_title(title: str, max_len: int = 30) -> str:
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_dt(lookback_hours: int) -> datetime:
    return now_utc() - timedelta(hours=lookback_hours)


def in_window(ts_str: str, cutoff: datetime) -> bool:
    if not ts_str:
        return False
    ts_str = ts_str.rstrip("Z").replace("Z", "+00:00")
    if "+" not in ts_str and ts_str.endswith("+00:00") is False:
        ts_str += "+00:00"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except ValueError:
        return False


def safe_get(url: str, headers: dict | None = None, timeout: int = 15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200 and len(r.text) >= 500:
            return r
        return None
    except Exception:
        return None


def jina_get(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(f"https://r.jina.ai/{url}", headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code == 200:
            return r.text
        return ""
    except Exception:
        return ""


def matches_any(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False


def contains_exclude(text: str, excludes: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in excludes)


# ---------------------------------------------------------------------------
# Step 1: Read config & seen.json
# ---------------------------------------------------------------------------


def read_watch_yaml() -> dict:
    if not os.path.exists(WATCH_YAML):
        log(f"ERROR: {WATCH_YAML} not found, exiting.")
        sys.exit(1)
    with open(WATCH_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_seen_json() -> dict[str, Any]:
    if not os.path.exists(SEEN_JSON):
        return {}
    try:
        with open(SEEN_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("urls", {})
    except (json.JSONDecodeError, KeyError):
        return {}


def clean_seen(seen: dict, ttl_days: int) -> dict:
    cutoff_date = (now_utc() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
    return {
        url: meta
        for url, meta in seen.items()
        if meta.get("last_seen", "9999") >= cutoff_date
    }


# ---------------------------------------------------------------------------
# Step 2A: GitHub Releases
# ---------------------------------------------------------------------------


def fetch_github_releases(repos: list[str], cutoff: datetime) -> list[dict]:
    items = []
    for repo in repos:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
        try:
            r = requests.get(url, headers=GITHUB_HEADERS, timeout=15)
            if r.status_code != 200:
                log(f"GitHub releases {repo}: HTTP {r.status_code}")
                continue
            releases = r.json()
        except Exception as e:
            log(f"GitHub releases {repo}: {e}")
            continue

        # Collect all in-window releases for this repo, then keep only the latest
        in_window_releases = [
            rel for rel in releases
            if in_window(rel.get("published_at", ""), cutoff) and not rel.get("prerelease", False)
        ]
        if not in_window_releases:
            continue
        # Same-repo same-run dedup: keep only latest published_at
        latest = max(in_window_releases, key=lambda r: r.get("published_at", ""))
        body = (latest.get("body") or "")[:400]
        items.append({
            "source": "github_releases",
            "repo": repo,
            "title": f"{repo.split('/')[-1]} {latest['tag_name']}",
            "url": latest["html_url"],
            "published_at": latest.get("published_at", ""),
            "body": body,
            "score": 100,
        })
    return items


# ---------------------------------------------------------------------------
# Step 2B: GitHub Trending
# ---------------------------------------------------------------------------


def fetch_github_trending(languages: list[str], must_match: list[str]) -> list[dict]:
    items = []
    seen_repos: set[str] = set()
    for lang in languages + [""]:
        lang_param = f"&l={lang}" if lang else ""
        url = f"https://github.com/trending?since=daily{lang_param}"
        r = safe_get(url, headers={"User-Agent": UA})
        if not r:
            log(f"GitHub trending {lang or 'all'}: failed")
            continue
        content = r.text
        articles = re.findall(
            r"<article[^>]*class[^>]*Box-row[^>]*>(.*?)</article>", content, re.DOTALL
        )
        for a in articles:
            path_m = re.search(r'href="/([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)"', a)
            desc_m = re.search(r"<p[^>]*>\s*(.*?)\s*</p>", a, re.DOTALL)
            stars_m = re.search(r"([\d,]+)\s+stars today", a)
            lang_m = re.search(r'itemprop="programmingLanguage">(.*?)<', a)
            if not path_m:
                continue
            path = path_m.group(1)
            if path in seen_repos:
                continue
            desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else ""
            stars_today = int(stars_m.group(1).replace(",", "")) if stars_m else 0
            detected_lang = lang_m.group(1) if lang_m else ""
            combined = f"{path} {desc}"
            if not matches_any(combined, must_match):
                continue
            seen_repos.add(path)
            items.append({
                "source": "github_trending",
                "repo": path,
                "title": f"{path} · {detected_lang}",
                "url": f"https://github.com/{path}",
                "description": desc,
                "stars_today": stars_today,
                "published_at": TODAY + "T00:00:00+00:00",
                "score": stars_today,
            })
    return items


# ---------------------------------------------------------------------------
# Step 2C: GitHub Search
# ---------------------------------------------------------------------------


def fetch_github_search(queries: list[str], cutoff: datetime) -> list[dict]:
    # Check rate limit first
    try:
        rl = requests.get(
            "https://api.github.com/rate_limit", headers=GITHUB_HEADERS, timeout=10
        )
        remaining = rl.json().get("resources", {}).get("search", {}).get("remaining", 1)
        if remaining == 0:
            log("GitHub Search: rate limit exhausted, skipping")
            return []
    except Exception:
        remaining = 1

    items = []
    seen_repos: set[str] = set()

    for query in queries:
        encoded = requests.utils.quote(query)
        url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&order=desc&per_page=10"
        try:
            r = requests.get(url, headers=GITHUB_HEADERS, timeout=15)
            if r.status_code != 200:
                log(f"GitHub search '{query}': HTTP {r.status_code}")
                continue
            data = r.json()
        except Exception as e:
            log(f"GitHub search '{query}': {e}")
            continue

        for repo in data.get("items", []):
            full_name = repo.get("full_name", "")
            if full_name in seen_repos:
                continue
            pushed = repo.get("pushed_at", "") or ""
            updated = repo.get("updated_at", "") or ""
            if not in_window(pushed, cutoff) and not in_window(updated, cutoff):
                continue
            seen_repos.add(full_name)
            items.append({
                "source": "github_search",
                "repo": full_name,
                "title": full_name.split("/")[-1],
                "url": repo.get("html_url", f"https://github.com/{full_name}"),
                "description": repo.get("description") or "",
                "stars": repo.get("stargazers_count", 0),
                "published_at": pushed or updated,
                "score": repo.get("stargazers_count", 0),
            })
        time.sleep(1)  # gentle rate limit

    return items


# ---------------------------------------------------------------------------
# Step 2D: HN Algolia
# ---------------------------------------------------------------------------


def fetch_hn(keywords: list[str], min_points: int, cutoff: datetime) -> list[dict]:
    since_ts = int(cutoff.timestamp())
    seen_ids: set[str] = set()
    items = []
    for kw in keywords:
        encoded = requests.utils.quote(kw)
        url = (
            f"https://hn.algolia.com/api/v1/search"
            f"?query={encoded}&tags=story"
            f"&numericFilters=created_at_i%3E{since_ts},points%3E{min_points}"
            f"&hitsPerPage=5"
        )
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                continue
            hits = r.json().get("hits", [])
        except Exception:
            continue
        for h in hits:
            oid = h.get("objectID", "")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            items.append({
                "source": "hn",
                "title": h.get("title", ""),
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                "points": h.get("points", 0),
                "published_at": datetime.fromtimestamp(
                    h.get("created_at_i", 0), tz=timezone.utc
                ).isoformat(),
                "score": h.get("points", 0),
            })
    return items


# ---------------------------------------------------------------------------
# Step 2E: Web scraping
# ---------------------------------------------------------------------------


def fetch_web(sources: list[dict], cutoff: datetime, global_must: list[str] | None = None) -> list[dict]:
    items = []
    for src in sources:
        url = src.get("url", "")
        use_jina = src.get("use_jina", False)
        must_match = src.get("must_match_any", global_must or [])
        if use_jina:
            content = jina_get(url)
        else:
            r = safe_get(url)
            if r:
                content = r.text
            else:
                log(f"Web {url}: failed, trying Jina")
                content = jina_get(url)
        if not content:
            log(f"Web {url}: no content")
            continue
        # Extract article links and titles
        links = re.findall(
            r'href=["\']([^"\']+)["\'][^>]*>\s*([^\n<]{10,120})', content
        )
        for href, title in links:
            title = title.strip()
            if not title or contains_exclude(title, ["img", "class=", "href"]):
                continue
            if must_match and not matches_any(f"{title} {href}", must_match):
                continue
            if not href.startswith("http"):
                base = re.match(r"(https?://[^/]+)", url)
                href = (base.group(1) if base else "") + href
            items.append({
                "source": "web",
                "origin_url": url,
                "title": title[:200],
                "url": href,
                "published_at": TODAY + "T00:00:00+00:00",
                "score": 0,
            })
    return items


# ---------------------------------------------------------------------------
# Step 2F: DEV Community
# ---------------------------------------------------------------------------


def fetch_dev_community(tags: list[str], cutoff: datetime) -> list[dict]:
    seen_ids: set[int] = set()
    items = []
    for tag in tags:
        try:
            r = requests.get(
                f"https://dev.to/api/articles?tag={tag}&per_page=10&top=1",
                timeout=15,
            )
            if r.status_code != 200:
                continue
            articles = r.json()
        except Exception:
            continue
        for a in articles:
            aid = a.get("id")
            if aid in seen_ids:
                continue
            pub = a.get("published_at", "")
            if not in_window(pub, cutoff):
                continue
            seen_ids.add(aid)
            items.append({
                "source": "dev",
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "reactions": a.get("positive_reactions_count", 0),
                "published_at": pub,
                "score": a.get("positive_reactions_count", 0),
                "tags": a.get("tags", []),
            })
    return items


# ---------------------------------------------------------------------------
# Step 3: Filter & deduplicate
# ---------------------------------------------------------------------------


def apply_filters(
    items: list[dict],
    exclude_kws: list[str],
    cutoff: datetime,
    seen_urls: dict,
    max_per_topic: int,
) -> tuple[list[dict], list[dict]]:
    """Return (new_items, hot_items)."""
    filtered = []
    for item in items:
        # Exclude by keyword
        text = item.get("title", "") + " " + item.get("description", "")
        if contains_exclude(text, exclude_kws):
            continue
        # Time window (web sources use TODAY as fallback, so always pass)
        filtered.append(item)

    # Cross-item dedup by URL and title similarity
    deduped: list[dict] = []
    seen_in_run: set[str] = set()
    for item in filtered:
        url = item.get("url", "")
        title = item.get("title", "")
        if url in seen_in_run:
            continue
        # Title similarity check
        skip = False
        for existing in deduped:
            if similarity(title, existing.get("title", "")) > 0.8:
                # Keep the one with higher score
                if item.get("score", 0) > existing.get("score", 0):
                    deduped.remove(existing)
                else:
                    skip = True
                    break
        if not skip:
            seen_in_run.add(url)
            deduped.append(item)

    # seen.json dedup + hot tracking
    new_items: list[dict] = []
    hot_items: list[dict] = []
    for item in deduped:
        url = item.get("url", "")
        if url in seen_urls:
            # Update seen record
            seen_urls[url]["last_seen"] = TODAY
            seen_urls[url]["count"] = seen_urls[url].get("count", 1) + 1
            if seen_urls[url]["count"] >= 3:
                hot_items.append({**item, "count": seen_urls[url]["count"]})
        else:
            seen_urls[url] = {
                "title": item.get("title", "")[:80],
                "url": url,
                "first_seen": TODAY,
                "last_seen": TODAY,
                "count": 1,
            }
            new_items.append(item)

    # Sort by score desc, cap per topic
    new_items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return new_items[:max_per_topic], hot_items


# ---------------------------------------------------------------------------
# "Why" generation
# ---------------------------------------------------------------------------


def generate_why_ai(items: list[dict], topic_name: str) -> list[dict]:
    """Call Claude API to batch-generate 'why' fields."""
    if not ANTHROPIC_API_KEY:
        return items

    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    items_json = json.dumps(
        [{"title": i.get("title", ""), "url": i.get("url", ""), "description": i.get("description", i.get("body", ""))[:200]} for i in items],
        ensure_ascii=False,
    )

    prompt = (
        f"{USER_PERSONA}\n\n"
        f"以下是「{topic_name}」板块中今日抓取的资讯列表（JSON）：\n{items_json}\n\n"
        "为每条资讯生成一个"为什么关注"字段（why），要求：\n"
        "1. 说明与前端/AI工具/国内云厂商的具体关联\n"
        "2. 优先说明①对日常编码工作流的直接影响，②面试高频考点，③技术趋势判断依据\n"
        "3. 禁止使用"建议关注"、"强烈推荐"等无信息量短语\n"
        "4. 长度30-50字\n"
        "5. 输出格式：JSON数组，每个元素只有 url 和 why 两个字段\n"
        "6. 相关性很低的条目，why 填空字符串（将被丢弃）\n"
        "直接输出JSON数组，不要其他内容。"
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fence if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("```").strip()
        why_list = json.loads(raw)
        why_map = {w["url"]: w["why"] for w in why_list}
        for item in items:
            item["why"] = why_map.get(item["url"], "")
    except Exception as e:
        log(f"AI why generation failed: {e}")

    return items


def generate_why_rule(item: dict, topic_id: str) -> str:
    """Fallback rule-based 'why' generation."""
    title = item.get("title", "")
    desc = item.get("description", item.get("body", ""))
    stars = item.get("stars", item.get("stars_today", 0))
    points = item.get("points", 0)
    reactions = item.get("reactions", 0)
    source = item.get("source", "")

    parts = []
    if points > 0:
        parts.append(f"HN {points}pts")
    if stars > 0:
        parts.append(f"{stars}⭐")
    if reactions > 0:
        parts.append(f"{reactions} reactions")

    heat = "、".join(parts) if parts else ""
    summary = (desc or title)[:40]

    if topic_id == "claude_ecosystem":
        return f"{heat}{'，' if heat else ''}{summary[:30]}；Claude Code 生态直接影响日常工作流"
    elif topic_id == "ai_tools_discovery":
        return f"{heat}{'，' if heat else ''}{summary[:30]}；AI 编程工具链新动向"
    elif topic_id == "china_ai_trends":
        return f"国内 AI 动态：{summary[:35]}"
    elif topic_id == "mcp_ecosystem":
        return f"{heat}{'，' if heat else ''}{summary[:30]}；MCP 生态最新进展"
    elif topic_id == "spec_driven_dev":
        return f"{heat}{'，' if heat else ''}{summary[:30]}；Spec 驱动开发实践"
    return f"{heat}{'，' if heat else ''}{summary[:40]}"


# ---------------------------------------------------------------------------
# Step 4: Build card
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
    topic_labels: dict[str, str],
    hot_items: list[dict],
    skipped_sources: list[str],
) -> dict:
    elements: list[dict] = []

    for topic_id, items in results.items():
        if not items:
            continue
        icon = TOPIC_ICONS.get(topic_id, "📌")
        label = topic_labels.get(topic_id, topic_id)
        lines = [f"**{icon} {label}**"]
        for item in items:
            t = truncate_title(item.get("title", ""), 30)
            url = item.get("url", "")
            why = item.get("why", "").strip()
            lines.append(f"· [{t}]({url})")
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
            t = truncate_title(item.get("title", ""), 30)
            url = item.get("url", "")
            cnt = item.get("count", 0)
            lines.append(f"· [{t}]({url}) · 已连续 {cnt} 天")
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
                "数据来源：GitHub · HN · 雷峰网 · DEV Community"
                "｜修改关注维度：编辑 watch.yaml"
                f"\n⚠️ 跳过：{skip_msg}"
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


# ---------------------------------------------------------------------------
# Step 5: Write files & push
# ---------------------------------------------------------------------------


def write_and_push(card_data: dict, seen_urls: dict, dry_run: bool = False) -> None:
    os.makedirs(".ai-news-bot", exist_ok=True)

    # Write card JSON
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(card_data, f, ensure_ascii=False, indent=2)
    # Validate
    with open(REPORT_JSON, "r", encoding="utf-8") as f:
        json.load(f)
    log(f"Wrote {REPORT_JSON}")

    # Write seen.json
    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)
    log(f"Wrote {SEEN_JSON}")

    if dry_run:
        log("dry_run=True, skipping git push")
        return

    branch = f"report/{TODAY}"
    subprocess.run(["git", "config", "user.email", "routine-bot@ai-news"], check=True)
    subprocess.run(["git", "config", "user.name", "AI News Routine"], check=True)
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "add", REPORT_JSON, SEEN_JSON], check=True)
    subprocess.run(["git", "commit", "-m", f"chore: daily report {TODAY}"], check=True)

    # Push with retry
    for attempt, wait in enumerate([0, 2, 4, 8, 16]):
        if wait:
            time.sleep(wait)
        result = subprocess.run(["git", "push", "-u", "origin", branch])
        if result.returncode == 0:
            log(f"Pushed to {branch}")
            return
        log(f"Push attempt {attempt+1} failed, retrying...")

    log("All push attempts failed. Printing report to stdout:")
    print(json.dumps(card_data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def main() -> None:
    config = read_watch_yaml()
    filters_cfg = config.get("filters", {})
    output_cfg = config.get("output", {})
    lookback_hours: int = filters_cfg.get("lookback_hours", 24)
    max_per_topic: int = filters_cfg.get("max_items_per_topic", 5)
    exclude_kws: list[str] = filters_cfg.get("exclude_keywords", [])
    ttl_days: int = filters_cfg.get("seen_ttl_days", 7)
    skip_if_empty: bool = output_cfg.get("skip_if_empty", True)

    cutoff = cutoff_dt(lookback_hours)
    log(f"Run date: {TODAY}, cutoff: {cutoff.isoformat()}")

    seen_urls = read_seen_json()
    seen_urls = clean_seen(seen_urls, ttl_days)

    topics = config.get("topics", [])
    topic_labels = {t["id"]: t["name"] for t in topics}
    results: dict[str, list[dict]] = {}
    all_hot_items: list[dict] = []
    skipped_sources: list[str] = []

    # Global cross-topic URL dedup
    global_url_seen: set[str] = set()

    for topic in topics:
        topic_id = topic["id"]
        topic_name = topic["name"]
        sources = topic.get("sources", {})
        raw_items: list[dict] = []

        # A. GitHub Releases
        if "github_releases" in sources:
            repos = sources["github_releases"]
            try:
                raw_items += fetch_github_releases(repos, cutoff)
            except Exception as e:
                skipped_sources.append(f"GitHub Releases/{topic_id}（{e}）")

        # B. GitHub Trending
        if "github_trending" in sources:
            cfg = sources["github_trending"]
            langs = cfg.get("languages", [])
            must = cfg.get("must_match_any", [])
            try:
                raw_items += fetch_github_trending(langs, must)
            except Exception as e:
                skipped_sources.append(f"GitHub Trending（{e}）")

        # C. GitHub Search
        if "github_search" in sources:
            queries = sources["github_search"].get("queries", [])
            try:
                raw_items += fetch_github_search(queries, cutoff)
            except Exception as e:
                skipped_sources.append(f"GitHub Search/{topic_id}（{e}）")

        # D. HN
        if "hn" in sources:
            hn_cfg = sources["hn"]
            try:
                raw_items += fetch_hn(
                    hn_cfg.get("keywords", []),
                    hn_cfg.get("min_points", 30),
                    cutoff,
                )
            except Exception as e:
                skipped_sources.append(f"HN/{topic_id}（{e}）")

        # E. Web
        if "web" in sources:
            web_sources = sources["web"]
            try:
                raw_items += fetch_web(web_sources, cutoff)
            except Exception as e:
                skipped_sources.append(f"Web/{topic_id}（{e}）")

        # F. DEV Community
        if "dev_community" in sources:
            dev_cfg = sources["dev_community"]
            try:
                raw_items += fetch_dev_community(dev_cfg.get("tags", []), cutoff)
            except Exception as e:
                skipped_sources.append(f"DEV/{topic_id}（{e}）")

        # Cross-topic global dedup by URL
        deduped_raw = []
        for item in raw_items:
            url = item.get("url", "")
            if url not in global_url_seen:
                global_url_seen.add(url)
                deduped_raw.append(item)

        # Filter & seen.json dedup
        new_items, hot_items = apply_filters(
            deduped_raw, exclude_kws, cutoff, seen_urls, max_per_topic
        )
        all_hot_items.extend(hot_items)

        # Generate "why" fields
        if new_items:
            if ANTHROPIC_API_KEY:
                new_items = generate_why_ai(new_items, topic_name)
            for item in new_items:
                if not item.get("why"):
                    item["why"] = generate_why_rule(item, topic_id)
            # Discard items where why is still empty after AI pass
            new_items = [i for i in new_items if i.get("why")]

        results[topic_id] = new_items
        log(f"Topic '{topic_id}': {len(new_items)} items")

    # Skip if all empty
    total_items = sum(len(v) for v in results.values())
    if total_items == 0 and skip_if_empty:
        log("No items found and skip_if_empty=true, exiting.")
        sys.exit(0)

    card_data = build_card(results, topic_labels, all_hot_items, skipped_sources)
    write_and_push(card_data, seen_urls)


if __name__ == "__main__":
    main()
