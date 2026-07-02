"""CollegeOne automated scraper.

Downloads three reports (CSV) from the Reports → Finance tab:
  - Debtors Detailed Report      (id=5)   → who owes what, grouped by grade
  - Payments Received Report     (id=3)   → cash flow
  - Families Report              (id=26)  → family + student roster

Uses a persistent browser profile so the login session sticks across runs.
First run: logs in with .env credentials. Subsequent runs: reuses session.

Usage:
  python -m integrations.collegeone_scraper          # download all 3 CSVs
  python -m integrations.collegeone_scraper --headed # debug visibly
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

load_dotenv(Path(__file__).parent.parent.parent / ".env")

LOGIN_URL = os.getenv("COLLEGEONE_URL", "https://suite.collegeone.net/signin")
REPORTS_URL = "https://suite.collegeone.net/report/index"
USERNAME = os.getenv("COLLEGEONE_USER", "")
PASSWORD = os.getenv("COLLEGEONE_PASS", "")

DATA_DIR = Path(__file__).parent.parent.parent / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
PROFILE_DIR = DATA_DIR / "browser_profile"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"

REPORTS = [
    {"key": "debtors_report", "tab": "Finance", "filename": "debtors_report.csv"},
    {"key": "payments_received_report", "tab": "Finance", "filename": "payments_received_report.csv"},
    # Families Report filter is finicky (returns 0 without manual grade/group
    # selection). Skipped — the Debtors Detailed Report contains every student
    # + parent + phone + email, so we derive family data from there instead.
]


def _dismiss_other_session_modal(page: Page) -> None:
    """Dismiss the 'logged in on another device' modal if it appears."""
    try:
        modal = page.locator("#otherLoginSess")
        if modal.is_visible(timeout=2000):
            ok_btn = modal.locator('button:has-text("OK")')
            if ok_btn.is_visible(timeout=1000):
                ok_btn.click()
                page.wait_for_timeout(800)
    except Exception:
        pass


def _login_if_needed(page: Page) -> None:
    if "/signin" not in page.url:
        _dismiss_other_session_modal(page)
        return
    print("  → logging in...")
    _dismiss_other_session_modal(page)
    page.fill("#mobile_email", USERNAME, timeout=10000)
    page.fill("#password", PASSWORD)
    page.locator('button:has-text("Login")').first.click()
    # After submitting, the "other session" modal may appear before redirect
    _dismiss_other_session_modal(page)
    page.wait_for_function(
        "() => !location.pathname.includes('signin')", timeout=20000
    )


def _download_report(page: Page, key: str, tab: str, filename: str,
                     select_all_grades: bool = False,
                     select_all_groups: bool = False) -> Path:
    """Navigate to Reports → <tab>, click Generate on <key>, select CSV, download."""
    page.goto(REPORTS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    _login_if_needed(page)
    if page.url != REPORTS_URL:
        page.goto(REPORTS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

    # Switch to the right tab (Academics is default; click Finance if needed)
    if tab != "Academics":
        page.locator(f'a:has-text("{tab}"):visible').first.click()
        page.wait_for_timeout(1200)

    # Open the report modal
    page.locator(f'[data-key="{key}"]:visible').first.click()
    page.wait_for_timeout(1500)

    # If this report needs grade/group filters set (e.g. Families Report),
    # populate the underlying multi-selects with ALL options.
    if select_all_grades or select_all_groups:
        page.evaluate("""(opts) => {
            const fillAll = (selectId) => {
                const sel = document.getElementById(selectId);
                if (!sel) return;
                Array.from(sel.options).forEach(o => o.selected = true);
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                // Some pages also use Select2 — refresh it if present.
                if (window.jQuery) {
                    try { jQuery(sel).trigger('change'); } catch (e) {}
                }
            };
            if (opts.grades) fillAll('s_grades');
            if (opts.groups) fillAll('s_groups');
        }""", {"grades": select_all_grades, "groups": select_all_groups})
        page.wait_for_timeout(400)

    # Select CSV format. The Bootstrap btn-group label click doesn't reliably
    # check the underlying hidden radio, so we set it directly via JS.
    page.evaluate("""() => {
        const labels = document.querySelectorAll('label.btn-format');
        labels.forEach(l => {
            const radio = l.querySelector('input[name="format"]');
            if (!radio) return;
            if (radio.value === 'csv') {
                radio.checked = true;
                l.classList.add('active');
                radio.dispatchEvent(new Event('change', {bubbles: true}));
            } else if (radio.value === 'pdf') {
                radio.checked = false;
                l.classList.remove('active');
            }
        });
    }""")
    page.wait_for_timeout(300)

    # Submit and capture the download
    with page.expect_download(timeout=60000) as dl_info:
        page.locator('button:has-text("Generate Report"):visible').click()
    dl = dl_info.value

    out_path = DOWNLOADS_DIR / filename
    dl.save_as(str(out_path))
    return out_path


def scrape_all(headed: bool = False) -> dict:
    """Download all three reports. Returns dict of {key: Path}."""
    if not USERNAME or not PASSWORD:
        sys.exit("ERROR: set COLLEGEONE_USER and COLLEGEONE_PASS in .env")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, Path] = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not headed,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for r in REPORTS:
            print(f"→ Downloading {r['filename']}...")
            try:
                path = _download_report(
                    page, r["key"], r["tab"], r["filename"],
                    select_all_grades=r.get("select_all_grades", False),
                    select_all_groups=r.get("select_all_groups", False),
                )
                results[r["key"]] = path
                print(f"  ✓ {path} ({path.stat().st_size:,} bytes)")
            except Exception as e:
                print(f"  ✗ {r['key']} failed: {e}")
                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SCREENSHOTS_DIR / f"fail_{r['key']}.png"), full_page=True)
        ctx.close()

    return results


def refresh_and_import() -> dict:
    """Scrape + run CSV importers. Used by scheduled job and /collegeone/refresh."""
    from importers import (
        import_debtors_report,
        import_families,
        import_payments_received,
        link_payments_by_name,
    )

    paths = scrape_all(headed=False)
    stats = {"downloaded": list(paths.keys())}

    if "families_report" in paths:
        stats["families"] = import_families(paths["families_report"])
    if "debtors_report" in paths:
        stats["debtors"] = import_debtors_report(paths["debtors_report"])
    if "payments_received_report" in paths:
        stats["payments"] = import_payments_received(paths["payments_received_report"])
        # Backfill family/student links by name match — handles the CollegeOne
        # student-no mismatch (their payment CSV uses different IDs than ours).
        stats["payment_links"] = link_payments_by_name()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="Run importers after download")
    args = parser.parse_args()

    if args.do_import:
        out = refresh_and_import()
    else:
        out = scrape_all(headed=args.headed)
    print(f"\nDone. {len(out)} files downloaded.")
