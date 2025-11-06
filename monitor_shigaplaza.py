# monitor_shigaplaza.py
import os, re, hashlib, time, sqlite3, requests, smtplib, urllib.parse
from email.message import EmailMessage
from bs4 import BeautifulSoup
from datetime import datetime

# === 監視対象 ===
SITE_RULES = {
    "www.shigaplaza.or.jp": {
        "list_urls": [
            "https://www.shigaplaza.or.jp/news/support/subsidy/",
            "https://www.shigaplaza.or.jp/service/support/subsidy/",
            "https://www.shigaplaza.or.jp/service/hojyokin-introduction/",
        ],
        "detail_patterns": [r"/news/"],
        "exclude_patterns": [],
        "brand_new_only": False,      # ←従来どおり：更新も通知
        "index_extract": False,
        "allow_external": False,
    },
    "www.kstcci.or.jp": {
        # ★修正点：/news を“一覧ページ”として扱い、そこから個別記事URLを抽出
        "list_urls": [
            "https://www.kstcci.or.jp/news/",
        ],
        # ★個別記事URLのパターン（例：/notice/post6799, /news/post6801 など）
        "detail_patterns": [r"^/(news|notice)/post\d+/?$"],
        # ★ページネーションは一覧扱いにするので、ここでは除外不要（index_extractで処理）
        "exclude_patterns": [],
        "brand_new_only": True,       # 新規のみ通知（URL単位）
        "index_extract": True,        # 一覧から個別記事リンクを抽出
        "allow_external": False,      # 外部リンクは拾わない（kstcci内のみ）
    },
}

# === キーワード（タイトル/本文に含まれればヒット） ===
KEYWORDS = ["補助金", "支援金", "講座"]

DB = "shigaplaza.db"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER = os.getenv("SMTP_SENDER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "57180928miwa@gmail.com")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) MonitorBot/1.6"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS items(
      id TEXT PRIMARY KEY,
      url TEXT, title TEXT, published TEXT, updated TEXT, src TEXT, created_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS samples(
      host TEXT PRIMARY KEY,
      sent_at TEXT
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
    if u.startswith("mailto:") or u.startswith("tel:") or "#" in u:
        return ""
    return u

def path_of(url):
    return urllib.parse.urlparse(url).path or "/"

def any_match(patterns, path):
    return any(re.search(p, path) for p in patterns)

def pick_articles_from_list(list_url, rule):
    """
    既定：同一ホスト内で detail_patterns に合うリンクを拾う。
    kstcci は index_extract=True のため、/news（および /news/page/…）を“一覧”とみなし、
    その中の個別記事リンク（/notice/postNNNN or /news/postNNNN）だけを抽出する。
    """
    host = host_of(list_url)
    soup = get_soup(list_url)
    links = set()

    # --- kstcci の一覧抽出 ---
    if rule.get("index_extract", False) and host == "www.kstcci.or.jp":
        p = path_of(list_url)
        # /news と /news/page/N ... を一覧とみなす
        if re.match(r"^/news(/page/\d+)?/?$", p):
            for a in soup.select("a[href]"):
                u = norm_url(list_url, a.get("href"))
                if not u:
                    continue
                if not same_host(u, host):
                    continue
                path = path_of(u).lower()
                # 個別記事だけ拾う
                if any_match(rule["detail_patterns"], path):
                    links.add(u)
            picked = sorted(links)
            print(f"[DEBUG] kstcci index-extract: picked {len(picked)} links from {list_url}")
            return picked[:200]

    # --- 既定（shigaplaza 等） ---
    for a in soup.select("a[href]"):
        u = norm_url(list_url, a.get("href"))
        if not u:
            continue
        if not same_host(u, host):
            continue
        path = path_of(u).lower()
        if rule.get("exclude_patterns") and any_match(rule["exclude_patterns"], path):
            continue
        if any_match(rule["detail_patterns"], path):
            links.add(u)

    picked = sorted(links)
    print(f"[DEBUG] {host}: picked {len(picked)} links from {list_url}")
    return picked[:200]

def parse_detail(url):
    s = get_soup(url)
    t = s.select_one("h1")
    title = (t.get_text(strip=True) if t else (s.title.get_text(strip=True) if s.title else url))
    text = s.get_text(" ", strip=True)

    def find_date(label):
        m = re.search(label + r"\s*[:：]?\s*([0-9]{4}[./年][01]?\d[./月][0-3]?\d)", text)
        if m:
            raw = m.group(1).replace("年", ".").replace("月", ".").replace("日", "")
            return raw.replace("/", ".")
        return ""

    published = find_date("公開日")
    updated   = find_date("最終更新") or find_date("更新日")
    hit = any(k in title or k in text for k in KEYWORDS)
    return dict(url=url, title=title, published=published, updated=updated, hit=hit)

def make_item_id(url, updated, published, rule):
    if rule.get("brand_new_only", False):
        return sha(url)               # kstcci: URLベース（更新では再通知しない）
    basis = url + "|" + (updated or published)
    return sha(basis or url)          # shigaplaza: 更新も通知

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

def host_seeded(host: str) -> bool:
    prefix = f"https://{host}/"
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM items WHERE url LIKE ? OR src LIKE ? LIMIT 1", (prefix + "%", prefix + "%"))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def sample_sent(host: str) -> bool:
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT 1 FROM samples WHERE host=?", (host,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def mark_sample_sent(host: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO samples(host, sent_at) VALUES(?, ?)", (host, datetime.utcnow().isoformat()+"Z"))
    con.commit(); con.close()

def date_key(published: str, updated: str):
    s = (updated or published or "").replace("/", ".")
    try:
        y, m, d = [int(x) for x in s.split(".")[:3]]
        return (y, m, d)
    except Exception:
        return (0, 0, 0)

def pick_latest_matching(host: str, rule: dict):
    candidates = []
    for src in rule["list_urls"]:
        try:
            for url in pick_articles_from_list(src, rule):
                d = parse_detail(url)
                if d["hit"]:
                    candidates.append((src, d))
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] pick_latest_matching error: {host} {src} {e}")
    if not candidates:
        return None
    candidates.sort(key=lambda t: (date_key(t[1]["published"], t[1]["updated"]), t[1]["url"]), reverse=True)
    return candidates[0]

def main():
    print(f"[DEBUG] FORCE_MAIL={os.getenv('FORCE_MAIL')!r}  FORCE_SAMPLE={os.getenv('FORCE_SAMPLE')!r}")
    init_db()
    total_new = 0

    # （任意）既存の最新ヒット記事を各ホスト1通ずつテスト送信
    if os.getenv("FORCE_SAMPLE","0").lower() in ("1","true","yes"):
        for host, rule in SITE_RULES.items():
            if sample_sent(host):
                print(f"[INFO] sample already sent for {host}")
                continue
            picked = pick_latest_matching(host, rule)
            if picked is None:
                print(f"[INFO] no matching article found for sample: {host}")
                continue
            src, d = picked
            item_id = make_item_id(d["url"], d["updated"], d["published"], rule)
            save({"id": item_id, **d}, src)
            subject = f"【テスト送信（既存最新）】{d['title']}"
            body = (
                f"タイトル：{d['title']}\n"
                f"公開日：{d['published'] or '—'} / 最終更新：{d['updated'] or '—'}\n"
                f"URL：{d['url']}\n"
                f"出所：{src}\n"
                f"※これはテスト送信です（既存の中の最新1件）。今後は新着のみ通知します。"
            )
            send_mail(subject, body)
            mark_sample_sent(host)
            time.sleep(1)

    # 通常運転（新着のみ通知）
    for host, rule in SITE_RULES.items():
        is_seed = not host_seeded(host) and os.getenv("FORCE_SEED", "1") == "1"
        if is_seed:
            print(f"[INFO] First-time silent seed for {host} (register existing items WITHOUT emailing)")

        for src in rule["list_urls"]:
            try:
                for url in pick_articles_from_list(src, rule):
                    d = parse_detail(url)
                    if not d["hit"]:
                        continue
                    item_id = make_item_id(d["url"], d["updated"], d["published"], rule)
                    if known(item_id):
                        continue
                    if is_seed:
                        save({"id": item_id, **d}, src)
                        continue
                    save({"id": item_id, **d}, src)
                    total_new += 1
                    subject = f"【新着】{d['title']}"
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
                send_mail("【監視失敗】サイト取得エラー", f"HOST: {host}\nSRC: {src}\nError: {e}")

    if total_new == 0 and os.getenv("FORCE_MAIL","0") == "1":
        send_mail("【監視テスト】通知経路の確認", "新着0件でしたが、通知経路の確認メールです。")

    print(f"done. new={total_new}")
if __name__ == "__main__":
    main()
