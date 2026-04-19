"""
auto_post.py
============
Gemini で日本語ツイートを生成し、X (Twitter) API v2 で自動投稿する。

特徴
- ジャンル比 テック4 : 金融3 : 自然科学3(HN含む)
- 1回の起動で 1ツイート(GitHub Actions の朝/夕 2クロンで 1日2投稿)
- 起動後にランダムスリープ(0〜N分)で投稿時刻を完全ランダム化
- 48時間以内に公開された記事しか投稿しない(週遅れ防止)
- SQLite で重複投稿を恒久ブロック
- HN だけでなく Reuters/FRB/ArXiv/Nature/Quanta も並列に拾う

実行例:
    python auto_post.py 240         # 0〜240分の範囲でランダムにスリープ後、1投稿
    python auto_post.py 240 --dry   # スリープ&Gemini整形まで、X投稿はしない

必要な環境変数:
    GEMINI_API_KEY
    X_API_KEY
    X_API_SECRET
    X_ACCESS_TOKEN
    X_ACCESS_SECRET
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import tweepy
from google import genai

# ============================================================
# 設定
# ============================================================

# ジャンル別ソース。1ジャンル内では先頭から順に新着を見て、まだ投稿してない
# かつ 48 時間以内の記事を最初の1本だけ採用する。
FEEDS: dict[str, list[str]] = {
    "tech": [
        # HN (高品質フィルタ付き)
        "https://hnrss.org/frontpage?points=150",
        "https://hnrss.org/best",
        # AI/ML 論文
        "http://export.arxiv.org/rss/cs.AI",
        "http://export.arxiv.org/rss/cs.LG",
        # 一般テック
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ],
    "finance": [
        # 主要メディア
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/marketsNews",
        "https://www.ft.com/?format=rss",
        # 中央銀行公式
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.ecb.europa.eu/rss/press.html",
        # 金融分析ブログ
        "https://www.calculatedriskblog.com/feeds/posts/default",
    ],
    "science": [
        # 主要科学誌
        "https://www.nature.com/nature.rss",
        "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
        "https://www.quantamagazine.org/feed/",
        # arXiv 物理 / 天文 / 生物
        "http://export.arxiv.org/rss/physics",
        "http://export.arxiv.org/rss/astro-ph",
        "http://export.arxiv.org/rss/q-bio",
        # 一般科学ニュース
        "https://phys.org/rss-feed/",
    ],
}

# 重み: tech 4 / finance 3 / science 3
GENRE_WEIGHTS: list[str] = ["tech"] * 4 + ["finance"] * 3 + ["science"] * 3

# 何時間以内の記事を採用するか(週遅れ防止)
MAX_AGE_HOURS = 48

DB_PATH = os.environ.get("POSTS_DB_PATH", "posts.db")
GEMINI_MODEL = "gemini-2.5-flash"  # 高速・激安・十分賢い

# Gemini への指示プロンプト (English tweets for a global audience)
PROMPT_TEMPLATE = """You write a single X (Twitter) post in ENGLISH, for a global
audience interested in tech, finance, and natural science.

# Output format (strict, 3 lines, <=260 chars total so URL fits)
Line 1: The original article title (verbatim, OR a crisper rewrite <= 90 chars
        if the original is clunky). No emojis. No quotes.
Line 2: -> one-line take (<= 80 chars). A sharp, non-generic angle:
        why this matters, a contrarian read, a question, or an implication.
        Do NOT summarize the article — add something.
Line 3: 2 hashtags (space-separated, English, lowercase camelCase allowed),
        then the URL verbatim.

# Good example (genre: finance)
BoJ at impasse as yen breaches 160
-> Verbal intervention alone won't hold this line much longer.
#forex #BoJ https://example.com/yen

# Bad examples (do NOT do)
- Translating the title into another language
- Generic takes like "Interesting read!" / "Worth a look"
- Bullet points, numbered lists, or markdown
- More than 3 lines
- Preamble like "Here is the tweet:"

# Input
genre: {genre}
title: {title}
summary: {summary}
url: {url}

Output the 3-line tweet only. No other text.
""".strip()

# ============================================================
# ロギング
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("autopost")


# ============================================================
# DB ヘルパ
# ============================================================

def db_open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posted(
            url_hash TEXT PRIMARY KEY,
            url      TEXT NOT NULL,
            title    TEXT,
            genre    TEXT,
            posted_at INTEGER NOT NULL
        )"""
    )
    conn.commit()
    return conn


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def already_posted(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM posted WHERE url_hash=?", (url_hash(url),)
    ).fetchone() is not None


def mark_posted(conn: sqlite3.Connection, url: str, title: str, genre: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO posted VALUES(?,?,?,?,?)",
        (url_hash(url), url, title[:300], genre, int(time.time())),
    )
    conn.commit()


# ============================================================
# RSS から候補を1本選ぶ
# ============================================================

def parse_published(entry) -> Optional[datetime]:
    """feedparser の published_parsed を tz-aware UTC に変換"""
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(k)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def pick_candidate(conn: sqlite3.Connection, genre: str):
    """指定ジャンルの全フィードを順に見て、最初の有効候補を返す"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    for feed_url in FEEDS[genre]:
        try:
            log.info("Fetching feed: %s", feed_url)
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.warning("Feed parse failed %s: %s", feed_url, e)
            continue
        for entry in feed.entries[:30]:
            link = entry.get("link")
            if not link:
                continue
            # ageフィルタ
            pub = parse_published(entry)
            if pub and pub < cutoff:
                continue
            # 重複フィルタ
            if already_posted(conn, link):
                continue
            return entry
    return None


# ============================================================
# Gemini で整形
# ============================================================

def make_tweet(g_client: "genai.Client", genre: str, entry) -> str:
    summary = (
        entry.get("summary")
        or entry.get("description")
        or entry.get("subtitle")
        or ""
    )
    # HTMLタグ除去のために単純な置換(完全ではないが十分)
    summary = (
        summary.replace("<p>", " ").replace("</p>", " ")
        .replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    )
    summary = summary[:600]

    prompt = PROMPT_TEMPLATE.format(
        genre=genre,
        title=entry.title,
        summary=summary,
        url=entry.link,
    )

    resp = g_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"temperature": 0.7, "max_output_tokens": 500},
    )
    text = (resp.text or "").strip()

    # 280字を超えていたら末尾URL以外を圧縮(URLは絶対残す)
    if len(text) > 280:
        log.warning("Tweet too long (%d), trimming.", len(text))
        text = text[:277] + "..."
    return text


# ============================================================
# X 投稿
# ============================================================

def post_to_x(text: str) -> dict:
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    res = client.create_tweet(text=text)
    return res.data or {}


# ============================================================
# メイン
# ============================================================

def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry" in args
    args = [a for a in args if a != "--dry"]
    sleep_window_min = int(args[0]) if args else 240

    # 完全ランダムスリープ(0 〜 sleep_window_min 分)
    delay_min = random.randint(0, max(0, sleep_window_min))
    log.info("Sleeping %d minutes before posting (window=%d)", delay_min, sleep_window_min)
    time.sleep(delay_min * 60)

    conn = db_open()

    # ジャンルを重み付きで1つ抽選
    genre = random.choice(GENRE_WEIGHTS)
    log.info("Selected genre: %s", genre)

    # 候補を探す。第1選択が空なら他ジャンルで救済。
    entry = pick_candidate(conn, genre)
    if entry is None:
        log.warning("No candidate in '%s' — trying other genres.", genre)
        for g in random.sample(["tech", "finance", "science"], 3):
            if g == genre:
                continue
            entry = pick_candidate(conn, g)
            if entry is not None:
                genre = g
                break
    if entry is None:
        log.error("No candidate in any genre. Giving up this run.")
        return 1

    log.info("Selected: [%s] %s | %s", genre, entry.title, entry.link)

    # Gemini で整形
    g_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tweet = make_tweet(g_client, genre, entry)
    log.info("Generated tweet (%d chars):\n%s", len(tweet), tweet)

    if dry_run:
        log.info("[DRY RUN] Skipping X post.")
        return 0

    # X 投稿
    res = post_to_x(tweet)
    log.info("Posted to X: %s", res)

    mark_posted(conn, entry.link, entry.title, genre)
    return 0


if __name__ == "__main__":
    sys.exit(main())
