"""
Fix incomplete uniform invoices — add missing Hoodie (jacket) items.

"Jackets" en el formulario = "Hoodies" en CollegeOne.

Edit workflow:
  1. /invoice → search parent last name → Enter
  2. Find the July 2026 uniform invoice row (294xxx)
  3. Click 3-dot dropdown in td[10] → click Edit (li[1]/a)
  4. Add Hoodie line item (student → item → qty → Add Item)
  5. Save and Close
"""

import os, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'backend'))
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

PROFILE_DIR = ROOT / "data" / "browser_profile"
SCREENSHOTS  = ROOT / "data" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

INVOICE_LIST_URL = "https://suite.collegeone.net/invoice"

XP_SEARCH_POPUP = '/html/body/span/span/span[1]/input'
XP_ITEM_STU  = '/html/body/div[1]/div/div[2]/div[2]/div/div[2]/div[1]/div[2]/form[2]/div/div[1]/div/div/span[1]/span[1]/span'
XP_ITEM_NAME = '/html/body/div[1]/div/div[2]/div[2]/div/div[2]/div[1]/div[2]/form[2]/div/div[3]/div/div/span[1]/span[1]/span'
XP_ITEM_QTY  = '//*[@id="i_qty"]'
XP_ADD_ITEM  = '//*[@id="btnAddItem"]'


# ── Fix manifest ──────────────────────────────────────────────────────────────
# jacket tuple: (type_kw, size_kw, qty)
#   type_kw  → must appear in CollegeOne item text (case-insensitive)
#   size_kw  → must appear in CollegeOne item text
#   "Y"      → youth sizes (Niños)
#   "Regular"→ adult sizes (Adultos)
#
# inv_amount_hint: float to pick the right invoice when same account has multiple 294xxx
#   (None = take first match)

FIXES = [
    {
        "student": "Luis Córdova",
        "search_term": "Cordova",
        "inv_hint": "Cordov",
        "inv_amount_hint": 140.0,      # #294308 $140 = 4 polos Regular
        "jacket": ("Hoodie", "Regular", 1),
        "jacket_label": "L Adultos",
    },
    {
        "student": "lourianie Córdova",
        "search_term": "Cordova",
        "inv_hint": "Cordov",
        "inv_amount_hint": 50.0,       # #294319 $50 = 1 polo + 1 tshirt Youth
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "S Niños (6-8)",
        "student_search_override": "Luis",   # lourianie no aparece en dropdown; asignar a hermano
    },
    {
        "student": "Jayden J. Salgado James",
        "search_term": "Salgado",
        "inv_hint": "James",           # account: "James Jimenez, Jocelyn"
        "inv_amount_hint": 70.0,       # #294309 $70
        "jacket": ("Hoodie", "Regular", 1),
        "jacket_label": "S Adultos",
    },
    {
        "student": "Chris yavet navarro declet",
        "search_term": "Declet",       # account: "Declet, Chrismarie"
        "inv_hint": "Declet",
        "inv_amount_hint": 110.0,      # #294316 $110
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "S Niños (6-8)",
    },
    {
        "student": "Yariel S. Aviles Rivera",
        "search_term": "Aviles",
        "inv_hint": "Aviles Estrella", # "Aviles Estrella, Desiree" #294318 $60
        "inv_amount_hint": 60.0,
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "M Niños (10-12)",
    },
    {
        "student": "Vanelope Rodríguez",
        "search_term": "Vanelope",
        "inv_hint": "",
        "inv_amount_hint": None,
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "M Niños (10-12)",
    },
    {
        "student": "Izaet Jared Pérez Otero",
        "search_term": "Pagan",        # account: "Pagan, Maria E" #294320 $200
        "inv_hint": "Pagan",
        "inv_amount_hint": 200.0,
        "jacket": ("Hoodie", "Regular", 1),
        "jacket_label": "S Adultos",
    },
    {
        "student": "Shadeiliz Pantoja",
        "search_term": "Sierra",       # account: "Sierra, Melmary" #294324 $100
        "inv_hint": "Sierra",
        "inv_amount_hint": 100.0,
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "L Niños (14-16)",
    },
    {
        "student": "Rubielys Rosado",
        "search_term": "Torres",       # account: "Torres, Raisa" #294325 $110
        "inv_hint": "Torres",
        "inv_amount_hint": 110.0,
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "M Niños (10-12)",
    },
    {
        "student": "Tiffany C Hernández Arroyo",
        "search_term": "Arroyo",
        "inv_hint": "Arroyo",
        "inv_amount_hint": 70.0,       # #294327 $70
        "jacket": ("Hoodie", "Y", 1),
        "jacket_label": "M Niños (10-12)",
    },
    {
        "student": "Yeslian M Montañez Cruz",
        "search_term": "Montanez",
        "inv_hint": "Montanez",
        "inv_amount_hint": None,
        "jacket": ("Hoodie", "Regular", 1),
        "jacket_label": "M Adultos",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_amount(s: str) -> float:
    try:
        return float(s.replace('$', '').replace(',', '').strip())
    except Exception:
        return 0.0


def search_invoices(page: Page, term: str) -> list[dict]:
    page.goto(INVOICE_LIST_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    # Clear any residual DataTable filter before typing new search
    page.fill('#search', '')
    page.keyboard.press('Enter')
    page.wait_for_timeout(1000)
    page.fill('#search', term)
    page.keyboard.press('Enter')
    page.wait_for_timeout(2500)

    rows = page.evaluate("""
        () => {
            const results = [];
            document.querySelectorAll('#dtTable tbody tr').forEach((tr, i) => {
                const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
                const editBtn = tr.querySelector('td:nth-child(10) div ul li:nth-child(1) a');
                results.push({
                    idx: i,
                    account: cells[1] || '',
                    invoice_no: cells[2] || '',
                    date: cells[3] || '',
                    total: cells[5] || '',
                    balance: cells[6] || '',
                    status: cells[7] || '',
                    has_edit: !!editBtn,
                });
            });
            return results;
        }
    """)
    return rows


def find_target_row(rows: list[dict], inv_hint: str, inv_amount_hint: float | None) -> dict | None:
    candidates = []
    for r in rows:
        if not r['invoice_no'].startswith('294'):
            continue
        if r['status'] in ('Paid', 'Cancelled'):
            continue
        if inv_hint and inv_hint.lower() not in r['account'].lower():
            continue
        candidates.append(r)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates — disambiguate by amount
    if inv_amount_hint is not None:
        closest = min(candidates, key=lambda r: abs(_parse_amount(r['total']) - inv_amount_hint))
        return closest

    # Fallback: earliest 294xxx (lowest invoice number)
    return min(candidates, key=lambda r: r['invoice_no'])


def open_edit_via_dropdown(page: Page, row_idx: int) -> bool:
    row_xp = f'//*[@id="dtTable"]/tbody/tr[{row_idx + 1}]'
    toggle_xp = f'{row_xp}/td[10]/div/button'

    # Open the 3-dot dropdown
    try:
        btn = page.locator(f'xpath={toggle_xp}').first
        btn.wait_for(timeout=3000)
        btn.click()
        page.wait_for_timeout(800)
        print(f"    ✓ Dropdown abierto", flush=True)
    except Exception:
        # Force-click the hidden Edit link directly
        print(f"    → Force-click en Edit link", flush=True)
        edit_xp = f'{row_xp}/td[10]/div/ul/li[1]/a'
        try:
            page.locator(f'xpath={edit_xp}').first.click(force=True)
            page.wait_for_timeout(1500)
            return True
        except Exception as e:
            print(f"    ✗ Force-click falló: {e}", flush=True)
            return False

    # Click Edit (first li)
    edit_xp = f'{row_xp}/td[10]/div/ul/li[1]/a'
    try:
        el = page.locator(f'xpath={edit_xp}').first
        el.wait_for(state='visible', timeout=3000)
        el.click()
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        print(f"    ✗ Click Edit falló: {e}", flush=True)
        page.screenshot(path=str(SCREENSHOTS / "edit_dropdown_fail.png"))
        return False


def add_item(page: Page, type_kw: str, size_kw: str, qty: int, label: str,
             student_name: str = "") -> bool:
    print(f"    Agregando Hoodie '{label}' x{qty}...", flush=True)

    # 1. Click student trigger and search for the student
    stu = page.locator(f'xpath={XP_ITEM_STU}')
    stu.scroll_into_view_if_needed()
    stu.click()
    page.wait_for_timeout(1000)

    # Use CSS selector — more reliable than absolute XPath for Select2 search input
    stu_search = page.locator('.select2-search__field').last
    stu_search.wait_for(state='visible', timeout=5000)

    # Build search attempts: strip accents, try first name, try last name
    def _strip(s: str) -> str:
        import unicodedata as ud
        return ''.join(c for c in ud.normalize('NFD', s) if ud.category(c) != 'Mn')

    words = student_name.split()
    search_attempts = [
        _strip(words[0]),           # first name, no accents
        words[0],                   # first name with accents
        _strip(words[-1]) if len(words) > 1 else "",  # last name, no accents
    ]
    search_attempts = [s for s in search_attempts if s]

    opts = []
    for attempt in search_attempts:
        stu_search.fill('')
        stu_search.type(attempt, delay=50)
        page.wait_for_timeout(1200)
        opts = page.locator('.select2-results__option[id^="select2-i_student_id-result-"]').all()
        print(f"      búsqueda '{attempt}' → {len(opts)} opción(es)", flush=True)
        if opts:
            break

    if not opts:
        print(f"      ✗ Ninguna opción de estudiante para '{student_name}'", flush=True)
        page.keyboard.press('Escape')
        return False

    # Pick best match — first name MUST appear in the option text
    stu_words = set(_strip(student_name).lower().split())
    first_name = _strip(words[0]).lower()
    best, best_score = None, 0
    for o in opts:
        txt = o.inner_text().strip()
        txt_norm = _strip(txt).lower()
        if first_name not in txt_norm:
            print(f"      opción descartada (primer nombre ausente): '{txt}'", flush=True)
            continue
        score = sum(1 for w in stu_words if w in txt_norm)
        print(f"      opción: '{txt}' (score={score})", flush=True)
        if score > best_score:
            best, best_score = o, score

    if best is None:
        print(f"      ✗ No se encontró match válido para '{student_name}'", flush=True)
        page.keyboard.press('Escape')
        return False

    best.click()
    page.wait_for_timeout(1200)
    print(f"      ✓ Estudiante seleccionado", flush=True)

    # 2. Open item dropdown and type to search
    item_trigger = page.locator(f'xpath={XP_ITEM_NAME}')
    item_trigger.scroll_into_view_if_needed()
    item_trigger.click()
    page.wait_for_timeout(800)

    search_input = page.locator(f'xpath={XP_SEARCH_POPUP}')
    search_input.wait_for(timeout=4000)
    search_input.fill(type_kw)
    page.wait_for_timeout(1200)

    items_ul = page.locator('xpath=/html/body/span/span/span[2]/ul')
    items_ul.wait_for(timeout=6000)
    li_items = items_ul.locator('li').all()

    all_item_texts = []
    picked = False
    for li in li_items:
        try:
            txt = li.inner_text().strip()
            if not txt or txt == "Apply All":
                continue
            all_item_texts.append(txt)
            tu = txt.upper()
            if type_kw.upper() in tu and size_kw.upper() in tu:
                print(f"      ✓ Seleccionado: '{txt}'", flush=True)
                li.click()
                picked = True
                break
        except Exception:
            pass

    if not picked:
        print(f"      ⚠ No encontrado. Ítems disponibles:", flush=True)
        for t in all_item_texts:
            print(f"        '{t}'", flush=True)
        page.keyboard.press('Escape')
        page.wait_for_timeout(300)
        return False

    page.wait_for_timeout(500)
    qty_field = page.locator(f'xpath={XP_ITEM_QTY}')
    qty_field.click(click_count=3)
    qty_field.fill(str(qty))
    page.wait_for_timeout(300)

    page.locator(f'xpath={XP_ADD_ITEM}').click()
    page.wait_for_timeout(1500)
    print(f"      ✓ Ítem agregado", flush=True)
    return True


def save_invoice(page: Page) -> bool:
    for sel in ['button:has-text("Save and Close")', 'button:has-text("Save")']:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(2500)
                if "edit" not in page.url and "create" not in page.url:
                    print(f"    ✓ Guardado — {page.url}", flush=True)
                    return True
                print(f"    ⚠ Aún en página de edición: {page.url}", flush=True)
        except Exception:
            pass
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--student", help="Procesar solo este estudiante (substring del nombre)")
    ap.add_argument("--dry-run", action="store_true", help="No guardar — solo verificar selección")
    args = ap.parse_args()

    fixes = FIXES
    if args.student:
        fixes = [f for f in FIXES if args.student.lower() in f['student'].lower()]
        if not fixes:
            print(f"No se encontró estudiante con '{args.student}' en el nombre")
            return

    results = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False, slow_mo=80,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("dialog", lambda d: d.accept())

        # Login
        page.goto("https://suite.collegeone.net", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        if "/signin" in page.url:
            page.fill("#mobile_email", os.getenv("COLLEGEONE_USER", ""))
            page.fill("#password", os.getenv("COLLEGEONE_PASS", ""))
            page.locator('button:has-text("Login")').first.click()
            page.wait_for_function("() => !location.pathname.includes('signin')", timeout=20000)
            page.wait_for_timeout(1500)
            print("✓ Logged in", flush=True)

        for fix in fixes:
            print(f"\n{'='*60}", flush=True)
            print(f"Estudiante: {fix['student']} — Hoodie {fix['jacket_label']}", flush=True)

            rows = search_invoices(page, fix['search_term'])
            print(f"  Resultados: {len(rows)} filas", flush=True)
            for r in rows:
                print(f"    #{r['invoice_no']} | {r['account']:<32} | {r['status']:<12} | {r['total']}", flush=True)

            target = find_target_row(rows, fix['inv_hint'], fix.get('inv_amount_hint'))
            if not target:
                print(f"  ✗ No se encontró factura 294xxx activa", flush=True)
                results.append({"student": fix['student'], "ok": False, "note": "Factura no encontrada"})
                continue

            print(f"  → Target: #{target['invoice_no']} | {target['account']} | {target['status']} | {target['total']}", flush=True)

            if not target['has_edit']:
                print(f"  ⚠ Sin botón Edit (status={target['status']})", flush=True)
                results.append({"student": fix['student'], "ok": False, "note": f"Sin Edit (status={target['status']})"})
                continue

            clicked = open_edit_via_dropdown(page, target['idx'])
            if not clicked:
                results.append({"student": fix['student'], "ok": False, "note": "No se pudo abrir Edit"})
                continue

            page.wait_for_timeout(1000)
            print(f"  → Edit URL: {page.url}", flush=True)

            if "edit" not in page.url:
                print(f"  ✗ URL inesperada", flush=True)
                results.append({"student": fix['student'], "ok": False, "note": f"URL inesperada: {page.url}"})
                continue

            type_kw, size_kw, qty = fix['jacket']
            stu_name = fix.get('student_search_override') or fix['student']
            ok = add_item(page, type_kw, size_kw, qty, fix['jacket_label'],
                          student_name=stu_name)

            if not ok:
                results.append({
                    "student": fix['student'], "ok": False,
                    "note": f"Hoodie '{fix['jacket_label']}' no encontrado en catálogo",
                    "inv": target['invoice_no'],
                })
                continue

            if args.dry_run:
                print(f"  [DRY RUN] Ítem encontrado — NO guardando", flush=True)
                results.append({"student": fix['student'], "ok": True, "inv": target['invoice_no'], "note": "DRY RUN"})
                continue

            saved = save_invoice(page)
            results.append({
                "student": fix['student'],
                "ok": saved,
                "inv": target['invoice_no'],
                "note": "OK" if saved else "Save falló",
            })

        ctx.close()

    print(f"\n{'='*60}")
    print("RESUMEN:")
    ok_list  = [r for r in results if r['ok']]
    fail_list = [r for r in results if not r['ok']]
    print(f"  ✓ Corregidas ({len(ok_list)}): {[r['student'] for r in ok_list]}")
    print(f"  ✗ Fallidas  ({len(fail_list)}):")
    for r in fail_list:
        fix = next((f for f in FIXES if f['student'] == r['student']), {})
        print(f"    • {r['student']} → {r['note']}  [Hoodie {fix.get('jacket_label','')}]")


if __name__ == "__main__":
    main()
