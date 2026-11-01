#!/usr/bin/env python3
"""
Wikipedia Vital Articles Monitor (Level 1–3)
只监控英文维基百科 Vital Articles Level 1、Level 2、Level 3 的词条新增和移除。
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    raise


# 只保留 Level 1、2、3。不要在这里保留 Level 4 或 Level 5。
LEVELS: Dict[int, str] = {
    1: "Wikipedia:Vital_articles/Level_1",
    2: "Wikipedia:Vital_articles/Level_2",
    3: "Wikipedia:Vital_articles/Level_3",
}

API_URL = "https://en.wikipedia.org/w/api.php"
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_GAP_SECONDS = 1.0
MAX_API_RETRIES = 4
MAX_ITEMS_PER_NOTIFICATION_SECTION = 100

# 避免网络异常时把明显不完整的名单写入快照，造成错误通知。
MINIMUM_ARTICLE_COUNTS = {1: 8, 2: 80, 3: 800}

NAMESPACES_EXCLUDE = {
    "wikipedia", "wp", "file", "image", "category", "template",
    "mediawiki", "user", "help", "portal", "draft", "timedtext",
    "module", "gadget", "gadget definition", "gadget discussion",
    "special", "media", "topic", "education program", "wikipedia talk",
    "commons", "meta", "wiktionary", "wikt", "wikibooks", "wikiquote",
    "wikinews", "wikisource", "wikiversity", "wikivoyage", "wikidata", "m",
}

LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", flags=re.DOTALL)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "VitalArticlesMonitor/2.1 (personal GitHub Actions monitoring tool)",
    "Accept": "application/json",
})


def api_get(params: Dict) -> Dict:
    """带短暂退避重试的 MediaWiki API 请求。"""
    last_error: Optional[Exception] = None

    for attempt in range(MAX_API_RETRIES):
        time.sleep(REQUEST_GAP_SECONDS)
        try:
            response = SESSION.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)

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


def fetch_wikitext(page_title: str) -> Tuple[str, str]:
    """通过 MediaWiki API 获取页面原始 wikitext 和最新修订时间。"""
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
    data = api_get(params)
    try:
        pages = data["query"]["pages"]
        page = pages[0] if isinstance(pages, list) else next(iter(pages.values()))
    except (KeyError, IndexError, StopIteration) as exc:
        raise RuntimeError(f"无法读取页面: {page_title}") from exc

    if "missing" in page:
        raise ValueError(f"页面不存在: {page_title}")

    revisions = page.get("revisions") or []
    if not revisions:
        raise ValueError(f"页面没有可读取的修订内容: {page_title}")

    revision = revisions[0]
    slot = revision.get("slots", {}).get("main", {})
    content = slot.get("content", slot.get("*"))
    timestamp = revision.get("timestamp", "")
    if content is None or not timestamp:
        raise ValueError(f"无法读取页面正文或修订时间: {page_title}")

    return content, timestamp


def normalize_title(title: str) -> str:
    """统一标题中的空格与下划线，避免无意义的格式差异。"""
    title = title.replace("_", " ").strip()
    return re.sub(r"\s+", " ", title)


def extract_articles(wikitext: str) -> Set[str]:
    """从 wikitext 中提取普通词条，排除命名空间链接和编辑注释。"""
    articles: Set[str] = set()
    wikitext = HTML_COMMENT_PATTERN.sub("", wikitext)

    for raw_target in LINK_PATTERN.findall(wikitext):
        target = raw_target.split("#", 1)[0].strip()
        if target.startswith(":"):
            target = target[1:].strip()
        target = normalize_title(target)
        if not target:
            continue

        if ":" in target:
            prefix = target.split(":", 1)[0].strip().casefold()
            if prefix in NAMESPACES_EXCLUDE or "talk" in prefix:
                continue

        # 英文 Wikipedia 普通标题首字母不区分大小写，统一后便于集合比较。
        articles.add(target[0].upper() + target[1:])

    return articles


class LevelMonitor:
    """单个 Level 的快照、对比和历史记录。"""

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

    def save_snapshot(self, articles: Set[str], timestamp: str):
        snapshot = {
            "schema_version": 2,
            "level": self.level,
            "timestamp": timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total_count": len(articles),
            "source_pages": [self.page_title],
            "articles": sorted(articles),
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

    def check(self) -> Optional[Dict]:
        """检查一次；首次创建快照时返回 None，因此不会发送微信通知。"""
        print(f"  [Level {self.level}] 获取页面: {self.page_title}")
        wikitext, revision_timestamp = fetch_wikitext(self.page_title)
        current_articles = extract_articles(wikitext)

        minimum = MINIMUM_ARTICLE_COUNTS[self.level]
        if len(current_articles) < minimum:
            raise ValueError(
                f"只提取到 {len(current_articles)} 个词条，低于安全下限 {minimum}；"
                "为了避免误报，本次不会更新快照。"
            )

        print(f"  [Level {self.level}] 提取 {len(current_articles):,} 个词条")
        old_snapshot = self.load_snapshot()

        if old_snapshot is None:
            self.save_snapshot(current_articles, revision_timestamp)
            print(f"  [Level {self.level}] 首次运行，已保存快照（不发送微信通知）")
            return None

        old_articles = set(old_snapshot.get("articles", []))
        added = current_articles - old_articles
        removed = old_articles - current_articles
        result = {
            "level": self.level,
            "revision_timestamp": revision_timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total": len(current_articles),
            "old_total": len(old_articles),
            "added": sorted(added),
            "removed": sorted(removed),
        }

        if added or removed:
            print(f"  [Level {self.level}] 检测到词条变动！+{len(added)} -{len(removed)}")
            self.save_history({
                "level": self.level,
                "revision_timestamp": revision_timestamp,
                "checked_at": result["checked_at"],
                "added": result["added"],
                "removed": result["removed"],
                "old_total": len(old_articles),
                "new_total": len(current_articles),
            })
            self.save_snapshot(current_articles, revision_timestamp)
        else:
            print(f"  [Level {self.level}] 无词条增减")
            # 无词条变化时不改写快照，避免每 6 小时产生无意义的 Git 提交。

        return result


def article_url(article_title: str) -> str:
    wiki_title = article_title.replace(" ", "_")
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
        wiki_title, safe="()'.,-"
    )


def append_article_changes(lines: List[str], articles: List[str], marker: str):
    for article in articles[:MAX_ITEMS_PER_NOTIFICATION_SECTION]:
        lines.append(f"   {marker} {article}  ({article_url(article)})")
    remaining = len(articles) - MAX_ITEMS_PER_NOTIFICATION_SECTION
    if remaining > 0:
        lines.append(
            f"   … 其余 {remaining} 个为避免微信消息过长未展开；"
            "完整名单已保存到仓库 va_monitor_data 中对应的 history.json 文件。"
        )


def format_level_notification(result: Dict) -> str:
    level = result["level"]
    lines = [
        f"【Level {level}】",
        f"页面: {LEVELS[level]}",
        f"最新修订时间: {result['revision_timestamp']}",
        f"统计: {result['old_total']:,} → {result['total']:,} "
        f"({result['total'] - result['old_total']:+,})",
    ]

    if result["removed"]:
        lines.append(f"❌ 移除 ({len(result['removed'])} 个):")
        append_article_changes(lines, result["removed"], "-")
    if result["added"]:
        lines.append(f"✅ 新增 ({len(result['added'])} 个):")
        append_article_changes(lines, result["added"], "+")

    lines.append("")
    return "\n".join(lines)


def format_combined_notification(results: List[Dict]) -> str:
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
    if not sct_key:
        return
    try:
        response = requests.post(
            f"https://sctapi.ftqq.com/{sct_key}.send",
            data={"title": title, "desp": body},
            timeout=20,
        )
        print(f"  ServerChan 发送结果: {response.status_code}")
        if response.status_code != 200:
            print(f"  ServerChan 回应: {response.text[:300]}")
    except Exception as exc:
        print(f"  ServerChan 发送失败: {exc}")


def send_bark(title: str, body: str, bark_key: str, server: str = "https://api.day.app"):
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
    if not bot_token or not chat_id:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"*{title}*\n\n{body}",
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        print(f"  Telegram 发送结果: {response.status_code}")
    except Exception as exc:
        print(f"  Telegram 发送失败: {exc}")


def send_dingtalk(title: str, body: str, webhook_url: str, secret: Optional[str] = None):
    if not webhook_url:
        return

    timestamp = str(round(time.time() * 1000))
    if secret:
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        sign_bytes = hmac.new(
            secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256
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
        description="Wikipedia Vital Articles 监控工具（Level 1–3）"
    )
    parser.add_argument("--run-once", action="store_true", help="运行一次后退出")
    parser.add_argument("--interval", type=float, default=24, help="循环监控间隔（小时）")
    parser.add_argument("--data-dir", type=str, default="./va_monitor_data", help="数据保存目录")
    parser.add_argument("--notify-serverchan", type=str, default="", help="Server 酱 SendKey（微信推送）")
    parser.add_argument("--notify-bark", type=str, default="", help="Bark Key（iOS 推送）")
    parser.add_argument("--notify-telegram-bot", type=str, default="", help="Telegram Bot Token")
    parser.add_argument("--notify-telegram-chat", type=str, default="", help="Telegram Chat ID")
    parser.add_argument("--notify-dingtalk", type=str, default="", help="钉钉 Webhook URL")
    parser.add_argument("--notify-dingtalk-secret", type=str, default="", help="钉钉加签 Secret")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(exist_ok=True)
    monitors = [LevelMonitor(level, data_dir) for level in sorted(LEVELS)]

    def run_check():
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始检查 Level 1–3…")
        changed_results: List[Dict] = []

        for monitor in monitors:
            try:
                result = monitor.check()
                if result and (result["added"] or result["removed"]):
                    changed_results.append(result)
            except Exception as exc:
                print(f"  [Level {monitor.level}] 检查出错: {exc}")

        if changed_results:
            print(f"\n共 {len(changed_results)} 个级别有词条变动")
            notification = format_combined_notification(changed_results)
            print("\n" + notification)

            title = "Vital Articles 变动"
            for result in changed_results:
                title += f" L{result['level']}(+{len(result['added'])} -{len(result['removed'])});"

            if args.notify_serverchan:
                send_serverchan(title, notification, args.notify_serverchan)
            if args.notify_bark:
                send_bark(title, notification, args.notify_bark)
            if args.notify_telegram_bot and args.notify_telegram_chat:
                send_telegram(title, notification, args.notify_telegram_bot, args.notify_telegram_chat)
            if args.notify_dingtalk:
                send_dingtalk(title, notification, args.notify_dingtalk, args.notify_dingtalk_secret)
        else:
            print("\nLevel 1–3 均无词条增减（未发送微信通知）")

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
