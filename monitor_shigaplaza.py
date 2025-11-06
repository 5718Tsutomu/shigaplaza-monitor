# monitor_shigaplaza.py
import os, re, hashlib, time, sqlite3, requests, smtplib, urllib.parse
from email.message import EmailMessage
from bs4 import BeautifulSoup

# === 監視対象ドメインごとの抽出ルール ===
SITE_RULES = {
    "www.shigaplaza.or.jp": {
        "list_urls": [
            "https://www.shigaplaza.or.jp/news/support/subsidy/",
            "https://www.shigaplaza.or.jp/service/support/subsidy/",
            "https://www.shigaplaza.or.jp/service/hojyokin-introduction/",
        ],
        # 記事候補リンク（部分一致）
        "detail_patterns": [r"/news/"],
        # 更新も通知（従来どおり）
        "brand_new_only": False,
    },
    "www.kstcci.or.jp": {
        "list_urls": [
            # トップも辿って /news/ へのリンクを拾う（保険）
            "https://www.kstcci.or.jp/",
            # もしニュース一覧が /news/ で公開されていれば、ここで直接巡回
            "https://www.kstcci.or.jp/news/",
        ],
        # ★ニュース限定：/news/ のみ許可
        "detail_patterns": [r"/news/"],
        # ★新規のみ通知（更新では再通知しない）
        "brand_new_only": True,
    },
}

# === 通知判定用キーワード（タイトル/本文のどちらかに含まれれば通知） ===
# ★ご指定に合わせて絞り込み
KEYWORDS = ["補助金", "支援金", "講座"]

DB = "shigaplaza.db"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER = os.getenv("SMTP_SENDER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "57180928miwa@gmail.com")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) MonitorBot/1.2"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS items(
      id TEXT PRIMARY KEY,
      url TEXT, title TEXT, published TEXT, updated TEXT, src TEXT, created_at TEXT
    )""")
    con.commit(); con.close()

def sha(s): 
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def get_soup(url):
    r = requests.get(url, timeout=25, headers={"User-Agent": UA})
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def host_of(url):
    return urllib.parse.urlparse(url).netloc

def same_host(url, host):
    return host_of(url) == host

def norm_url(base, href):
    href = (href or "").strip()
    if not href:
        return ""
    u = urllib.parse.urljoin(base, href)
    # mailto, tel, # 等は除外
    if u.startswith("mailto:") or u.startswith("tel:") or "#" in u:
        return ""
    return u

def pick_articles_from_list(list_url, host, patterns):
    """
    一覧URLを開き、同一ホスト内で patterns のいずれかを含むリンクを記事候補として返す。
    """
    soup = get_soup(list_url)
    links = set()
    for a in soup.select("a[href]"):
        u = norm_url(list_url, a.get("href"))
        if not u:
            continue
        if not same_host(u, host):
            continue
        path = urllib.parse.urlparse(u).path.lower()
        if any(re.search(p, path) for p in patterns):
            links.add(u)
    links = sorted(links)
    print(f"[DEBUG] {host}: picked {len(links)} links from {list_url}")
    # 多すぎる場合の安全弁
    return links[:200]

def parse_detail(url):
    s = get_soup(url)
    t = s.select_one("h1")
    title = (t.get_text(strip=True) if t else (s.title.get_text(strip=True) if s.title else url))
    text = s.get_text(" ", strip=True)

    def find_date(label):
        # 例：「公開日：2024.05.01」「最終更新 2024/05/01」「2024年5月1日」等
        m = re.search(label + r"\s*[:：]?\s*([0-9]{4}[./年][01]?\d[./月][0-3]?\d)", text)
        if m:
            raw = m.group(1).replace("年", ".").replace("月", ".").replace("日", "")
            raw = raw.replace("/", ".")
            return raw
        return ""

    published = find_date("公開日")
    updated   = find_date("最終更新") or find_date("更新日")
    hit = any(k in title or k in text for k in KEYWORDS)

    return dict(url=url, title=title, published=published, updated=updated, hit=hit)

def make_item_id(url, updated, published, rule):
    """
    一意IDの作り方：
      - brand_new_only=True のサイト（kstcci）は URL のみ（更新では再通知しない）
      - それ以外は URL + (更新日 or 公開日)（更新も通知対象）
    """
    if rule.get("brand_new_only", False):
        return sha(url)
    basis = url + "|" + (updated or published)
    return sha(basis or url)

def known(item_id):
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE id=?", (item_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def save(item, src):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,datetime('now'))",
                (item["id"], item["url"], item["title"], item["published"], item["updated"], src))
    con.commit(); con.close()

def send_mail(subject: str, body: str):
    if not (SMTP_SENDER and SMTP_PASSWORD):
        print("SMTP env not set; skip email")
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

def main():
    print(f"[DEBUG] FORCE_MAIL={os.getenv('FORCE_MAIL')!r}")
    init_db()
    new_count = 0

    # 各ドメインごとに巡回
    for host, rule in SITE_RULES.items():
        for src in rule["list_urls"]:
            try:
                for url in pick_articles_from_list(src, host, rule["detail_patterns"]):
                    d = parse_detail(url)
                    if not d["hit"]:
                        continue
                    # 一意IDの生成（サイトごとの方針に従う）
                    item_id = make_item_id(d["url"], d["updated"], d["published"], rule)
                    if known(item_id):
                        continue
                    # 保存＆通知
                    save({"id": item_id, **d}, src); new_count += 1
                    subject = f"【新着】{d['title']}"
                    body = (
                        f"タイトル：{d['title']}\n"
                        f"公開日：{d['published'] or '—'} / 最終更新：{d['updated'] or '—'}\n"
                        f"URL：{d['url']}\n"
                        f"出所：{src}\n"
                    )
                    send_mail(subject, body)
                    time.sleep(1)  # 送信マナー
                time.sleep(2)      # アクセスマナー
            except Exception as e:
                send_mail("【監視失敗】サイト取得エラー", f"HOST: {host}\nSRC: {src}\nError: {e}")

    # FORCE_MAIL=1（初回など）は新着0でもテスト通知
    force = os.getenv("FORCE_MAIL", "0") == "1"
    if new_count == 0 and force:
        send_mail("【監視テスト】通知経路の確認", "新着0件でしたが、通知経路の確認メールです。")
    print(f"done. new={new_count}")

if __name__ == "__main__":
    main()
