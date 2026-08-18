"""
CollegeOne — /invoice
- Set show = 100
- Collect all "Past Due" row IDs upfront
- For each row: go back to invoice list → 3-dots → Edit → Due Date = 2026-07-31 → Save and Close
"""
import os, sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

INVOICE_URL = "https://suite.collegeone.net/invoice"
USERNAME    = os.getenv("COLLEGEONE_USER", "")
PASSWORD    = os.getenv("COLLEGEONE_PASS", "")
PROFILE_DIR = ROOT / "data" / "browser_profile"
SCREENSHOTS = ROOT / "data" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)
NEW_DUE = "2026-07-31 00:00:00"


def dismiss_modal(page):
    try:
        modal = page.locator("#otherLoginSess")
        if modal.is_visible(timeout=2000):
            btn = modal.locator('button:has-text("OK")')
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(800)
    except Exception:
        pass


def login_if_needed(page):
    if "/signin" not in page.url:
        dismiss_modal(page)
        return
    print("  → logging in...", flush=True)
    dismiss_modal(page)
    page.fill("#mobile_email", USERNAME, timeout=10000)
    page.fill("#password", PASSWORD)
    page.locator('button:has-text("Login")').first.click()
    dismiss_modal(page)
    page.wait_for_function("() => !location.pathname.includes('signin')", timeout=25000)
    print("  ✓ logged in", flush=True)


def goto_invoice_list(page):
    if "invoice" not in page.url or page.locator('span.badge:has-text("Past Due")').count() == 0:
        page.goto(INVOICE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)
        login_if_needed(page)
        # Set show = 100
        try:
            label = page.locator(
                'xpath=/html/body/div[1]/div/div[2]/div[2]/div/div[2]/div[1]/div[3]/div[1]/div[1]/label'
            )
            label.wait_for(timeout=5000)
            label.locator('select').select_option("100")
            page.wait_for_timeout(2500)
        except Exception:
            try:
                page.locator('select[name*="length"]').first.select_option("100")
                page.wait_for_timeout(2500)
            except Exception:
                pass


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        slow_mo=80,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # --- Step 1: Go to invoice list and collect all Past Due row IDs ---
    print(f"→ Opening {INVOICE_URL} ...", flush=True)
    page.goto(INVOICE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    login_if_needed(page)
    page.wait_for_timeout(1500)
    if "invoice" not in page.url:
        page.goto(INVOICE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

    # Set Show = 100
    print("→ Setting show = 100...", flush=True)
    try:
        label = page.locator(
            'xpath=/html/body/div[1]/div/div[2]/div[2]/div/div[2]/div[1]/div[3]/div[1]/div[1]/label'
        )
        label.wait_for(timeout=6000)
        label.locator('select').select_option("100")
        page.wait_for_timeout(2500)
        print("  ✓ Show = 100", flush=True)
    except Exception as e:
        print(f"  ⚠ {e}", flush=True)
        try:
            page.locator('select[name*="length"]').first.select_option("100")
            page.wait_for_timeout(2500)
            print("  ✓ Show = 100 (fallback)", flush=True)
        except Exception as e2:
            print(f"  ⚠ fallback: {e2}", flush=True)

    page.wait_for_timeout(1000)

    # Collect row IDs and names for all Past Due invoices
    print("→ Collecting Past Due invoice row IDs...", flush=True)
    row_data = []  # list of (row_id, name)
    badges = page.locator('span.badge:has-text("Past Due")').all()
    print(f"  Found {len(badges)} Past Due badges", flush=True)

    for badge in badges:
        try:
            row = badge.locator('xpath=ancestor::tr').first
            row_id = row.get_attribute('id') or ""
            name_td = row.locator('td').nth(1)
            name = name_td.inner_text(timeout=3000).strip() if name_td.count() else "?"
            if row_id:
                row_data.append((row_id, name))
                print(f"  + {name} (id={row_id})", flush=True)
        except Exception as e:
            print(f"  ⚠ collect error: {e}", flush=True)

    print(f"  Total to process: {len(row_data)}", flush=True)

    if not row_data:
        print("  No Past Due invoices found.", flush=True)
        ctx.close()
        sys.exit(0)

    # --- Step 2: Process each row ---
    updated = 0
    errors = 0

    for idx, (row_id, name) in enumerate(row_data):
        print(f"\n[{idx+1}/{len(row_data)}] {name} (id={row_id})", flush=True)

        try:
            # Make sure we're on the invoice list with the row visible
            dots = page.locator(f'xpath=//*[@id="{row_id}"]/td[10]/div/button')
            if dots.count() == 0:
                print("  Row not visible — reloading invoice list...", flush=True)
                goto_invoice_list(page)
                page.wait_for_timeout(1000)
                dots = page.locator(f'xpath=//*[@id="{row_id}"]/td[10]/div/button')
                if dots.count() == 0:
                    print("  ✗ Row still not found after reload — skipping", flush=True)
                    errors += 1
                    continue

            # Click the 3-dot button
            dots.scroll_into_view_if_needed()
            dots.click()
            page.wait_for_timeout(700)

            # Click Edit from dropdown
            edit_clicked = False
            for edit_sel in [
                '.open .dropdown-menu a:has-text("Edit")',
                '.dropdown-menu.show a:has-text("Edit")',
                'a:has-text("Edit")',
            ]:
                try:
                    el = page.locator(edit_sel).first
                    if el.is_visible(timeout=1200):
                        el.click()
                        edit_clicked = True
                        print(f"  ✓ Clicked Edit", flush=True)
                        break
                except Exception:
                    pass

            if not edit_clicked:
                print("  ✗ Edit option not found", flush=True)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                errors += 1
                continue

            # Wait for edit form to load
            page.wait_for_timeout(2000)

            # Set due date via JS (field: name="due_date", type="text")
            result = page.evaluate(f"""() => {{
                const due = document.querySelector('input[name="due_date"], input[id="due_date"]');
                if (!due) return {{found: false}};
                const old = due.value;
                due.value = '{NEW_DUE}';
                due.dispatchEvent(new Event('change', {{bubbles:true}}));
                due.dispatchEvent(new Event('input', {{bubbles:true}}));
                return {{found: true, old: old, new: due.value}};
            }}""")

            if not result.get('found'):
                print(f"  ✗ due_date field not found — screenshot", flush=True)
                page.screenshot(path=str(SCREENSHOTS / f"no_due_{idx}.png"))
                # Cancel
                for cl_sel in ['button:has-text("Cancel")', 'a:has-text("Cancel")', '.modal .close']:
                    try:
                        cl = page.locator(cl_sel).first
                        if cl.is_visible(timeout=500):
                            cl.click()
                            break
                    except Exception:
                        pass
                errors += 1
                continue

            print(f"  ✓ Due date: {result.get('old','?')} → {result.get('new','?')}", flush=True)
            page.wait_for_timeout(400)

            # Save — prefer "Save and Close" to return to list
            saved = False
            for save_sel in [
                'button:has-text("Save and Close")',
                'a:has-text("Save and Close")',
                'button:has-text("Save")',
                'button[type="submit"]',
            ]:
                try:
                    btn = page.locator(save_sel).first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        page.wait_for_timeout(2000)
                        print(f"  ✓ Saved ({save_sel})", flush=True)
                        saved = True
                        updated += 1
                        break
                except Exception:
                    pass

            if not saved:
                print("  ⚠ Save button not found", flush=True)
                page.screenshot(path=str(SCREENSHOTS / f"no_save_{idx}.png"))
                errors += 1

        except Exception as e:
            print(f"  ✗ Error: {e}", flush=True)
            page.screenshot(path=str(SCREENSHOTS / f"error_{idx}.png"))
            errors += 1

    print(f"\n✓ Done. Updated: {updated}/{len(row_data)}  Errors: {errors}", flush=True)
    print("Browser open — closing in 10s", flush=True)
    page.wait_for_timeout(10000)
    ctx.close()
