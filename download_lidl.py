import hashlib
import json
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
MAIN_URL = "https://www.lidl.co.uk/c/online-leaflets/s10023175?ar=10"
PAGE_TIMEOUT = 30000


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def extract_title_from_pdf_slug(pdf_url):
    pdf_name = Path(pdf_url).name

    match = re.match(
        r"\d{2}-\d{2}-\d{2}-\d{2}-(.+?)(?:-\d+)?\.pdf$",
        pdf_name,
        re.IGNORECASE,
    )

    if match:
        slug = match.group(1)
    else:
        slug = Path(pdf_name).stem

    return slug.replace("-", " ").title().replace(" ", "-")


def is_weekly_leaflet(leaflet_url, pdf_url):
    leaflet_slug = Path(leaflet_url.split("/ar/")[0]).name.lower()
    pdf_slug = Path(pdf_url).stem.lower()

    return "weekly" in leaflet_slug or "weekly" in pdf_slug


def normalize_filename(leaflet_url, pdf_url, current_year):
    pdf_name = Path(pdf_url).name

    date_match = re.match(
        r"(?P<sd>\d{2})-(?P<sm>\d{2})-(?P<ed>\d{2})-(?P<em>\d{2})-(?P<slug>.+?)(?:-\d+)?\.pdf$",
        pdf_name,
        re.IGNORECASE,
    )

    if date_match:
        start_day = date_match.group("sd")
        start_month = date_match.group("sm")
        end_day = date_match.group("ed")
        end_month = date_match.group("em")

        start_year = current_year
        end_year = current_year

        if int(end_month) < int(start_month):
            end_year = current_year + 1

        title = extract_title_from_pdf_slug(pdf_url)

        return (
            f"{start_year}-{start_month}-{start_day}_"
            f"{end_year}-{end_month}-{end_day}-{title}.pdf"
        )

    return pdf_name


def folder_period_from_filename(filename):
    match = re.match(
        r"(\d{4})-(\d{2})-(\d{2})_(\d{4})-(\d{2})-(\d{2})",
        filename,
    )

    if not match:
        return None

    start_year, start_month, start_day, end_year, end_month, end_day = match.groups()

    def month_name(month):
        names = {
            "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
            "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
            "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
        }
        return names.get(month, month)

    return f"{month_name(start_month)}{int(start_day):d}-{month_name(end_month)}{int(end_day):d}"


def year_week_from_filename(filename):
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})_\d{4}-\d{2}-\d{2}", filename)

    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        date = datetime(year, month, day)
        return date.isocalendar()[:2]

    date = datetime.now()
    return date.isocalendar()[:2]


def sanitize_folder_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def destination_folder(output_dir, filename, leaflet_url, pdf_url):
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir

    if is_weekly_leaflet(leaflet_url, pdf_url):
        year, week = year_week_from_filename(filename)
        period = folder_period_from_filename(filename)

        if period:
            return output_dir / str(year) / f"Week {week:02d} - {period}"

    title = extract_title_from_pdf_slug(pdf_url)
    return output_dir / "Specials" / sanitize_folder_name(title)


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def existing_hashes(folder):
    hashes = set()

    if not folder.exists():
        return hashes

    for path in folder.rglob("*.pdf"):
        if path.is_file():
            hashes.add(sha256_file(path))

    return hashes


def send_pushover(config, title, message):
    pushover = config.get("pushover", {})
    if not pushover.get("enabled", False):
        return

    token = pushover.get("token", "").strip()
    user = pushover.get("user", "").strip()

    if not token or not user:
        print("Pushover enabled but token/user missing")
        return

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": token,
                "user": user,
                "title": title,
                "message": message,
                "priority": "0",
            },
            timeout=30,
        )

        response.raise_for_status()

        if response.json().get("status") == 1:
            print("Pushover notification sent")
        else:
            print(f"Pushover rejected the notification: {response.text}")

    except Exception as error:
        print(f"Pushover notification failed: {error}")


def send_ha_webhook(config, payload):
    ha = config.get("home_assistant", {})
    if not ha.get("enabled", False):
        return

    webhook_url = ha.get("webhook_url", "").strip()

    if not webhook_url:
        print("Home Assistant webhook enabled but URL missing")
        return

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=30,
        )

        response.raise_for_status()
        print("Home Assistant webhook sent")

    except Exception as error:
        print(f"Home Assistant webhook failed: {error}")


def viewer_url_from_leaflet(leaflet_url):
    base = re.sub(r"/ar/\d+$", "", leaflet_url)
    return f"{base}/view/flyer/page/1"


def notify_new_leaflet(filename, destination, leaflet_url, config):
    viewer_url = viewer_url_from_leaflet(leaflet_url)

    send_pushover(config, "Lidl leaflet", f"New leaflet: {viewer_url}")

    send_ha_webhook(config, {
        "type": "new_leaflet",
        "filename": filename,
        "local_path": str(destination),
        "viewer_url": viewer_url,
    })


def notify_error(config, message):
    send_pushover(config, "Lidl leaflet failed", message)

    send_ha_webhook(config, {
        "type": "error",
        "message": message,
    })


def save_version(temp_path, filename, leaflet_url, pdf_url, config):
    output_dir = Path(config["output_dir"])
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir

    folder = destination_folder(output_dir, filename, leaflet_url, pdf_url)
    folder.mkdir(parents=True, exist_ok=True)

    versions_dir = folder / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)

    destination = folder / filename

    size = temp_path.stat().st_size

    if size < 10000:
        print(f"Downloaded file is unexpectedly small: {filename}")
        return False

    with temp_path.open("rb") as file:
        if file.read(4) != b"%PDF":
            print(f"Downloaded file is not a valid PDF: {filename}")
            return False

    new_hash = sha256_file(temp_path)

    if new_hash in existing_hashes(folder):
        print(f"No change for {filename} ({folder.name})")
        return False

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    version_name = f"{Path(filename).stem}-{timestamp}-{new_hash[:8]}.pdf"
    version_path = versions_dir / version_name

    shutil.copy2(temp_path, version_path)
    shutil.copy2(temp_path, destination)

    print(f"New or changed leaflet saved: {destination}")
    print(f"Version kept: {version_path}")
    print(f"SHA-256: {new_hash}")

    notify_new_leaflet(filename, destination, leaflet_url, config)

    return True


def get_leaflet_urls(page):
    flyer_links = page.locator("a.flyer").all()
    urls = []

    for link in flyer_links:
        href = link.get_attribute("href")

        if href:
            full_url = urljoin(page.url, href)

            if full_url not in urls:
                urls.append(full_url)

    return urls


def find_pdf_in_dom(page):
    """Return the first visible .pdf href found anywhere in the DOM."""
    hrefs = page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href*=".pdf"], button[onclick*=".pdf"], [data-href*=".pdf"]'))
            .map(el => el.href || el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('onclick'))
            .filter(h => h && h.includes('.pdf'))
    """)

    for href in hrefs:
        if href:
            return href

    return None


def click_menu_and_get_pdf(page):
    menu_selectors = [
        "button[aria-label='Menu']",
        "button[aria-label*='menu' i]",
        "button[aria-label='Open menu']",
        "button[title='Menu']",
        "button[title*='menu' i]",
        "[data-testid='menu-button']",
        "button svg[aria-label='Menu']",
    ]

    for selector in menu_selectors:
        button = page.locator(selector).first

        if button.count() > 0:
            try:
                if button.is_visible():
                    print(f"Clicking menu via selector: {selector}")
                    button.click()
                    page.wait_for_timeout(1000)

                    pdf_href = find_pdf_in_dom(page)

                    if pdf_href:
                        return pdf_href

                    link = page.locator("a[href*='.pdf']").first
                    if link.count() > 0 and link.is_visible():
                        return urljoin(page.url, link.get_attribute("href"))
            except Exception:
                continue

    return None


def get_pdf_url(page):
    page.wait_for_timeout(2000)

    pdf_href = find_pdf_in_dom(page)

    if pdf_href:
        return urljoin(page.url, pdf_href)

    link = page.locator("a[href*='.pdf']").first
    if link.count() > 0 and link.is_visible():
        return urljoin(page.url, link.get_attribute("href"))

    print("PDF not visible, trying Menu...")
    pdf_url = click_menu_and_get_pdf(page)

    if pdf_url:
        return pdf_url

    return None


def download_pdf_with_requests(pdf_url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
    }

    response = requests.get(pdf_url, headers=headers, timeout=120)
    response.raise_for_status()

    temp_path = BASE_DIR / "temp_download.pdf"

    with temp_path.open("wb") as file:
        file.write(response.content)

    return temp_path


def download_leaflet(page, leaflet_url, config):
    try:
        print(f"\nProcessing leaflet: {leaflet_url}")

        page.goto(leaflet_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)

        pdf_url = get_pdf_url(page)

        if not pdf_url:
            print(f"Could not find PDF URL for {leaflet_url}")
            debug_dir = BASE_DIR / "debug"
            debug_dir.mkdir(exist_ok=True)
            debug_file = debug_dir / f"debug-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
            page_text = page.evaluate("() => document.body.innerText")[:5000]
            with debug_file.open("w", encoding="utf-8") as f:
                f.write(f"URL: {leaflet_url}\n\nVISIBLE TEXT:\n{page_text}\n")
            print(f"Debug info saved to {debug_file}")
            return False

        print(f"Found PDF: {pdf_url}")

        filename = normalize_filename(leaflet_url, pdf_url, datetime.now().year)

        temp_path = download_pdf_with_requests(pdf_url)
        result = save_version(temp_path, filename, leaflet_url, pdf_url, config)
        temp_path.unlink(missing_ok=True)

        return result

    except PlaywrightTimeoutError as error:
        print(f"Timed out while processing leaflet: {error}")
        return False

    except Exception as error:
        print(f"Error processing leaflet: {error}")
        return False


def run(config):
    with sync_playwright() as playwright:
        browser_kwargs = {
            "headless": config.get("headless", True),
        }

        browser = playwright.chromium.launch(**browser_kwargs)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        print(f"Opening: {MAIN_URL}")
        page.goto(MAIN_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)

        # Reject cookies if present
        try:
            if page.is_visible("#onetrust-reject-all-handler"):
                page.click("#onetrust-reject-all-handler")
                print("Rejected cookies.")
                page.wait_for_timeout(500)
        except Exception:
            pass

        leaflet_urls = get_leaflet_urls(page)

        print(f"Found {len(leaflet_urls)} leaflet(s).")
        for url in leaflet_urls:
            print(f"  {url}")

        if not leaflet_urls:
            raise RuntimeError("No leaflet links were found.")

        for leaflet_url in leaflet_urls:
            download_leaflet(page, leaflet_url, config)

        browser.close()


def main():
    config = load_config()

    try:
        run(config)
    except Exception:
        error_text = traceback.format_exc()
        print(error_text)
        notify_error(config, error_text)
        sys.exit(1)


if __name__ == "__main__":
    main()
