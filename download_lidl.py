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
MAIN_URL = "https://www.lidl.co.uk/c/online-leaflets/s10023175"


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def safe_filename(filename):
    filename = filename.split("?")[0]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    return filename


def week_folder_name(filename):
    match = re.search(r"(\d{2}-\d{2}-\d{2}-\d{2})", filename)

    if match:
        return match.group(1)

    year, week, _ = datetime.now().isocalendar()
    return f"{year}-W{week:02d}"


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def existing_hashes(week_dir):
    hashes = set()

    if not week_dir.exists():
        return hashes

    for path in week_dir.rglob("*.pdf"):
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


def save_version(temp_path, filename, leaflet_url, config):
    output_dir = BASE_DIR / config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    week_dir = output_dir / week_folder_name(filename)
    versions_dir = week_dir / "versions"

    week_dir.mkdir(parents=True, exist_ok=True)
    versions_dir.mkdir(parents=True, exist_ok=True)

    destination = week_dir / filename

    size = temp_path.stat().st_size

    if size < 10000:
        print(f"Downloaded file is unexpectedly small: {filename}")
        return False

    with temp_path.open("rb") as file:
        if file.read(4) != b"%PDF":
            print(f"Downloaded file is not a valid PDF: {filename}")
            return False

    new_hash = sha256_file(temp_path)

    if new_hash in existing_hashes(week_dir):
        print(f"No change for {filename} ({week_dir.name})")
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


def get_main_page_leaflet_urls(config):
    cookies = config.get("cookies", {})
    print(f"Loaded {len(cookies)} cookie(s)")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
    }

    response = requests.get(MAIN_URL, headers=headers, cookies=cookies, timeout=60)
    response.raise_for_status()

    detail_pattern = re.compile(r"(/l/en/online-leaflets/[^\"'>\s]+)")
    detail_urls = sorted(set(detail_pattern.findall(response.text)))

    return [urljoin(MAIN_URL, path) for path in detail_urls]


def dismiss_consent(page):
    accept_selectors = [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]

    for selector in accept_selectors:
        locator = page.locator(selector)

        try:
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                print(f"Accepted cookie banner using: {selector}")
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    try:
        page.evaluate(
            """
            () => {
                const selectors = [
                    '#onetrust-consent-sdk',
                    '.onetrust-pc-dark-filter',
                    '.ot-fade-in',
                ];
                for (const selector of selectors) {
                    document.querySelectorAll(selector)
                        .forEach((element) => element.remove());
                }
                document.body.style.overflow = 'auto';
            }
            """
        )
    except Exception as error:
        print(f"Could not remove consent overlay: {error}")

    page.wait_for_timeout(500)


def find_pdf_button(page):
    selectors = [
        "a:has-text('PDF download')",
        "button:has-text('PDF download')",
        "a[href*='.pdf']",
        "a[download]",
    ]

    for selector in selectors:
        locator = page.locator(selector)

        if locator.count() > 0:
            return locator.first

    return None


def extract_pdf_url(page):
    menu_button = page.locator("button[aria-label='Menu']")

    if menu_button.count() == 0:
        menu_button = page.locator("button:has-text('Menu')")

    if menu_button.count() == 0:
        menu_button = page.get_by_role(
            "button",
            name=re.compile(r"menu", re.IGNORECASE),
        )

    if menu_button.count() == 0:
        print("Could not find the Menu button")
        return None

    try:
        menu_button.first.click(timeout=15000)
    except Exception:
        print("Menu click was blocked, forcing the click")
        dismiss_consent(page)
        menu_button.first.click(force=True, timeout=15000)

    page.wait_for_timeout(1500)

    pdf_button = find_pdf_button(page)

    if pdf_button is None:
        print("Could not find the PDF download button")
        return None

    # First try: the button might already be a link with the PDF URL
    href = pdf_button.get_attribute("href")

    if href:
        full_url = urljoin(page.url, href)

        if full_url.lower().endswith(".pdf") or ".pdf?" in full_url.lower():
            print(f"Found PDF URL from href: {full_url}")
            return full_url

    # Second try: click it and intercept the network request
    captured_url = [None]

    def handle_request(request):
        url = request.url

        if url.lower().endswith(".pdf") or ".pdf?" in url.lower():
            if captured_url[0] is None:
                captured_url[0] = url
                print(f"Intercepted PDF request: {url}")

    page.on("request", handle_request)

    try:
        print("Clicking PDF download and intercepting request")

        pdf_button.click(timeout=15000)
        page.wait_for_timeout(3000)

        if captured_url[0]:
            return captured_url[0]

        # Third try: page may have navigated directly to the PDF
        current_url = page.url

        if current_url.lower().endswith(".pdf") or ".pdf?" in current_url.lower():
            print(f"Page navigated to PDF: {current_url}")
            return current_url

    except Exception as error:
        print(f"Error clicking PDF button: {error}")

    finally:
        page.remove_listener("request", handle_request)

    print("Could not determine PDF URL")
    return None


def download_pdf_with_requests(pdf_url, config):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
    }

    response = requests.get(
        pdf_url,
        headers=headers,
        cookies=config["cookies"],
        timeout=120,
    )

    response.raise_for_status()

    temp_path = BASE_DIR / "temp_download.pdf"

    with temp_path.open("wb") as file:
        file.write(response.content)

    return temp_path


def download_leaflet(browser, leaflet_url, config):
    page = browser.new_page()

    try:
        print(f"Opening leaflet: {leaflet_url}")

        page.goto(leaflet_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)

        dismiss_consent(page)

        pdf_url = extract_pdf_url(page)

        if not pdf_url:
            print(f"Could not find PDF URL for {leaflet_url}")
            return False

        print(f"PDF URL: {pdf_url}")

        filename = safe_filename(Path(pdf_url).name)

        temp_path = download_pdf_with_requests(pdf_url, config)
        result = save_version(temp_path, filename, leaflet_url, config)
        temp_path.unlink(missing_ok=True)

        return result

    except PlaywrightTimeoutError as error:
        print(f"Timed out while processing leaflet: {error}")
        return False

    except Exception as error:
        print(f"Error processing leaflet: {error}")
        return False

    finally:
        page.close()


def run(config):
    leaflet_urls = get_main_page_leaflet_urls(config)

    print(f"Found {len(leaflet_urls)} leaflet link(s)")
    for url in leaflet_urls:
        print(f"  {url}")

    if not leaflet_urls:
        raise RuntimeError("No leaflet links were found.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.get("headless", True))

        for leaflet_url in leaflet_urls:
            download_leaflet(browser, leaflet_url, config)

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
