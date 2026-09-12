#!/usr/bin/env python3
"""
Wikipedia Vital Articles Monitor (Level 1–5)
监控英文维基百科 Vital Articles 1–5 级词条名单的新增和移除。

Level 1–3 的词条直接写在各自页面中；Level 4 和 Level 5 则拆分在多个
“子列表页面”中。本脚本会在每次检查时自动从 Level 4/5 总页面发现当前
子列表，再汇总其中的词条，因此今后 Wikipedia 调整子列表结构时也能跟上。
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    raise


# 监控配置：级别编号 -> 总页面标题
LEVELS: Dict[int, str] = {
    1: "Wikipedia:Vital_articles/Level_1",
    2: "Wikipedia:Vital_articles/Level_2",
    3: "Wikipedia:Vital_articles/Level_3",
    4: "Wikipedia:Vital_articles/Level_4",
    5: "Wikipedia:Vital_articles/Level_5",
}

# Level 4、5 的总页面本身没有完整词条名单；名单在它们当前链接的子页面中。
SPLIT_LIST_LEVELS = {4, 5}

API_URL = "https://en.wikipedia.org/w/api.php"
REQUEST_TIMEOUT_SECONDS = 90
REQUEST_GAP_SECONDS = 1.0        # 礼貌地降低对 Wikipedia API 的请求频率
MAX_API_RETRIES = 4
SUBPAGE_BATCH_SIZE = 8           # 一次读取的子页面数，避免单次回应过大
MAX_ITEMS_PER_NOTIFICATION_SECTION = 100

# 防止 API/页面结构临时异常时把一个很小的不完整集合保存成新快照，进而造成误报。
# 这些只是安全下限，并不是固定的官方目标数量。
MINIMUM_ARTICLE_COUNTS = {
    1: 8,
    2: 80,
    3: 800,
    4: 8000,
    5: 40000,
}

# 排除的命名空间。主命名空间中也可能有冒号（例如 Star Trek: ...），
# 因此只排除已知的非词条命名空间，不能简单地排除所有含冒号的链接。
NAMESPACES_EXCLUDE = {
    "wikipedia", "wp", "file", "image", "category", "template",
    "mediawiki", "user", "help", "portal", "draft", "timedtext",
    "module", "gadget", "gadget definition", "gadget discussion",
    "special", "media", "topic", "education program", "wikipedia talk",
    "commons", "meta", "wiktionary", "wikt", "wikibooks", "wikiquote",
    "wikinews", "wikisource", "wikiversity", "wikivoyage", "wikidata",
}

LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", flags=re.DOTALL)
LIST_ITEM_PATTERN = re.compile(r"^\s*#+")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "VitalArticlesMonitor/2.0 (personal GitHub Actions monitoring tool)",
    "Accept": "application/json",
})


@dataclass
class CollectedLevel:
    """一次读取某个级别后得到的词条和来源信息。"""

    articles: Set[str]
    revision_timestamp: str
    source_pages: List[str]
    collection_method: str


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    """把列表按固定大小拆分。"""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def api_get(params: Dict) -> Dict:
    """带有短暂退避重试的 MediaWiki API 请求。"""
    last_error: Optional[Exception] = None

    for attempt in range(MAX_API_RETRIES):
        # 多个子页面请求之间留一点间隔，避免给 Wikipedia API 制造突发流量。
        time.sleep(REQUEST_GAP_SECONDS)
        try:
            response = SESSION.get(
                API_URL,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            # 429（请求过快）和临时的 5xx 错误可以稍后重试。
            if response.status_code == 429 or 500 <= response.status_code < 600:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait_seconds = max(1.0, float(retry_after))
                except ValueError:
                    wait_seconds = float(2 ** (attempt + 1))
                wait_seconds = min(wait_seconds, 30.0)
                print(
                    f"  Wikipedia API 暂时返回 {response.status_code}，"
                    f"{wait_seconds:.0f} 秒后重试（{attempt + 1}/{MAX_API_RETRIES}）"
                )
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            data = response.json()

            # maxlag / ratelimited 有时会以 HTTP 200 + JSON error 的形式返回。
            api_error = data.get("error")
            if api_error:
                code = api_error.get("code", "unknown")
                if code in {"maxlag", "ratelimited"} and attempt < MAX_API_RETRIES - 1:
                    wait_seconds = float(2 ** (attempt + 1))
                    print(
                        f"  Wikipedia API 暂时繁忙（{code}），"
                        f"{wait_seconds:.0f} 秒后重试（{attempt + 1}/{MAX_API_RETRIES}）"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise RuntimeError(f"Wikipedia API 错误（{code}）：{api_error.get('info', '')}")

            return data

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == MAX_API_RETRIES - 1:
                break
            wait_seconds = float(2 ** (attempt + 1))
            print(
                f"  请求 Wikipedia 时出错：{exc}；"
                f"{wait_seconds:.0f} 秒后重试（{attempt + 1}/{MAX_API_RETRIES}）"
            )
            time.sleep(wait_seconds)

    raise RuntimeError(f"连续请求 Wikipedia API 失败：{last_error}")


def get_page_content(page: Dict) -> Tuple[str, str]:
    """从 API 的单页结果中取出最新 wikitext 和修订时间。"""
    if "missing" in page:
        raise ValueError(f"页面不存在: {page.get('title', '未知页面')}")

    revisions = page.get("revisions") or []
    if not revisions:
        raise ValueError(f"页面没有可读取的修订内容: {page.get('title', '未知页面')}")

    revision = revisions[0]
    slot = revision.get("slots", {}).get("main", {})
    # formatversion=2 通常使用 content；保留 * 是为了兼容旧式 API 回应。
    content = slot.get("content", slot.get("*"))
    if content is None:
        raise ValueError(f"无法读取页面正文: {page.get('title', '未知页面')}")

    timestamp = revision.get("timestamp", "")
    if not timestamp:
        raise ValueError(f"无法读取页面修订时间: {page.get('title', '未知页面')}")

    return content, timestamp


def page_list_from_response(data: Dict) -> List[Dict]:
    """兼容 formatversion=2 和较旧格式，返回页面结果列表。"""
    try:
        pages = data["query"]["pages"]
    except KeyError as exc:
        raise RuntimeError("Wikipedia API 回应中没有页面内容") from exc
    if isinstance(pages, dict):
        return list(pages.values())
    return pages


def fetch_wikitext(page_title: str) -> Tuple[str, str]:
    """通过 MediaWiki API 获取一个页面的原始 wikitext 和修订时间。"""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "revisions",
        "titles": page_title,
        "rvprop": "content|timestamp",
        "rvslots": "main",
        "rvlimit": "1",
        "redirects": "1",
        "maxlag": "5",
    }
    pages = page_list_from_response(api_get(params))
    if len(pages) != 1:
        raise ValueError(f"读取页面时得到异常数量的结果: {page_title}")
    return get_page_content(pages[0])


def fetch_many_wikitext(page_titles: List[str]) -> List[Tuple[str, str, str]]:
    """分批读取多个页面，返回 (实际标题, wikitext, 修订时间)。"""
    fetched: List[Tuple[str, str, str]] = []

    for title_batch in chunks(page_titles, SUBPAGE_BATCH_SIZE):
        # 查询多个页面时不要写 rvlimit：MediaWiki API 只允许 rvlimit 用于单页查询。
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "revisions",
            "titles": "|".join(title_batch),
            "rvprop": "content|timestamp",
            "rvslots": "main",
            "redirects": "1",
            "maxlag": "5",
        }
        pages = page_list_from_response(api_get(params))
        if len(pages) != len(title_batch):
            raise ValueError(
                "没有完整读取 Level 子列表页面；为了避免误报，本次不会更新快照。"
            )

        for page in pages:
            content, timestamp = get_page_content(page)
            fetched.append((page.get("title", "未知页面"), content, timestamp))

    if len(fetched) != len(page_titles):
        raise ValueError("Level 子列表读取数量不完整；为了避免误报，本次不会更新快照。")
    return fetched


def normalize_title(title: str) -> str:
    """统一 Wikipedia 标题的空格和下划线写法，避免无意义的误差异。"""
    title = title.replace("_", " ").strip()
    return re.sub(r"\s+", " ", title)


def clean_link_target(raw_target: str) -> str:
    """把一个 [[链接目标]] 转成用于比较的标题。"""
    target = raw_target.split("#", 1)[0].strip()
    if target.startswith(":"):
        target = target[1:].strip()
    return normalize_title(target)


def is_article_title(target: str) -> bool:
    """判断链接是否属于普通词条（主命名空间）。"""
    if not target:
        return False
    if ":" in target:
        prefix = target.split(":", 1)[0].strip().casefold()
        if prefix in NAMESPACES_EXCLUDE or "talk" in prefix:
            return False
    return True


def extract_articles(wikitext: str) -> Set[str]:
    """从 wikitext 中提取普通词条链接，排除命名空间链接与 HTML 注释。"""
    wikitext = HTML_COMMENT_PATTERN.sub("", wikitext)
    articles: Set[str] = set()

    for raw_target in LINK_PATTERN.findall(wikitext):
        target = clean_link_target(raw_target)
        if not is_article_title(target):
            continue
        # 英文 Wikipedia 的普通标题首字母不区分大小写；统一为大写，便于集合比较。
        articles.add(target[0].upper() + target[1:])

    return articles


def extract_articles_from_list_items(wikitext: str) -> Set[str]:
    """提取 Level 4/5 子列表中的词条。

    这些页面的正式词条均使用以 # 开头的 MediaWiki 列表行。只读取这些行可避开
    页面说明文字中提到的其他词条，避免把“参见”链接误判为 Vital Article。
    """
    wikitext = HTML_COMMENT_PATTERN.sub("", wikitext)
    list_only_text = "\n".join(
        line for line in wikitext.splitlines() if LIST_ITEM_PATTERN.match(line)
    )
    return extract_articles(list_only_text)


def discover_sublist_pages(root_wikitext: str, level: int) -> List[str]:
    """从 Level 4/5 总页面发现当前实际承载词条名单的子页面。"""
    canonical_prefix = f"Wikipedia:Vital articles/Level {level}/"
    prefix_folded = canonical_prefix.casefold()
    subpages: Set[str] = set()

    # 只看总页面正文中直接列出的当前子列表；不要扫描所有历史子页面，
    # 因为 Wikipedia 里保留着旧名称、草稿和归档页面。
    root_wikitext = HTML_COMMENT_PATTERN.sub("", root_wikitext)
    for raw_target in LINK_PATTERN.findall(root_wikitext):
        target = clean_link_target(raw_target)
        if target.casefold().startswith(prefix_folded):
            subpages.add(target)

    if not subpages:
        raise ValueError(
            f"未能从 Level {level} 总页面找到当前子列表；为了避免误报，本次不会更新快照。"
        )

    return sorted(subpages)


def collect_level(level: int) -> CollectedLevel:
    """读取某一级别的完整词条集合。"""
    page_title = LEVELS[level]
    print(f"  [Level {level}] 获取页面: {page_title}")
    root_wikitext, root_timestamp = fetch_wikitext(page_title)

    if level not in SPLIT_LIST_LEVELS:
        articles = extract_articles(root_wikitext)
        source_pages = [page_title]
        revision_timestamp = root_timestamp
        method = "总页面"
    else:
        source_pages = discover_sublist_pages(root_wikitext, level)
        print(f"  [Level {level}] 发现 {len(source_pages)} 个当前子列表页面，正在汇总…")
        subpages = fetch_many_wikitext(source_pages)

        articles: Set[str] = set()
        timestamps = [root_timestamp]
        for source_title, wikitext, timestamp in subpages:
            subpage_articles = extract_articles_from_list_items(wikitext)
            if not subpage_articles:
                raise ValueError(
                    f"子列表页面没有提取到词条: {source_title}；为了避免误报，本次不会更新快照。"
                )
            articles.update(subpage_articles)
            timestamps.append(timestamp)

        # 所有 API 时间均为 UTC ISO 格式，字符串比较即可得到最新修订时间。
        revision_timestamp = max(timestamps)
        method = "自动发现并汇总当前子列表"

    minimum = MINIMUM_ARTICLE_COUNTS[level]
    if len(articles) < minimum:
        raise ValueError(
            f"只提取到 {len(articles):,} 个词条，低于 Level {level} 的安全下限 "
            f"{minimum:,}；为了避免误报，本次不会更新快照。"
        )

    print(f"  [Level {level}] 提取 {len(articles):,} 个不重复词条")
    return CollectedLevel(
        articles=articles,
        revision_timestamp=revision_timestamp,
        source_pages=source_pages,
        collection_method=method,
    )


class LevelMonitor:
    """单个级别的监控器。"""

    def __init__(self, level: int, data_dir: Path):
        self.level = level
        self.page_title = LEVELS[level]
        self.data_dir = data_dir
        self.snapshot_file = data_dir / f"level_{level}_snapshot.json"
        self.history_file = data_dir / f"level_{level}_history.json"

    def load_snapshot(self) -> Optional[Dict]:
        if not self.snapshot_file.exists():
            return None
        with open(self.snapshot_file, "r", encoding="utf-8") as file:
            return json.load(file)

    def save_snapshot(self, collected: CollectedLevel):
        snapshot = {
            "schema_version": 2,
            "level": self.level,
            "timestamp": collected.revision_timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total_count": len(collected.articles),
            "collection_method": collected.collection_method,
            "source_page_count": len(collected.source_pages),
            "source_pages": collected.source_pages,
            "articles": sorted(collected.articles),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.snapshot_file, "w", encoding="utf-8") as file:
            json.dump(snapshot, file, ensure_ascii=False, indent=2)

    def save_history(self, record: Dict):
        history: List[Dict] = []
        if self.history_file.exists():
            with open(self.history_file, "r", encoding="utf-8") as file:
                history = json.load(file)
        history.append(record)
        with open(self.history_file, "w", encoding="utf-8") as file:
            json.dump(history, file, ensure_ascii=False, indent=2)

    def is_legacy_invalid_split_snapshot(self, snapshot: Dict) -> bool:
        """识别旧版若曾把 Level 4/5 误存为 0 条的快照，避免一次性假报警。"""
        if self.level not in SPLIT_LIST_LEVELS:
            return False
        old_articles = snapshot.get("articles", [])
        return (
            not snapshot.get("source_pages")
            or len(old_articles) < MINIMUM_ARTICLE_COUNTS[self.level]
        )

    def check(self) -> Optional[Dict]:
        """检查一次变动；首次建立快照时返回 None，不发送通知。"""
        collected = collect_level(self.level)
        old_snapshot = self.load_snapshot()

        if old_snapshot is None:
            self.save_snapshot(collected)
            print(f"  [Level {self.level}] 首次运行，已保存快照（不发送微信通知）")
            return None

        # 如果有人曾用“只加 4/5 两行”的旧版脚本运行过，它会把总页面误解析成 0 条。
        # 自动重新建立正确的 Level 4/5 基准，避免发出数万条的假变动通知。
        if self.is_legacy_invalid_split_snapshot(old_snapshot):
            self.save_snapshot(collected)
            print(
                f"  [Level {self.level}] 检测到旧版无效快照，已重新建立正确基准"
                "（不发送微信通知）"
            )
            return None

        old_articles = set(old_snapshot.get("articles", []))
        added = collected.articles - old_articles
        removed = old_articles - collected.articles

        result = {
            "level": self.level,
            "revision_timestamp": collected.revision_timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total": len(collected.articles),
            "old_total": len(old_articles),
            "source_page_count": len(collected.source_pages),
            "added": sorted(added),
            "removed": sorted(removed),
        }

        if added or removed:
            print(
                f"  [Level {self.level}] 检测到词条变动！"
                f"+{len(added)} -{len(removed)}"
            )
            record = {
                "level": self.level,
                "revision_timestamp": collected.revision_timestamp,
                "checked_at": result["checked_at"],
                "added": result["added"],
                "removed": result["removed"],
                "old_total": len(old_articles),
                "new_total": len(collected.articles),
                "source_page_count": len(collected.source_pages),
                "source_pages": collected.source_pages,
            }
            self.save_history(record)
            self.save_snapshot(collected)
        else:
            print(f"  [Level {self.level}] 无词条增减")
            # 没有词条变化时不重写快照，避免 GitHub 每 6 小时产生一次无意义提交。

        return result


def article_url(article_title: str) -> str:
    """构造可点击的英文 Wikipedia 词条链接。"""
    wiki_title = article_title.replace(" ", "_")
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
        wiki_title, safe="()'.,-"
    )


def append_article_changes(lines: List[str], articles: List[str], marker: str):
    """把词条变化写入通知，并在极少见的大批量变化时避免消息过长。"""
    for article in articles[:MAX_ITEMS_PER_NOTIFICATION_SECTION]:
        lines.append(f"   {marker} {article}  ({article_url(article)})")

    remaining = len(articles) - MAX_ITEMS_PER_NOTIFICATION_SECTION
    if remaining > 0:
        lines.append(
            f"   … 其余 {remaining} 个为避免微信消息过长未展开；"
            "完整名单已保存到仓库 va_monitor_data 中对应的 history.json 文件。"
        )


def format_level_notification(result: Dict) -> str:
    """格式化单个级别的变动通知。"""
    level = result["level"]
    lines = [f"【Level {level}】"]
    lines.append(f"页面: {LEVELS[level]}")
    if level in SPLIT_LIST_LEVELS:
        lines.append(f"数据来源: {result['source_page_count']} 个当前子列表页面")
    lines.append(f"最新修订时间: {result['revision_timestamp']}")
    lines.append(
        f"统计: {result['old_total']:,} → {result['total']:,} "
        f"({result['total'] - result['old_total']:+,})"
    )

    if result["removed"]:
        lines.append(f"❌ 移除 ({len(result['removed'])} 个):")
        append_article_changes(lines, result["removed"], "-")

    if result["added"]:
        lines.append(f"✅ 新增 ({len(result['added'])} 个):")
        append_article_changes(lines, result["added"], "+")

    lines.append("")
    return "\n".join(lines)


def format_combined_notification(results: List[Dict]) -> str:
    """合并多个级别的变动通知。"""
    lines = [
        "=" * 60,
        "📢 Wikipedia Vital Articles 词条变动通知",
        f"检测时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 60,
        "",
    ]

    for result in results:
        lines.append(format_level_notification(result))

    lines.append("=" * 60)
    return "\n".join(lines)


def send_serverchan(title: str, body: str, sct_key: str):
    """通过 Server 酱发送微信通知。"""
    if not sct_key:
        return
    url = f"https://sctapi.ftqq.com/{sct_key}.send"
    try:
        response = requests.post(url, data={"title": title, "desp": body}, timeout=20)
        print(f"  ServerChan 发送结果: {response.status_code}")
        if response.status_code != 200:
            print(f"  ServerChan 回应: {response.text[:300]}")
    except Exception as exc:
        print(f"  ServerChan 发送失败: {exc}")


def send_bark(title: str, body: str, bark_key: str, server: str = "https://api.day.app"):
    """通过 Bark 发送 iOS 通知（可选）。"""
    if not bark_key:
        return
    url = (
        f"{server}/{urllib.parse.quote(bark_key)}/"
        f"{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
    )
    try:
        response = requests.get(url, timeout=20)
        print(f"  Bark 发送结果: {response.status_code}")
    except Exception as exc:
        print(f"  Bark 发送失败: {exc}")


def send_telegram(title: str, body: str, bot_token: str, chat_id: str):
    """通过 Telegram 发送通知（可选）。"""
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"*{title}*\n\n{body}"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        print(f"  Telegram 发送结果: {response.status_code}")
    except Exception as exc:
        print(f"  Telegram 发送失败: {exc}")


def send_dingtalk(title: str, body: str, webhook_url: str, secret: Optional[str] = None):
    """通过钉钉机器人发送通知（可选）。"""
    if not webhook_url:
        return

    timestamp = str(round(time.time() * 1000))
    if secret:
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        sign_bytes = hmac.new(
            secret.encode("utf-8"),
            string_to_sign,
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(sign_bytes))
        webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"### {title}\n\n{body.replace(chr(10), chr(10) + chr(10))}",
        },
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=20)
        print(f"  DingTalk 发送结果: {response.status_code}")
    except Exception as exc:
        print(f"  DingTalk 发送失败: {exc}")


def main():
    parser = argparse.ArgumentParser(
        description="Wikipedia Vital Articles 监控工具（Level 1–5）"
    )
    parser.add_argument("--run-once", action="store_true", help="运行一次后退出")
    parser.add_argument(
        "--interval", type=float, default=24, help="循环监控间隔（小时），默认 24"
    )
    parser.add_argument(
        "--data-dir", type=str, default="./va_monitor_data", help="数据保存目录"
    )
    parser.add_argument(
        "--notify-serverchan", type=str, default="", help="Server 酱 SendKey（微信推送）"
    )
    parser.add_argument("--notify-bark", type=str, default="", help="Bark Key（iOS 推送）")
    parser.add_argument(
        "--notify-telegram-bot", type=str, default="", help="Telegram Bot Token"
    )
    parser.add_argument(
        "--notify-telegram-chat", type=str, default="", help="Telegram Chat ID"
    )
    parser.add_argument(
        "--notify-dingtalk", type=str, default="", help="钉钉 Webhook URL"
    )
    parser.add_argument(
        "--notify-dingtalk-secret", type=str, default="", help="钉钉加签 Secret"
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(exist_ok=True)
    monitors = [LevelMonitor(level, data_dir) for level in sorted(LEVELS)]

    def run_check():
        print(
            f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            "开始检查 Level 1–5…"
        )
        changed_results: List[Dict] = []

        for monitor in monitors:
            try:
                result = monitor.check()
                if result and (result["added"] or result["removed"]):
                    changed_results.append(result)
            except Exception as exc:
                # 单一级别临时出错不会阻止其它级别继续检查，也不会覆盖它的旧快照。
                print(f"  [Level {monitor.level}] 检查出错: {exc}")

        if changed_results:
            print(f"\n共 {len(changed_results)} 个级别有词条变动")
            notification = format_combined_notification(changed_results)
            print("\n" + notification)

            title = "Vital Articles 变动"
            for result in changed_results:
                title += (
                    f" L{result['level']}"
                    f"(+{len(result['added'])} -{len(result['removed'])});"
                )

            if args.notify_serverchan:
                send_serverchan(title, notification, args.notify_serverchan)
            if args.notify_bark:
                send_bark(title, notification, args.notify_bark)
            if args.notify_telegram_bot and args.notify_telegram_chat:
                send_telegram(
                    title,
                    notification,
                    args.notify_telegram_bot,
                    args.notify_telegram_chat,
                )
            if args.notify_dingtalk:
                send_dingtalk(
                    title,
                    notification,
                    args.notify_dingtalk,
                    args.notify_dingtalk_secret,
                )
        else:
            print("\nLevel 1–5 均无词条增减（未发送微信通知）")

    if args.run_once:
        run_check()
    else:
        interval_seconds = args.interval * 3600
        print(f"启动持续监控，每 {args.interval} 小时检查一次")
        print("按 Ctrl+C 停止\n")
        while True:
            run_check()
            next_check = datetime.now() + timedelta(seconds=interval_seconds)
            print(f"\n下次检查: {next_check.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
