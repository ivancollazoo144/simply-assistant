"""
Fix/create the 4 pending uniform invoices:
  1. José Hernández   → search 'Jose' (no accent)
  2. Adrián Berrios   → search 'Adrian' (no accent)
  3. Amaia Acevedo    → search 'Amahia' (enrolled as Acevedo Casado Amahia Zahelis)
  4. Yeslian Montañez → search 'Yeslian' (might not be enrolled; skip gracefully)

IMPORTANT: The invoice created for 'Amaia Acevedo' in the previous run is WRONG
(it went to Rivera Rivera Amaia V — a duplicate).
This script creates the CORRECT invoice for Amahia Acevedo.
After confirming it's correct, manually delete the wrong Rivera duplicate in CollegeOne.
"""
import os, re, unicodedata
import requests
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CREATE_URL  = "https://suite.collegeone.net/invoice/family/create"
INVOICE_URL = "https://suite.collegeone.net/invoice"
PROFILE_DIR = ROOT / "data" / "browser_profile"
SCREENSHOTS = ROOT / "data" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = 1240110678

XP_YEAR      = '//*[@id="school_year_id"]'
XP_SEMESTER  = '//*[@id="semester_id"]'
XP_ACCOUNT   = '//*[@id="formCRUD"]/div[4]/div[1]/div/span[1]/span[1]/span'
XP_STUDENT   = '//*[@id="formCRUD"]/div[5]/div[1]/div/span[1]/span[1]/span'
XP_PAYTERM   = '//*[@id="select2-payment_term_id-container"]'
XP_ITEM_STU  = '//*[@id="formItem"]/div/div[1]/div/div/span[1]/span[1]/span'
XP_ITEM_NAME = '//*[@id="formItem"]/div/div[3]/div/div/span[1]/span[1]/span'
XP_ITEM_QTY  = '//*[@id="i_qty"]'
XP_ADD_ITEM  = '//*[@id="btnAddItem"]'
XP_SEARCH    = '/html/body/span/span/span[1]/input'

# (student display name, search term, [(type_kw, size_kw, qty)])
PENDING = [
    ("José Hernández",         "Jose",    [("Polo", "Regular", 2), ("Tshirt", "Regular", 1)]),
    ("Adrián A. Berrios García","Adrian",  [("Polo", "Regular", 4)]),
    ("Amaia Acevedo (Amahia)", "Amahia",  [("Polo", "Regular", 1), ("Tshirt", "Regular", 4)]),
    ("Yeslian M Montañez Cruz","Yeslian", [("Polo", "Regular", 2), ("Tshirt", "Regular", 2)]),
]


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        print(f"Telegram: {'OK' if r.ok else r.text}", flush=True)
    except Exception as e:
        print(f"Telegram err: {e}", flush=True)


def s2_search_pick(page, xp_trigger, search_text, pick_text=None):
    el = page.locator(f'xpath={xp_trigger}')
    el.scroll_into_view_if_needed()
    el.click()
    page.wait_for_timeout(600)
    si = page.locator(f'xpath={XP_SEARCH}')
    si.wait_for(timeout=5000)
    si.fill(search_text)
    page.wait_for_timeout(900)
    if pick_text:
        opt = page.locator(f'.select2-results__option:has-text("{pick_text}")').first
    else:
        opt = page.locator('.select2-results__option:not(.select2-results__option--loading)').first
    opt.wait_for(timeout=6000)
    opt.click()
    page.wait_for_timeout(500)


def s2_open_pick(page, xp_trigger, student_pick=False):
    el = page.locator(f'xpath={xp_trigger}')
    el.scroll_into_view_if_needed()
    el.click()
    page.wait_for_timeout(1000)
    if student_pick:
        opt = page.locator('[id^="select2-students-result-"]').first
    else:
        opt = page.locator('.select2-results__option:not([aria-disabled="true"]):not(.select2-results__option--loading)').first
    opt.wait_for(timeout=8000)
    opt.click()
    page.wait_for_timeout(500)


def create_invoice(page, display_name, search_term, items):
    print(f"\n--- {display_name} (search: '{search_term}') ---", flush=True)

    page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)

    page.locator(f'xpath={XP_YEAR}').select_option(label="2025-2026", timeout=5000)
    page.wait_for_timeout(400)
    page.locator(f'xpath={XP_SEMESTER}').select_option(label="Semester 2", timeout=5000)
    page.wait_for_timeout(400)

    s2_search_pick(page, XP_ACCOUNT, search_term, pick_text="[Parent]")
    print(f"  ✓ Account (Parent) selected", flush=True)
    page.wait_for_timeout(800)

    s2_open_pick(page, XP_STUDENT, student_pick=True)
    print(f"  ✓ Student selected", flush=True)

    s2_search_pick(page, XP_PAYTERM, "5", pick_text="5")
    print(f"  ✓ Payment term: 5 Days", flush=True)

    for type_kw, size_kw, qty in items:
        print(f"  Adding: {type_kw} {size_kw} ×{qty}", flush=True)

        item_stu_el = page.locator(f'xpath={XP_ITEM_STU}')
        item_stu_el.scroll_into_view_if_needed()
        item_stu_el.click()
        page.wait_for_timeout(1200)
        opt = page.locator('.select2-results__option[id^="select2-i_student_id-result-"]').first
        opt.wait_for(timeout=8000)
        opt.click()
        page.wait_for_timeout(500)

        page.locator(f'xpath={XP_ITEM_NAME}').scroll_into_view_if_needed()
        page.locator(f'xpath={XP_ITEM_NAME}').click()
        page.wait_for_timeout(1000)
        items_ul = page.locator('xpath=/html/body/span/span/span[2]/ul')
        items_ul.wait_for(timeout=6000)
        li_items = items_ul.locator('li').all()
        picked = False
        for li in li_items:
            try:
                txt = li.inner_text().strip()
                if not txt or txt == "Apply All":
                    continue
                txt_up = txt.upper()
                if type_kw.upper() in txt_up and size_kw.upper() in txt_up:
                    li.click()
                    picked = True
                    print(f"    ✓ Picked '{txt}'", flush=True)
                    break
                else:
                    print(f"    skip '{txt}'", flush=True)
            except Exception:
                pass
        if not picked:
            print(f"    ⚠ {type_kw} {size_kw} not found — pressing Escape", flush=True)
            page.screenshot(path=str(SCREENSHOTS / f"fix_item_{display_name[:10]}_{type_kw}.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            continue

        page.wait_for_timeout(500)
        qty_field = page.locator(f'xpath={XP_ITEM_QTY}')
        qty_field.click(click_count=3)
        qty_field.fill(str(qty))
        page.wait_for_timeout(300)

        page.locator(f'xpath={XP_ADD_ITEM}').click()
        page.wait_for_timeout(1200)

    page.screenshot(path=str(SCREENSHOTS / f"fix_before_save_{display_name[:15]}.png"))

    saved = False
    for btn_sel in ['button:has-text("Save and Close")', 'button:has-text("Save")']:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(2500)
                if "create" not in page.url:
                    print(f"  ✓ Saved — {page.url}", flush=True)
                    saved = True
                else:
                    print(f"  ⚠ Still on create page", flush=True)
                    page.screenshot(path=str(SCREENSHOTS / f"fix_save_issue_{display_name[:15]}.png"))
                break
        except Exception:
            pass

    return saved


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False, slow_mo=80,
        viewport={"width": 1440, "height": 900},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto(INVOICE_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    if "/signin" in page.url:
        page.fill("#mobile_email", os.getenv("COLLEGEONE_USER", ""))
        page.fill("#password", os.getenv("COLLEGEONE_PASS", ""))
        page.locator('button:has-text("Login")').first.click()
        page.wait_for_function("() => !location.pathname.includes('signin')", timeout=20000)

    created, errors = [], []
    for display_name, search_term, items in PENDING:
        try:
            ok = create_invoice(page, display_name, search_term, items)
            if ok:
                created.append(display_name)
            else:
                errors.append(display_name)
        except Exception as e:
            print(f"  ✗ Error on {display_name}: {e}", flush=True)
            page.screenshot(path=str(SCREENSHOTS / f"fix_error_{display_name[:15]}.png"))
            errors.append(display_name)

    msg = (
        f"✅ Fix uniforms completado.\n"
        f"Creadas: {', '.join(created) if created else 'ninguna'}\n"
        f"Errores: {', '.join(errors) if errors else 'ninguno'}\n\n"
        f"⚠️ Recuerda borrar manualmente la factura duplicada de Rivera Rivera Amaia V "
        f"(fue creada por error en la corrida anterior al buscar 'Amaia Acevedo')."
    )
    print(f"\n{msg}", flush=True)
    send_telegram(msg)
    page.wait_for_timeout(15000)
    ctx.close()
