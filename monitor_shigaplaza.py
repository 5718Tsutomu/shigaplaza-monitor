# monitor_shigaplaza.py
import os, re, hashlib, time, sqlite3, requests, smtplib
from email.message import EmailMessage
from bs4 import BeautifulSoup

BASE = "https://www.shigaplaza.or.jp"
LISTS = [
    f"{BASE}/news/support/subsidy/",
    f"{BASE}/service/support/subsidy/",
    f"{BASE}/service/hojyokin-introduction/",
]
DB = "shigaplaza.db"
KEYWORDS = ["補助金", "支援金", "講座", "セミナー"]

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER = os.getenv("SMTP_SENDER")           # GitHub Secrets
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")       # GitHub Secrets
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "57180928miwa@gmail.com")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) MonitorBot/1.0"

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

def pick_articles_from_list(url):
    soup = get_soup(url)
    links = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/news/") or "/news/" in href:
            links.add(href if href.startswith("http") else BASE + href)
    return sorted(links)

def parse_detail(url):
    s = get_soup(url)
    t = s.select_one("h1")
    title = (t.get_text(strip=True) if t else (s.title.get_text(strip=True) if s.title else url))
    text = s.get_text(" ", strip=True)

    def find_date(label):
        m = re.search(label + r"\s*[:：]?\s*([0-9]{4}\.[01][0-9]\.[0-3][0-9])", text)
        return m.group(1) if m else ""

    published = find_date("公開日")
    updated   = find_date("最終更新日")
    hit = any(k in title or k in text for k in KEYWORDS)

    basis = url + "|" + (updated or published)
    item_id = sha(basis or url)

    return dict(id=item_id, url=url, title=title, published=published, updated=updated, hit=hit)

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
    msg["From"] = f"滋賀プラザ監視 <{SMTP_SENDER}>"
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(SMTP_SENDER, SMTP_PASSWORD)
        s.send_message(msg)

def main():
    init_db()
    new_count = 0
    for src in LISTS:
        try:
            for url in pick_articles_from_list(src):
                d = parse_detail(url)
                if not d["hit"]:
                    continue
                if known(d["id"]):
                    continue
                save(d, src); new_count += 1
                subject = f"【滋賀プラザ】新着/更新: {d['title']}"
                body = (
                    f"タイトル：{d['title']}\n"
                    f"公開日：{d['published'] or '—'} / 最終更新：{d['updated'] or '—'}\n"
                    f"URL：{d['url']}\n"
                    f"出所：{src}\n"
                )
                send_mail(subject, body)
                time.sleep(1)
            time.sleep(2)
        except Exception as e:
            send_mail("【滋賀プラザ】監視失敗", f"URL: {src}\nError: {e}")
    force = os.getenv("FORCE_MAIL","0")=="1"
    if new_count == 0 and force:
        send_mail("【滋賀プラザ】テスト通知", "新着0件でしたが、通知経路の確認メールです。")
    print(f"done. new={new_count}")

if __name__ == "__main__":
    main()
