#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滋賀県内 関連サイト 監視スクリプト（メール通知）
- 既存：滋賀県産業支援プラザ / 草津商工会議所
- 追加：守山商工会議所 / 大津商工会議所 / 栗東商工会議所 / 野洲市商工会 / 甲賀市商工会
- 変更点①：キーワードをサイト別に更新
- 変更点②：新規5サイトを追加（指定ページ起点）
- 変更点③：メール本文の「出所」はURLではなくサイト名で表記

※ 既読判定は DB（SQLite）でURLベース。既に見たURLは再通知しません（＝新着のみ通知）。
※ 例外時はメール送信しません（ログのみ）。
"""

import os, re, time, hashlib, sqlite3, smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ====== 設定 ======
DB = "shigaplaza.db"

# 送信先（ワークフローの環境変数から取得されます）
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER = os.getenv("SMTP_SENDER")          # 送信元 Gmail
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")      # 同 Gmail のアプリパスワード
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")  # 受信先

# UA / タイムアウト
HEADERS = {"User-Agent": "Mozilla/5.0 (+monitor-shigaplaza/1.0)"}
REQ_TIMEOUT = 20

# ---- 監視ルール定義 ----
#   name: 出所（メールに表示するサイト名）
#   entrances: 監視起点URL（一覧/新着のページなど）
#   keywords: ヒットワード
#   title_only: True=タイトルのみで判定 / False=タイトル＋本文で判定
SITE_RULES = [
    # 既存：滋賀県産業支援プラザ（タイトル or 本文 / 新ワードを反映）
    {
        "name": "滋賀県産業支援プラザ",
        "entrances": [
            # これまで使っていた2系統の入り口（あなたが以前指定した導線に相当）
            # 具体的なURL構造に依らず、この起点からリンクをたどって詳細記事を解析します
            "https://www.shigaplaza.or.jp/",
        ],
        "keywords": ["補助金", "支援金", "助成金", "講座", "セミナー"],
        "title_only": False,   # タイトル＋本文で判定
    },
    # 既存：草津商工会議所（タイトルのみ判定のまま / ワード追加）
    {
        "name": "草津商工会議所",
        "entrances": [
            "https://www.kstcci.or.jp/news",
        ],
        "keywords": ["補助金", "支援金", "助成金", "講座", "セミナー"],
        "title_only": True,    # タイトルのみ（ご要望どおり維持）
    },
    # 追加：守山商工会議所（新着更新情報）
    {
        "name": "守山商工会議所",
        "entrances": [
            "https://moriyama-cci.or.jp/",
        ],
        "keywords": ["補助金", "支援金", "助成金"],
        "title_only": False,
    },
    # 追加：大津商工会議所（補助金・助成金情報）
    {
        "name": "大津商工会議所",
        "entrances": [
            "https://www.otsucci.or.jp/information/subsidy",
        ],
        "keywords": ["補助金", "支援金", "助成金"],
        "title_only": False,
    },
    # 追加：栗東商工会議所（お知らせ・新着情報）
    {
        "name": "栗東商工会議所",
        "entrances": [
            "https://rittosci.com/",
        ],
        "keywords": ["補助金", "支援金", "助成金"],
        "title_only": False,
    },
    # 追加：野洲市商工会（新着情報）
    {
        "name": "野洲市商工会",
        "entrances": [
            "https://yasu-cci.or.jp/topics",
        ],
        "keywords": ["補助金", "支援金", "助成金"],
        "title_only": False,
    },
    # 追加：甲賀市商工会（ニュース＆トピックス）
    {
        "name": "甲賀市商工会",
        "entrances": [
            "http://www.koka-sci.jp/",
        ],
        "keywords": ["補助金", "支援金", "助成金"],
        "title_only": False,
    },
]

# ====== DB ======
def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            published TEXT,
            updated TEXT,
            source TEXT,
            created_at TEXT
        )
    """)
    # URL既読チェック用（過去互換も兼用）
    con.execute("CREATE INDEX IF NOT EXISTS idx_items_url ON items(url)")
    con.commit()
    con.close()

def known(item_id):
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE id=?", (item_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def known_by_url(url):
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE url=?", (url,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def save(item):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,datetime('now'))",
        (item["id"], item["url"], item["title"], item.get("published"), item.get("updated"), item["source"])
    )
    con.commit(); con.close()

# ====== HTTP / 解析 ======
def get(url):
    r = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

def abspath(base, href):
    return urljoin(base, href)

def sha(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def norm_text(s):
    if not s: return ""
    # 改行・タブ等整理
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def extract_title(soup):
    # <h1> 優先 → <title>
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    # 最後の fallback
    h2 = soup.find("h2")
    return h2.get_text(strip=True) if h2 else ""

def extract_date(soup):
    # ざっくり日付らしき文字列
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", text)
    return m.group(1) if m else None

def collect_candidate_links(base_url, html):
    soup = BeautifulSoup(html, "lxml")
    base_netloc = urlparse(base_url).netloc
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href: continue
        u = abspath(base_url, href)
        pu = urlparse(u)
        # 同一ドメインのみ / メール・電話等は除外
        if pu.scheme.startswith("http") and pu.netloc.endswith(base_netloc):
            # 明らかなナビ/ページャーなどは軽く除外
            if any(x in u.lower() for x in ["/page/", "/?s=", "/search", "/tag/"]):
                continue
            links.append(u)
    # 重複除去（順保持）
    seen = set(); uniq = []
    for u in links:
        if u not in seen:
            uniq.append(u); seen.add(u)
    # 上限（暴走防止）
    return uniq[:40]

def parse_detail(url):
    try:
        html = get(url)
    except Exception:
        return None
    soup = BeautifulSoup(html, "lxml")
    title = extract_title(soup)
    # 本文テキスト
    # よくあるmain/article/section優先で抽出 → なければ全体
    main = soup.find(["main","article","section"]) or soup.body
    body_text = norm_text(main.get_text(" ", strip=True) if main else soup.get_text(" ", strip=True))
    pub = extract_date(soup)
    return {
        "url": url,
        "title": title or "",
        "body": body_text or "",
        "published": pub,
        "updated": None,   # 不明時は None
    }

def keyword_hit(text, keywords):
    text = text or ""
    return any(k in text for k in keywords)

# ====== メール ======
def send_mail(subject: str, body: str):
    if not (SMTP_SENDER and SMTP_PASSWORD and RECIPIENT_EMAIL):
        print("[warn] SMTP env not set; skip email")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"サイト監視 <{SMTP_SENDER}>"
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(SMTP_SENDER, SMTP_PASSWORD)
        s.send_message(msg)

# ====== 監視メイン ======
def crawl_rule(rule):
    name        = rule["name"]
    entrances   = rule["entrances"]
    keywords    = rule["keywords"]
    title_only  = rule.get("title_only", False)

    sent = 0
    for ent in entrances:
        try:
            html = get(ent)
        except Exception as e:
            print(f"[warn] get entrance failed: {ent} ({e})")
            continue

        links = collect_candidate_links(ent, html)
        for link in links:
            try:
                d = parse_detail(link)
                if not d: 
                    continue

                # ヒット判定
                hay = (d["title"] or "")
                if not title_only:
                    hay += " " + (d["body"] or "")
                if not keyword_hit(hay, keywords):
                    continue

                # 既読（URL）ならスキップ（＝新着のみ通知）
                if known_by_url(d["url"]):
                    continue

                # 送信
                subject = f"新着: {d['title'] or '(タイトル不明)'}"
                body = (
                    f"タイトル：{d['title'] or '—'}\n"
                    f"公開日：{d['published'] or '—'}\n"
                    f"URL：{d['url']}\n"
                    f"出所：{name}\n"   # ← ③：URLではなくサイト名で表記
                )
                send_mail(subject, body)

                # 保存（id は URL ベース）
                item_id = sha(d["url"])
                save({
                    "id": item_id,
                    "url": d["url"],
                    "title": d["title"],
                    "published": d["published"],
                    "updated": d["updated"],
                    "source": name,
                })
                sent += 1

                time.sleep(1)
            except Exception as e:
                # サイトごとの個別失敗は握りつぶす（メールは送らない）
                print(f"[warn] detail parse failed: {link} ({e})")
                continue
        time.sleep(2)
    return sent

def main():
    init_db()
    total = 0
    for rule in SITE_RULES:
        try:
            n = crawl_rule(rule)
            total += n
        except Exception as e:
            print(f"[warn] crawl failed: {rule['name']} ({e})")
    print(f"done. notified={total}")

if __name__ == "__main__":
    main()
