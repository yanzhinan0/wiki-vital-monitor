#!/usr/bin/env python3
"""
Wikipedia Vital Articles Monitor (Level 1 + 2 + 3)
监控英文维基百科 Vital Articles 1-3 级页面的词条变动
"""

import re
import json
import time
import hashlib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Set, Dict, Optional, List, Tuple

try:
    import requests
except ImportError:
    print("请安装 requests: pip install requests")
    raise


# 监控配置：级别编号 -> 页面标题
LEVELS: Dict[int, str] = {
    1: "Wikipedia:Vital_articles/Level_1",
    2: "Wikipedia:Vital_articles/Level_2",
    3: "Wikipedia:Vital_articles/Level_3",
}

API_URL = "https://en.wikipedia.org/w/api.php"

# 排除的命名空间
NAMESPACES_EXCLUDE = {
    'wikipedia', 'wp', 'file', 'image', 'category', 'template',
    'mediawiki', 'user', 'help', 'portal', 'draft', 'timedtext',
    'module', 'gadget', 'special', 'wikipedia talk'
}


def fetch_wikitext(page_title: str) -> Tuple[str, str]:
    """通过 MediaWiki API 获取页面原始 wikitext 和修订时间"""
    params = {
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "titles": page_title,
        "rvprop": "content|timestamp",
        "rvslots": "main",
        "rvlimit": 1,
        "redirects": 1,
    }
    headers = {
        "User-Agent": "VitalArticlesMonitor/1.0 (personal monitoring tool)"
    }
    resp = requests.get(API_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    pages = data["query"]["pages"]
    page = next(iter(pages.values()))
    if "missing" in page:
        raise ValueError(f"页面不存在: {page_title}")
    revision = page["revisions"][0]
    content = revision["slots"]["main"]["*"]
    timestamp = revision["timestamp"]
    return content, timestamp


def extract_articles(wikitext: str) -> Set[str]:
    """从 wikitext 中提取普通词条（排除命名空间链接）"""
    pattern = r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]'
    matches = re.findall(pattern, wikitext)
    articles = set()
    for match in matches:
        target = match.split('#')[0].strip()
        if not target:
            continue
        if target.startswith(':'):
            target = target[1:].strip()
        if not target:
            continue
        if ':' in target:
            prefix = target.split(':')[0].strip().lower()
            if prefix in NAMESPACES_EXCLUDE or 'talk' in prefix:
                continue
        if target:
            articles.add(target[0].upper() + target[1:])
    return articles


class LevelMonitor:
    """单个级别的监控器"""
    def __init__(self, level: int, data_dir: Path):
        self.level = level
        self.page_title = LEVELS[level]
        self.data_dir = data_dir
        self.snapshot_file = data_dir / f"level_{level}_snapshot.json"
        self.history_file = data_dir / f"level_{level}_history.json"

    def load_snapshot(self) -> Optional[Dict]:
        if not self.snapshot_file.exists():
            return None
        with open(self.snapshot_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def save_snapshot(self, articles: Set[str], timestamp: str):
        snapshot = {
            "level": self.level,
            "timestamp": timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total_count": len(articles),
            "articles": sorted(list(articles)),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    def save_history(self, record: Dict):
        history = []
        if self.history_file.exists():
            with open(self.history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        history.append(record)
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def check(self) -> Optional[Dict]:
        """检查一次变动，返回结果字典；如果首次运行返回 None"""
        print(f"  [Level {self.level}] 获取页面: {self.page_title}")
        wikitext, revision_timestamp = fetch_wikitext(self.page_title)
        current_articles = extract_articles(wikitext)
        print(f"  [Level {self.level}] 提取 {len(current_articles)} 个词条")

        old_snapshot = self.load_snapshot()

        if old_snapshot is None:
            self.save_snapshot(current_articles, revision_timestamp)
            print(f"  [Level {self.level}] 首次运行，已保存快照")
            return None

        old_articles = set(old_snapshot["articles"])
        added = current_articles - old_articles
        removed = old_articles - current_articles

        result = {
            "level": self.level,
            "revision_timestamp": revision_timestamp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total": len(current_articles),
            "old_total": len(old_articles),
            "added": sorted(list(added)),
            "removed": sorted(list(removed)),
        }

        if added or removed:
            print(f"  [Level {self.level}] 检测到变动！+{len(added)} -{len(removed)}")
            record = {
                "level": self.level,
                "revision_timestamp": revision_timestamp,
                "checked_at": result["checked_at"],
                "added": result["added"],
                "removed": result["removed"],
                "old_total": len(old_articles),
                "new_total": len(current_articles),
            }
            self.save_history(record)
            self.save_snapshot(current_articles, revision_timestamp)
        else:
            print(f"  [Level {self.level}] 无变动")
            self.save_snapshot(current_articles, revision_timestamp)

        return result


def format_level_notification(result: Dict) -> str:
    """格式化单个级别的变动通知"""
    level = result["level"]
    lines = [f"【Level {level}】"]
    lines.append(f"页面: {LEVELS[level]}")
    lines.append(f"修订时间: {result['revision_timestamp']}")
    lines.append(f"统计: {result['old_total']} → {result['total']} ({result['total'] - result['old_total']:+d})")

    if result["removed"]:
        lines.append(f"❌ 移除 ({len(result['removed'])} 个):")
        for a in result["removed"]:
            url = f"https://en.wikipedia.org/wiki/{a.replace(' ', '_')}"
            lines.append(f"   - {a}  ({url})")

    if result["added"]:
        lines.append(f"✅ 新增 ({len(result['added'])} 个):")
        for a in result["added"]:
            url = f"https://en.wikipedia.org/wiki/{a.replace(' ', '_')}"
            lines.append(f"   + {a}  ({url})")

    lines.append("")
    return "\n".join(lines)


def format_combined_notification(results: List[Dict]) -> str:
    """合并多个级别的变动通知"""
    lines = []
    lines.append("=" * 60)
    lines.append("📢 Wikipedia Vital Articles 变动通知")
    lines.append(f"检测时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 60)
    lines.append("")

    for r in results:
        lines.append(format_level_notification(r))

    lines.append("=" * 60)
    return "\n".join(lines)


def send_serverchan(title: str, body: str, sct_key: str):
    if not sct_key:
        return
    url = f"https://sctapi.ftqq.com/{sct_key}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": body}, timeout=10)
        print(f"  ServerChan 发送结果: {resp.status_code}")
    except Exception as e:
        print(f"  ServerChan 发送失败: {e}")


def send_bark(title: str, body: str, bark_key: str, server: str = "https://api.day.app"):
    if not bark_key:
        return
    import urllib.parse
    url = f"{server}/{urllib.parse.quote(bark_key)}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
    try:
        resp = requests.get(url, timeout=10)
        print(f"  Bark 发送结果: {resp.status_code}")
    except Exception as e:
        print(f"  Bark 发送失败: {e}")


def send_telegram(title: str, body: str, bot_token: str, chat_id: str):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"*{title}*\n\n{body}"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}, timeout=10)
        print(f"  Telegram 发送结果: {resp.status_code}")
    except Exception as e:
        print(f"  Telegram 发送失败: {e}")


def send_dingtalk(title: str, body: str, webhook_url: str, secret: Optional[str] = None):
    if not webhook_url:
        return
    import urllib.parse
    import hmac
    import base64
    timestamp = str(round(time.time() * 1000))
    if secret:
        secret_enc = secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"### {title}\n\n{body.replace(chr(10), chr(10)+chr(10))}"
        }
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        print(f"  DingTalk 发送结果: {resp.status_code}")
    except Exception as e:
        print(f"  DingTalk 发送失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="Wikipedia Vital Articles 监控工具 (Level 1-3)")
    parser.add_argument("--run-once", action="store_true", help="运行一次后退出")
    parser.add_argument("--interval", type=float, default=24, help="循环监控间隔（小时），默认 24")
    parser.add_argument("--data-dir", type=str, default="./va_monitor_data", help="数据保存目录")
    parser.add_argument("--notify-serverchan", type=str, default="", help="ServerChan SendKey（微信推送）")
    parser.add_argument("--notify-bark", type=str, default="", help="Bark Key（iOS 推送）")
    parser.add_argument("--notify-telegram-bot", type=str, default="", help="Telegram Bot Token")
    parser.add_argument("--notify-telegram-chat", type=str, default="", help="Telegram Chat ID")
    parser.add_argument("--notify-dingtalk", type=str, default="", help="钉钉 Webhook URL")
    parser.add_argument("--notify-dingtalk-secret", type=str, default="", help="钉钉加签 Secret")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(exist_ok=True)

    monitors = [LevelMonitor(lvl, data_dir) for lvl in sorted(LEVELS.keys())]

    def run_check():
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始检查 Level 1-3...")
        changed_results: List[Dict] = []
        for m in monitors:
            try:
                result = m.check()
                if result and (result["added"] or result["removed"]):
                    changed_results.append(result)
            except Exception as e:
                print(f"  [Level {m.level}] 检查出错: {e}")

        if changed_results:
            print(f"\n共 {len(changed_results)} 个级别有变动")
            notification = format_combined_notification(changed_results)
            print("\n" + notification)

            # 发送通知
            title = "Vital Articles 变动"
            for r in changed_results:
                title += f" L{r['level']}(+{len(r['added'])} -{len(r['removed'])});"

            if args.notify_serverchan:
                send_serverchan(title, notification, args.notify_serverchan)
            if args.notify_bark:
                send_bark(title, notification, args.notify_bark)
            if args.notify_telegram_bot and args.notify_telegram_chat:
                send_telegram(title, notification, args.notify_telegram_bot, args.notify_telegram_chat)
            if args.notify_dingtalk:
                send_dingtalk(title, notification, args.notify_dingtalk, args.notify_dingtalk_secret)
        else:
            print("\nLevel 1-3 均无变动")

    if args.run_once:
        run_check()
    else:
        interval_seconds = args.interval * 3600
        print(f"启动持续监控，每 {args.interval} 小时检查一次")
        print("按 Ctrl+C 停止\n")
        while True:
            run_check()
            next_check = datetime.now() + __import__('datetime').timedelta(seconds=interval_seconds)
            print(f"\n下次检查: {next_check.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
