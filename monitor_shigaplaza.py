#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滋賀県内 監視スクリプト（メール通知）

挙動:
- 各サイトのニュース一覧をクロールし、「タイトルにキーワードを含む」記事を監視
- そのサイトについてDBに既読が1件もない場合（=実質初回）は:
    → そのサイトの既存ヒットの中から「最新らしい1件だけ」メール通知
    → その他のヒットは通知せず DB に既読登録のみ
- 既読があるサイトは「新しいURLだけ」通知
- 通知メールの「出所」はURLではなくサイト名文字列
- SEED_LATEST=1 のとき:
    → DBの有無に関わらず、全サイトを「初回扱い」にして
       各サイトの最新ヒット1件だけ送信（テスト用）

既読判定は URL ベース（SQLite）。
例外時はメール送信せずログのみ。
"""

import os, re, time, hashlib, sqlite3, smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

DB = "shigaplaza.db"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER = os.getenv("SMTP_SENDER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {"User-Agent": "Mozilla/5.0 (+monitor-shiga/1.3)"}
REQ_TIMEOUT = 20

# 手動実行時に「各サイト1件ずつ既存最新も送る」かどうか（Actions から渡される）
SEED_LATEST = os.getenv("SEED_LATEST", "0") == "1"

# ========= 監視ルール =========
SITE_RULES = [
    # 既存2サイト（キーワード更新後）
    {
        "name": "滋賀県産業支援プラザ",
        "entrances": ["https://www.shigaplaza.or.jp/"],
        "keywords": ["補助金", "支援金", "助成金", "講座", "セミナー"],
    },
    {
        "name": "草津商工会議所",
        "entrances": ["https://www.kstcci.or.jp/news"],
        "keywords": ["補助金", "支援金", "助成金", "講座", "セミナー"],
    },
    # 追加5サイト
    {
        "name": "守山商工会議所",
        "entrances": ["https://moriyama-cci.or.jp/"],
        "keywords": ["補助金", "支援金", "助成金"],
    },
    {
        "name": "大津商工会議所",
        "entrances": ["https://www.otsucci.or.jp/information/subsidy"],
        "keywords": ["補助金", "支援金", "助成金"],
    },
    {
        "name": "栗東商工会議所",
        "entrances": ["https://rittosci.com/"],
        "keywords": ["補助金", "支援金", "助成金"],
    },
    {
        "name": "野洲市商工会",
        "entrances": ["https://yasu-cci.or.jp/topics"],
        "keywords": ["補助金", "支援金", "助成金"],
    },
    {
        "name": "甲賀市商工会",
        "entrances": ["http://www.koka-sci.jp/"],
        "keywords": ["補助金", "支援金", "助成金"],
    },
]

# ========= DB =========
def init_db():
    """
    - テーブルが無ければ新規作成
    - 既にある場合は、足りないカラム(source, created_at)をALTER TABLEで追加
    - 必要なインデックスを作成
    """
    con = sqlite3.connect(DB)
    cur = con.cursor()
    # 1) テーブルなければ作る（新しい定義）
    cur.execute("""
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
    con.commit()

    # 2) 既存テーブルのカラムを確認して、足りなければ追加
    cur.execute("PRAGMA table_info(items)")
    cols = [row[1] for row in cur.fetchall()]  # row[1] がカラム名

    if "source" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN source TEXT")
        con.commit()
        print("[info] DB migrated: added column 'source'")

    if "created_at" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN created_at TEXT")
        con.commit()
        print("[info] DB migrated: added column 'created_at'")

    # 3) インデックス作成
    cur.execute("CREATE INDEX IF NOT EXISTS idx_items_url ON items(url)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_items_source ON items(source)")
    con.commit()
    con.close()

def known_by_url(url: str) -> bool:
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE url=?", (url,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def site_has_any_seen(source_name: str) -> bool:
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE source=? LIMIT 1", (source_name,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def save_item(item: dict):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,datetime('now'))",
        (item["id"], item["url"], item["title"], item.get("published"),
         item.get("updated"), item["source"])
    )
    con.commit(); con.close()

# ========= HTTP / 解析 =========
def http_get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

def abs_url(base: str, href: str) -> str:
    return urljoin(base, href)

def sha(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()

def extract_title(soup) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h2 = soup.find("h2")
    return h2.get_text(strip=True) if h2 else ""

def extract_date_guess(soup) -> str | None:
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", txt)
    return m.group(1) if m else None

def collect_same_domain_links(base_url: str, html: str, limit=80) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    base_netloc = urlparse(base_url).netloc
    out = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        u = abs_url(base_url, href)
        pu = urlparse(u)
        if not pu.scheme.startswith("http"):
            continue
        if not pu.netloc.endswith(base_netloc):
            continue
        low = u.lower()
        # 検索・タグページなどは軽く除外（強すぎると取りこぼすので控えめ）
        if any(x in low for x in ["/?s=", "/search", "/tag/"]):
            continue
        out.append(u)
        if len(out) >= limit:
            break
    # 重複除去（順保持）
    seen = set(); uniq = []
    for u in out:
        if u not in seen:
            uniq.append(u); seen.add(u)
    return uniq

def parse_detail(url: str) -> dict | None:
    try:
        html = http_get(url)
    except Exception:
        return None
    soup = BeautifulSoup(html, "lxml")
    title = extract_title(soup)
    pub = extract_date_guess(soup)
    return {"url": url, "title": title or "", "published": pub, "updated": None}

def title_hit(title: str, keywords: list[str]) -> bool:
    t = title or ""
    return any(k in t for k in keywords)

# ========= メール =========
def send_mail(subject: str, body: str):
    if not (SMTP_SENDER and SMTP_PASSWORD and RECIPIENT_EMAIL):
        print("[warn] SMTP not set; skip mail")
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

def save_known(item_like: dict, source_name: str):
    save_item({
        "id": sha(item_like["url"]),
        "url": item_like["url"],
        "title": item_like.get("title") or "",
        "published": item_like.get("published"),
        "updated": item_like.get("updated"),
        "source": source_name,
    })

# ========= 監視本体 =========
def crawl_rule(rule: dict) -> int:
    """
    通常運転:
      - そのサイトに既読が1件以上ある & SEED_LATEST=0
          → キーワードヒット & 未既読URLだけ通知
      - それ以外（初回 or SEED_LATEST=1）
          → キーワードヒット候補を集め、
               * 最“新”と推定される1件だけ通知
               * 他の候補は通知せず既読登録のみ
    """
    name = rule["name"]
    entrances = rule["entrances"]
    keywords = rule["keywords"]

    already_seen_site = site_has_any_seen(name)
    force_initial = SEED_LATEST   # 手動seed用フラグ
    candidates = []               # 初回 or seed 時用
    sent = 0

    for ent in entrances:
        try:
            html = http_get(ent)
        except Exception as e:
            print(f"[warn] entrance get failed: {ent} ({e})")
            continue

        links = collect_same_domain_links(ent, html)
        for link in links:
            try:
                d = parse_detail(link)
                if not d:
                    continue
                if not title_hit(d["title"], keywords):
                    continue

                # 通常運転：既読あり & seedモードでない → 新着のみ通知
                if already_seen_site and not force_initial:
                    if known_by_url(d["url"]):
                        continue
                    subject = f"新着: {d['title'] or '(タイトル不明)'}"
                    body = (
                        f"タイトル：{d['title'] or '—'}\n"
                        f"公開日：{d['published'] or '—'}\n"
                        f"URL：{d['url']}\n"
                        f"出所：{name}\n"
                    )
                    send_mail(subject, body)
                    save_known(d, name)
                    sent += 1
                    time.sleep(1)
                else:
                    # 初回 or seedモード: とりあえず候補として保持
                    candidates.append(d)
            except Exception as e:
                print(f"[warn] detail parse failed: {link} ({e})")
                continue
        time.sleep(2)

    # 初回 or seedモード: 候補から“最新”1件だけ通知し、残りは既読登録のみ
    mode_initial = (not already_seen_site) or force_initial
    if mode_initial and candidates:
        def date_key(x):
            p = x.get("published") or ""
            pnum = re.sub(r"[^\d]", "", p)
            try:
                return int(pnum)
            except Exception:
                return -1

        candidates.sort(key=date_key, reverse=True)
        top = candidates[0]

        # seedモードのときは、たとえ既にknownでも「テスト用に一度だけ」送る
        if force_initial or not known_by_url(top["url"]):
            subject = f"（初回）最新: {top['title'] or '(タイトル不明)'}"
            body = (
                f"タイトル：{top['title'] or '—'}\n"
                f"公開日：{top['published'] or '—'}\n"
                f"URL：{top['url']}\n"
                f"出所：{name}\n"
                + ("※初回/seed実行のため、このサイトの既存ヒットは通知せず既読登録のみ行いました。\n"
                   if not already_seen_site else
                   "※seed実行のため、このサイトの既存ヒットから最新1件のみテスト送信しました。\n")
            )
            send_mail(subject, body)
            save_known(top, name)
            sent += 1

        # 残りは通知せず既読登録のみ（既にknownならスキップ）
        for d in candidates[1:]:
            if not known_by_url(d["url"]):
                save_known(d, name)

    return sent

def main():
    init_db()
    total = 0
    for rule in SITE_RULES:
        try:
            total += crawl_rule(rule)
        except Exception as e:
            print(f"[warn] crawl failed: {rule['name']} ({e})")
    print(f"done. notified={total}")

if __name__ == "__main__":
    main()
