# x-autopost

Gemini で日本語ツイートを生成し、X (Twitter) API v2 で1日2回ランダム時刻に投稿する
GitHub Actions ベースの自動投稿システム。

- ジャンル比: **テック 4 / 金融 3 / 自然科学 3**(HN含む複数RSS)
- 1日 **2投稿**、JST のゴールデンタイム(朝/昼/夕/夜の4スロットから2つ)にランダム配置
- 48時間以内の記事しか拾わない(週遅れ防止)
- SQLite で重複ブロック
- 月コスト目安: **$0.6 ≒ ¥90**(X API ¥90 + Gemini ¥3)

---

## 構成

```
x-autopost/
├── auto_post.py            # メイン
├── requirements.txt
├── .github/workflows/
│   └── post.yml            # 1日2回起動 (UTC 13:00 / 07:00)
├── posts.db                # 自動生成(投稿履歴・重複防止)
└── README.md               # このファイル
```

---

## セットアップ手順(初回のみ・所要15分)

### 1. GitHub に private リポジトリを作る

1. github.com の「+ → New repository」
2. リポ名は何でもOK(例: `x-autopost`)
3. **Private** に設定して Create
4. 自分の PC でこのフォルダ全部を push

```bash
cd x-autopost
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin git@github.com:<your-user>/x-autopost.git
git push -u origin main
```

### 2. X (Twitter) Developer 登録 + キー取得

1. https://developer.x.com/ で Sign in
2. 「Sign up for Free Account」(2026年現在は新規はPay-Per-Useのみ)
3. **App を作成** → Project に追加
4. アプリの「Keys and tokens」画面で 4 つ取得:
   - **API Key**
   - **API Key Secret**
   - **Access Token**
   - **Access Token Secret**
5. App Settings → User authentication settings で
   **Read and Write** 権限を有効化(Read だけだと投稿できない)
6. 「Billing」 → クレジットを **$5** 程度チャージしておく
   (1投稿 $0.01 なので500投稿分。8ヶ月持つ)

### 3. Gemini API キーを発行

1. https://aistudio.google.com/apikey を開く
2. 「Create API key」 → コピー(`AIza...` から始まる文字列)

### 4. GitHub Secrets に5個登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**
で以下を1個ずつ追加:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | 手順3で取得したキー |
| `X_API_KEY` | 手順2の API Key |
| `X_API_SECRET` | 手順2の API Key Secret |
| `X_ACCESS_TOKEN` | 手順2の Access Token |
| `X_ACCESS_SECRET` | 手順2の Access Token Secret |

### 5. ドライランでテスト(投稿しないテスト)

GitHub の **Actions タブ → AutoPost → Run workflow** で:
- `dry`: **true**
- `window`: **0**

を入れて Run。ログに「Generated tweet …」と出れば成功。
キーのスペル間違いやpermission不足はここで全部発覚します。

### 6. 本番テスト投稿

同じく Run workflow で:
- `dry`: **false**
- `window`: **0**

を入れて Run。X タイムラインに日本語ツイートが1本付けば完了。
以降は cron で **JST 22:00〜02:00** と **JST 16:00〜20:00** の各帯にランダムで投稿されます。

---

## 投稿フォーマット(Gemini 出力例)

```
日銀、円安160円突破で手詰まり
-> 口先介入だけでこのラインを守るのはそろそろ厳しいと思います。
#為替 #日銀 https://example.com/yen
```

3行構成:
1. 元タイトル(日本語訳 or 90字以内に圧縮)
2. `->` で始まる短い独自視点 (問いかけ / 予測 / 逆張り / 日本視点 のいずれか)
3. 日本語ハッシュタグ2個 + URL

---

## 運用

### 投稿頻度を変えたい場合

`auto_post.py` の `GENRE_WEIGHTS` を編集すれば比率変更可。
`.github/workflows/post.yml` の `cron` を増減すれば日別投稿数も変えられる。

### ジャンルやソースを足す場合

`auto_post.py` の `FEEDS` 辞書に RSS URL を追記するだけ。

### 動作確認

- **GitHub Actions タブ** で run 履歴・ログが見られる
- **posts.db** がアーティファクトとして30日間保存される(投稿履歴の確認用)

### 一時停止したい

リポジトリの **Settings → Actions → General → Disable Actions** でOK。
再開も同画面で。

---

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `403 Forbidden` from X | App permission が Read のみ。Read and Write に変更し、Access Token を再発行 |
| `429 Too Many Requests` | Pay-Per-Use残高切れ。Developer Console でチャージ |
| ツイートが空 | RSS が全部 48h より古い or 全部投稿済み。`MAX_AGE_HOURS` を一時的に 168 等に |
| Gemini が英語を返す | プロンプト改ざん or モデル不調。`PROMPT_TEMPLATE` の「日本語で書きます」指示を確認 |

---

## コスト管理

- X API は **Pay-Per-Use $0.01/post** なので、月60投稿で **$0.60**
- Gemini Flash は1投稿あたり数銭。月で **約$0.02**
- GitHub Actions は public/private 問わず無料枠 2,000分/月で十分(本処理は1回1〜5分)
- `Settings → Billing & plans` でアラートを設定しておくと安心
