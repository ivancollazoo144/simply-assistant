"""
CollegeOne — create uniform invoices from CSV.
Workflow per invoice:
  1. Create URL → school year 2025-2026, semester 2
  2. Account Name (div[4]): search by student first name, pick [Parent]
  3. Student (div[5]): loads after Account Name, pick first student
  4. Payment term = 5 Days
  5. Per item (polo then tshirt, skip if qty==0):
     a. Select student in item row (click, pick first)
     b. Item dropdown appears: pick "Y" for youth, "Regular" for regular/ladies
     c. Set qty
     d. Click Add Item
  6. Save and Close
  7. After all done → send Telegram message
"""
import csv, os, re, unicodedata
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
CSV_PATH    = Path("/Users/ivancollazo/Downloads/Pre Orden Uniformes 2026-2027 (Responses) - Form Responses 1 (1).csv")

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


def get_item(cantidad, size, kind):
    """Return ((type_keyword, size_keyword), qty) or (None, 0) to skip."""
    qty = int(cantidad or 0)
    s = (size or "").upper()
    if qty == 0 or "NONE" in s or s == "":
        return None, 0
    type_kw = "Polo" if kind == "polo" else "Tshirt"
    size_kw = "Y" if "YOUTH" in s else "Regular"
    return (type_kw, size_kw), qty


def get_jacket_item(jacket_size):
    """Return ((type_kw, size_kw), qty) for hoodie, or (None, 0) if empty."""
    s = (jacket_size or "").strip()
    if not s:
        return None, 0
    s_norm = strip_accents(s).upper()
    size_kw = "Y" if "NINOS" in s_norm else "Regular"
    return ("Hoodie", size_kw), 1


def s2_search_pick(page, xp_trigger, search_text, pick_text=None):
    """Open select2 via xp_trigger, type search_text, pick option containing pick_text (or first)."""
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


def s2_open_pick(page, xp_trigger, pick_text=None, student_pick=False):
    """Open select2 WITHOUT typing, pick option by text or first enabled."""
    el = page.locator(f'xpath={xp_trigger}')
    el.scroll_into_view_if_needed()
    el.click()
    page.wait_for_timeout(1000)
    if student_pick:
        # Header student: select2-students-result-* ; real options have IDs, placeholder has none
        opt = page.locator('[id^="select2-students-result-"]').first
    elif pick_text:
        opt = page.locator(f'.select2-results__option:has-text("{pick_text}")').first
    else:
        # Skip disabled/loading options
        opt = page.locator('.select2-results__option:not([aria-disabled="true"]):not(.select2-results__option--loading)').first
    opt.wait_for(timeout=8000)
    opt.click()
    page.wait_for_timeout(500)


def strip_accents(s):
    s = unicodedata.normalize('NFD', s)
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def norm_name(s):
    s = s.lower().strip()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^a-z0-9 ]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def wait_no_overlay(page, timeout=10000):
    """Wait until any loading overlay is gone."""
    try:
        overlay = page.locator('.loadingoverlay')
        if overlay.count() > 0 and overlay.first.is_visible(timeout=500):
            overlay.first.wait_for(state='hidden', timeout=timeout)
    except Exception:
        pass


# Override search term for students whose CollegeOne name differs from form name
SEARCH_OVERRIDE = {
    norm_name("Amaia Acevedo"):           "Amahia",       # enrolled as "Acevedo Casado Amahia Zahelis"
    norm_name("Mateo A Vargas Rivera"):   "Vargas Rivera",
    norm_name("Alvaro A. Vargas Rivera"): "Vargas Rivera",
    norm_name("Aurora I. Vargas Rivera"): "Vargas Rivera",
}


def create_invoice(page, student_csv_name, items, idx):
    """items = list of (keyword, qty). Returns True on success."""
    key = norm_name(student_csv_name)
    if key in SEARCH_OVERRIDE:
        first_name = SEARCH_OVERRIDE[key]
    else:
        # Strip accents so CollegeOne search works (e.g. José→Jose, Adrián→Adrian)
        first_name = strip_accents(student_csv_name.strip().split()[0])
    print(f"\n--- Invoice: {student_csv_name} (search: '{first_name}') ---", flush=True)

    page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    wait_no_overlay(page)

    # School year
    page.locator(f'xpath={XP_YEAR}').select_option(label="2025-2026", timeout=5000)
    page.wait_for_timeout(400)

    # Semester 2
    page.locator(f'xpath={XP_SEMESTER}').select_option(label="Semester 2", timeout=5000)
    page.wait_for_timeout(400)

    # Account Name — search by first name, pick [Parent]
    s2_search_pick(page, XP_ACCOUNT, first_name, pick_text="[Parent]")
    print(f"  ✓ Account Name (Parent) selected", flush=True)
    page.wait_for_timeout(800)

    # Student (div[5]) — loads after Account Name; pick first enabled student
    page.wait_for_timeout(600)
    s2_open_pick(page, XP_STUDENT, student_pick=True)
    print(f"  ✓ Student selected", flush=True)

    # Payment term: 5 Days
    s2_search_pick(page, XP_PAYTERM, "5", pick_text="5")
    print(f"  ✓ Payment term: 5 Days", flush=True)

    # Screenshot form state
    if idx == 0:
        page.screenshot(path=str(SCREENSHOTS / f"form_filled_{idx}.png"))

    # Add items
    for item_idx, ((type_kw, size_kw), qty) in enumerate(items):
        print(f"  Adding: {type_kw} {size_kw} qty={qty}", flush=True)
        item_keyword = (type_kw, size_kw)

        # Student in item row — click trigger, wait, then pick first enabled option
        wait_no_overlay(page)
        item_stu_el = page.locator(f'xpath={XP_ITEM_STU}')
        item_stu_el.scroll_into_view_if_needed()
        item_stu_el.click()
        page.wait_for_timeout(1500)
        if idx == 0 and item_idx == 0:
            page.screenshot(path=str(SCREENSHOTS / "diag_item_stu_dropdown.png"))
            opts = page.locator('.select2-results__option').all()
            for o in opts:
                try:
                    print(f"    opt: '{o.inner_text()}' id={o.get_attribute('id')} disabled={o.get_attribute('aria-disabled')}", flush=True)
                except Exception:
                    pass
        # Pick the student option that has an ID (placeholder "Select Student" has id=None)
        opt = page.locator('.select2-results__option[id^="select2-i_student_id-result-"]').first
        opt.wait_for(timeout=8000)
        opt.click()
        page.wait_for_timeout(500)

        # Item name — open dropdown, pick matching keyword
        type_kw, size_kw = item_keyword
        page.locator(f'xpath={XP_ITEM_NAME}').scroll_into_view_if_needed()
        page.locator(f'xpath={XP_ITEM_NAME}').click()
        page.wait_for_timeout(1000)
        # Wait for dropdown to finish loading (dismiss "Searching..." state)
        try:
            page.wait_for_function(
                "() => !document.querySelector('.select2-results__option--loading')",
                timeout=6000
            )
        except Exception:
            pass

        def _pick_item_from_ul():
            items_ul = page.locator('xpath=/html/body/span/span/span[2]/ul')
            items_ul.wait_for(timeout=6000)
            for li in items_ul.locator('li').all():
                try:
                    txt = li.inner_text().strip()
                    if not txt or txt == "Apply All":
                        continue
                    tu = txt.upper()
                    print(f"    item option: '{txt}'", flush=True)
                    if type_kw.upper() in tu and size_kw.upper() in tu:
                        li.click()
                        print(f"    ✓ Picked '{txt}'", flush=True)
                        return True
                except Exception:
                    pass
            return False

        picked = _pick_item_from_ul()

        # Hoodie items may require typing — retry with search
        if not picked and type_kw == "Hoodie":
            print(f"    → Typing 'Hoodie' to search...", flush=True)
            search_inp = page.locator(f'xpath={XP_SEARCH}')
            if search_inp.is_visible(timeout=1000):
                search_inp.fill("Hoodie")
                page.wait_for_timeout(800)
                picked = _pick_item_from_ul()

        if not picked:
            print(f"    ⚠ {type_kw} {size_kw} not found — skipping item", flush=True)
            page.screenshot(path=str(SCREENSHOTS / f"item_dropdown_{idx}_{item_idx}.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            continue
        page.wait_for_timeout(500)

        # Qty
        qty_field = page.locator(f'xpath={XP_ITEM_QTY}')
        qty_field.click(click_count=3)
        qty_field.fill(str(qty))
        page.wait_for_timeout(300)

        if idx == 0:
            page.screenshot(path=str(SCREENSHOTS / f"item_filled_{idx}_{type_kw}_{size_kw}.png"))

        # Add Item
        page.locator(f'xpath={XP_ADD_ITEM}').click()
        page.wait_for_timeout(1200)
        wait_no_overlay(page)
        print(f"    ✓ Item '{item_keyword}' ×{qty} → Add clicked", flush=True)

    # Screenshot before save
    page.screenshot(path=str(SCREENSHOTS / f"before_save_{idx}.png"))

    # Save and Close
    saved = False
    for btn_sel in [
        'button:has-text("Save and Close")',
        'button:has-text("Save")',
    ]:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(4000)
                if "create" not in page.url:
                    print(f"  ✓ Saved — {page.url}", flush=True)
                    saved = True
                else:
                    print(f"  ⚠ Still on create page after {btn_sel}", flush=True)
                    page.screenshot(path=str(SCREENSHOTS / f"save_issue_{idx}.png"))
                break
        except Exception:
            pass

    return saved


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = 1240110678


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠ No TELEGRAM_BOT_TOKEN — skipping notification", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        if r.ok:
            print(f"✓ Telegram sent", flush=True)
        else:
            print(f"⚠ Telegram error: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠ Telegram exception: {e}", flush=True)


# -- Load CSV, deduplicate, skip already-done --
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    all_rows = list(csv.DictReader(f))

# Deduplicate: keep LAST entry per normalized name
seen: dict[str, dict] = {}
for r in all_rows:
    key = norm_name(r["Nombre del estudiante:"])
    seen[key] = r  # later entries override earlier ones

# All students from the original CSV are already done
ALREADY_DONE = {norm_name(n) for n in [
    "Jose Hernandez", "Anaira Rodriguez", "Luis Cordova", "Luis Córdova",
    "Jayden J. Salgado James", "Lourianie Córdova", "lourianie Córdova",
    "Karolina Vargas", "Alondra De Jesus", "Mikaela Cintron",
    "Valentina Molini", "Edwin Y Rolon Suarez", "Chris yavet navarro declet",
    "Yariel S. Aviles Rivera", "Vanelope Rodriguez", "Vanelope Rodríguez",
    "Izaet Jared Perez Otero", "Izaet Jared Pérez Otero",
    "Amaia Rivera", "Zahara Crespo Cardona", "Suleiny Santiago",
    "Adrian A. Berrios Garcia", "Adrián A. Berrios García",
    "Shadeiliz Pantoja", "Rubielys Rosado",
    "Markos David Otero Ayala", "Markos D. Otero Ayala",
    "Tiffany C Hernandez Arroyo", "Tiffany C Hernández Arroyo",
    "Ynniel M. Rodriguez Rivera", "Mikael Rodriguez Rivera",
    "Yeslian M Montanez Cruz", "Yeslian M Montañez Cruz",
    "Amira Martinez Cora", "Amira Martínez Cora",
    "Amaia Acevedo", "Jaileen Acevedo", "Dakziel Camacho",
    # Created in session 2 (2026-07-24)
    "Eliezer Rivera Rivera",
    "Dariel Alejandro Perez Huertas", "Dariel Alejandro Pérez Huertas",
    "LUZ RODRIGUEZ", "LUZ RODRÍGUEZ",
    "Maya Zoe Ayala Oliveras", "Maya Zoé Ayala Oliveras",
    "Yeziel Vaello",
    "Cecilia",
    "Amalia Merced",
    "Javier Carrasquillo",
    "Marcos Pascual",
    "Daniela Camacho",
    "Ian",
]}
SKIP_EXTRA: set = set()  # dedup handled by ALREADY_DONE

process = []
for key, r in seen.items():
    if key in ALREADY_DONE or key in SKIP_EXTRA:
        continue
    process.append(r)

# Preserve original CSV order
order = {norm_name(r["Nombre del estudiante:"]): i for i, r in enumerate(all_rows)}
process.sort(key=lambda r: order.get(norm_name(r["Nombre del estudiante:"]), 999))

print(f"Total CSV rows: {len(all_rows)}, Unique students: {len(seen)}", flush=True)
print(f"Already done (skipping): {len(ALREADY_DONE)}", flush=True)
print(f"To process now: {len(process)}", flush=True)
for r in process:
    polo_kw, polo_qty = get_item(r["Polo (Cantidad)"], r["Size POLO"], "polo")
    tsh_kw,  tsh_qty  = get_item(r["T-Shirt (Cantidad)"], r["Size T-Shirt"], "tshirt")
    jkt_kw,  jkt_qty  = get_jacket_item(r.get("Jackets Size", ""))
    items = [(k, q) for k, q in [(polo_kw, polo_qty), (tsh_kw, tsh_qty), (jkt_kw, jkt_qty)] if k]
    print(f"  {r['Nombre del estudiante:'].strip():<35} | {r['Realizar el pago:']:<12} → {items}", flush=True)

# -- Run --
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False, slow_mo=80,
        viewport={"width": 1440, "height": 900},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # Ensure logged in
    page.goto(INVOICE_URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    if "/signin" in page.url:
        page.fill("#mobile_email", os.getenv("COLLEGEONE_USER", ""))
        page.fill("#password", os.getenv("COLLEGEONE_PASS", ""))
        page.locator('button:has-text("Login")').first.click()
        page.wait_for_timeout(3000)
        # Dismiss "other session active" modal if it appears
        try:
            modal = page.locator('#otherLoginSess')
            if modal.is_visible(timeout=2000):
                btn = modal.locator('button').first
                btn.click(force=True)
                page.wait_for_timeout(1500)
                print("  → Modal de sesión activa descartado", flush=True)
        except Exception:
            pass
        page.wait_for_function("() => !location.pathname.includes('signin')", timeout=20000)
        print("✓ Logged in", flush=True)

    created = 0
    errors  = []
    for idx, row in enumerate(process):
        student = row["Nombre del estudiante:"].strip()
        polo_kw, polo_qty = get_item(row["Polo (Cantidad)"], row["Size POLO"], "polo")
        tsh_kw,  tsh_qty  = get_item(row["T-Shirt (Cantidad)"], row["Size T-Shirt"], "tshirt")
        jkt_kw,  jkt_qty  = get_jacket_item(row.get("Jackets Size", ""))
        items = [(k, q) for k, q in [(polo_kw, polo_qty), (tsh_kw, tsh_qty), (jkt_kw, jkt_qty)] if k]
        try:
            ok = create_invoice(page, student, items, idx)
            if ok:
                created += 1
                print(f"  [{created}/{len(process)}] ✓ {student}", flush=True)
            else:
                errors.append(student)
        except Exception as e:
            print(f"  ✗ Error on {student}: {e}", flush=True)
            page.screenshot(path=str(SCREENSHOTS / f"error_{idx}.png"))
            errors.append(student)

    total_done = 3 + created  # 3 already done + this run
    summary = (
        f"✅ Facturas de uniformes completadas.\n"
        f"Esta sesión: {created}/{len(process)} creadas.\n"
        f"Total completadas: {total_done}/30 estudiantes únicos.\n"
    )
    if errors:
        summary += f"⚠ Con error: {', '.join(errors)}"

    print(f"\n{summary}", flush=True)
    send_telegram(summary)
    page.wait_for_timeout(15000)
    ctx.close()
