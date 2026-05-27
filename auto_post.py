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

# Gemini への指示プロンプト (日本語ツイート、丁寧カジュアル)
PROMPT_TEMPLATE = """あなたは X (Twitter) に1本の投稿を**日本語**で書きます。読者は AI / 金融 /
自然科学 に関心のある日本人で、リテラシーは中以上です。単なる要約ではなく、読者が
「なるほど」「面白いですね」と思える視点を1つ加えてください。

## 言葉遣い・トーン
- 「ですます」基調の丁寧カジュアル
- 親しみやすさをキープし、過度に硬くしない / 過度に砕けない
- 自然な締めの例:「〜だと思います」「面白いですね」「気になりますね」「〜ということですね」
- 専門用語は最低限の補足を入れるか、文脈で読める範囲で使ってOK
- 「整備士」「整備工場」「車のメンテと同じで」等の自動車整備に関する比喩・職業ネタは**絶対に使わない**
- 絵文字は使わない / 引用符で全体を囲まない / 「ツイート:」のような前置きを書かない

## 出力フォーマット(厳守、3行、URLが入るので全体260字以内目安)
1行目: 元記事のタイトル(原文ママ、または90字以内に意訳して圧縮)。絵文字・引用符なし。
       元記事が英語の場合は自然な日本語タイトルに訳してください。
2行目: `->` で始まる独自の視点を1行(100字以内目安)。
       投稿ごとに以下の(a)〜(d)からランダムに1つ選んでください:
         (a) 問いかけ ― 読者に考えさせるオープンな質問
         (b) 予測   ― 「今後12ヶ月で〜になると思います」等、ややハッタリ気味の予想
         (c) 逆張り ― 多数派の見方に対する反論や見落とされがちな論点
         (d) 日本視点 ― 日本市場/規制/企業(ソフトバンク、トヨタ、Sakana AI、日銀 等)
                       や日本の生活者にとっての含意
       要約や翻訳だけで終わらせない。必ず視点・意見を1つ足す。
3行目: 日本語ハッシュタグ2個(スペース区切り)、続けて元URLをそのまま貼る。
       ハッシュタグ例: #AI #人工知能 #LLM #生成AI #機械学習 #半導体
                      #金融 #投資 #マクロ経済 #米株 #日銀 #為替 #暗号資産
                      #物理 #宇宙 #量子コンピュータ #生物学 #研究

## 良い例 (深さを真似てください。文面のコピーはしないこと)

[問いかけ]
米10年債利回りが5%に到達
-> 個人投資家はついに株より債券を選ぶようになるんでしょうか。皆さんはどう見ます?
#金利 #米債 https://example.com/yields

[予測]
AppleがオンデバイスAI向け新シリコンを発表
-> 2027年にはハイエンドスマホはほぼ全部LLMを端末側で動かしている、と予想します。クラウドAIは消費者市場では負けそうですね。
#Apple #生成AI https://example.com/apple

[逆張り]
「AIがプログラマを置き換える」アンケート結果
-> 開発者は消えるんじゃなくて、より「何を作るか」を決める側に寄っていくだけだと思います。むしろ需要は増えそう。
#AI #エンジニア https://example.com/survey

[日本視点]
Mistralが640M調達
-> 日本ではSakana AIが現実的な対抗馬ですが、フランス並みの税優遇を経産省が打てるかが鍵ですね。
#Mistral #日本AI https://example.com/mistral

## やってはいけないこと
- 中身のない一般論(「興味深いですね」「必見」だけ等)
- 単なる要約・翻訳で終わる
- 箇条書きや Markdown 記法
- 3行を超える
- 「以下がツイートです:」のような前置き
- 自動車整備関連の比喩・職業ネタ

## 入力
genre: {genre}
title: {title}
summary: {summary}
url: {url}

上記の指示に沿って、3行のツイート本文だけを出力してください。それ以外のテキスト
(説明、前置き、コードブロック等)は一切出力しないでください。
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

def _call_gemini(g_client: "genai.Client", prompt: str) -> str:
    resp = g_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "temperature": 0.85,
            "max_output_tokens": 500,
            "thinking_config": {"thinking_budget": 0},
        },
    )
    return (resp.text or "").strip()


def _validate_tweet(text: str, url: str) -> Optional[str]:
    """戻り値: 不合格理由(文字列)。合格時は None。"""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != 3:
        return f"line count != 3 (got {len(lines)})"
    if not lines[1].lstrip().startswith("->"):
        return "line 2 does not start with '->'"
    hashtag_count = sum(1 for tok in lines[2].split() if tok.startswith("#"))
    if hashtag_count < 2:
        return f"line 3 has < 2 hashtags (got {hashtag_count})"
    if url not in lines[2]:
        return "line 3 missing URL"
    return None


def make_tweet(g_client: "genai.Client", genre: str, entry) -> str:
    summary = (
        entry.get("summary")
        or entry.get("description")
        or entry.get("subtitle")
        or ""
    )
    # HTMLタグ除去のための単純な置換(完全ではないが十分)
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

    text = _call_gemini(g_client, prompt)
    reason = _validate_tweet(text, entry.link)
    if reason is not None:
        log.warning("Tweet format validation failed: %s. Retrying once.", reason)
        text = _call_gemini(g_client, prompt)

    # --- URL guard: URLが本文に無い場合は必ず末尾に追加 ---
    # (Geminiが3行目をサボるケースのフォールバック。URLがない投稿は
    #  クリックスルー価値がゼロになるのでこれは致命的)
    if entry.link not in text:
        log.warning("URL missing from generated tweet, appending.")
        # URLを入れるスペースを確保 (t.co短縮で23字扱いだが余裕を見て原URL長+1)
        reserve = len(entry.link) + 1  # newline
        limit = 280 - reserve
        if len(text) > limit:
            text = text[:max(0, limit - 3)].rstrip() + "..."
        text = text.rstrip() + "\n" + entry.link

    # 280字を超えていたら末尾URL以外を圧縮(URLは絶対残す)
    if len(text) > 280:
        # URLがある場合、URL部分を保持して前半を圧縮
        if entry.link in text:
            pre, url = text.rsplit(entry.link, 1)
            keep = 280 - len(entry.link) - 1  # 1 for separator
            pre = pre[:max(0, keep - 3)].rstrip() + "..."
            text = pre + "\n" + entry.link
        else:
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
# 投稿スロット (4 スロット方式)
# ============================================================
#
# JST 基準で 4 つの候補スロットを用意し、日付ハッシュにより 4 つから
# SLOTS_PER_DAY 個を deterministic にサンプルする。GitHub Actions の
# cron は 4 スロット全てでトリガーするが、Python 側で「当該スロットが
# 今日選ばれていない」なら即 exit する設計。
POST_SLOTS_UTC: list[tuple[int, int]] = [
    (22, 0),   # JST 07:00 (= UTC 22:00 前日)
    (2, 30),   # JST 11:30
    (7, 0),    # JST 16:00
    (13, 30),  # JST 22:30
]
SLOTS_PER_DAY = 2             # 1 日に投稿するスロット数
POST_JITTER_MAX_MIN = 10      # 投稿前に挟む小ランダムスリープの上限(分)

JST = timezone(timedelta(hours=9))


def current_slot_index(now_utc: datetime) -> Optional[tuple[int, datetime]]:
    """now_utc 時点で過去・最直近に発火した POST_SLOTS_UTC のスロット番号と
    そのスケジュール時刻(UTC) のタプルを返す。
    GitHub Actions cron の遅延(数時間オーダー)に耐えるため、上限窓を設けず
    過去で最も近いスロットを常に採用する。"""
    best_idx: Optional[int] = None
    best_diff: Optional[float] = None
    best_scheduled: Optional[datetime] = None
    for i, (h, m) in enumerate(POST_SLOTS_UTC):
        scheduled_today = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
        # 日付境界対策で前日/翌日候補も含めて評価
        for candidate in (scheduled_today - timedelta(days=1),
                          scheduled_today,
                          scheduled_today + timedelta(days=1)):
            diff_sec = (now_utc - candidate).total_seconds()
            if diff_sec >= 0:
                if best_diff is None or diff_sec < best_diff:
                    best_idx = i
                    best_diff = diff_sec
                    best_scheduled = candidate
    if best_idx is None:
        return None
    return (best_idx, best_scheduled)


def selected_slots_for_jst_date(jst_date_iso: str) -> list[int]:
    """JST 日付文字列 (YYYY-MM-DD) をシードに 4 スロットから SLOTS_PER_DAY 個を
    deterministic にサンプルし、昇順で返す。"""
    digest = hashlib.sha256(jst_date_iso.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    rng = random.Random(seed)
    indices = list(range(len(POST_SLOTS_UTC)))
    rng.shuffle(indices)
    return sorted(indices[:SLOTS_PER_DAY])


# ============================================================
# メイン
# ============================================================

def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry" in args
    force = "--force" in args
    # 後方互換: 旧 CLI(window 分の数値引数) が渡されても受け流す
    _ignored = [a for a in args if a not in ("--dry", "--force") and a.isdigit()]

    if not force:
        now_utc = datetime.now(timezone.utc)
        result = current_slot_index(now_utc)
        if result is None:
            log.warning(
                "No past slot found for current time (UTC=%s). Exiting.",
                now_utc.isoformat(),
            )
            return 0
        slot_idx, slot_scheduled = result
        # スロットのスケジュール時刻からJST日付を計算(cron遅延でUTC日付が
        # ずれてもスロット本来のJST日付で評価する)
        jst_slot_date = slot_scheduled.astimezone(JST).date().isoformat()
        selected = selected_slots_for_jst_date(jst_slot_date)
        log.info(
            "Slot %d (scheduled UTC=%s, JST date=%s), selected slots for day=%s",
            slot_idx, slot_scheduled.isoformat(), jst_slot_date, selected,
        )
        if slot_idx not in selected:
            log.info("Slot %d not selected for JST %s. Exiting without posting.", slot_idx, jst_slot_date)
            return 0
        # Bot 判定回避用の 0〜POST_JITTER_MAX_MIN 分ジッター
        jitter_min = random.randint(0, POST_JITTER_MAX_MIN)
        log.info(
            "Slot %d selected. Sleeping %d min jitter before posting.",
            slot_idx, jitter_min,
        )
        time.sleep(jitter_min * 60)
    else:
        log.info("--force given: skipping slot selection.")

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
