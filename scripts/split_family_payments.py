"""One-shot: split family (2-child) C1 payments into 2 QB payments each.

Runs inside the Docker container:
  docker exec simply-assistant python3 scripts/split_family_payments.py
"""
import sys, json
sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/.env")

from db import connect, init_db
from integrations import quickbooks as qb

init_db()

# Map: (c1_customer_name, c1_amount_str) -> (student1_name, student2_name)
FAMILY_SPLITS = [
    ("Natal Navarro, Yamelis",         "Vimelis M Ortiz Natal",          "Sofia V Ortiz Natal"),
    ("Cuenca, Jessica",                "Patricia Ortiz Cuenca",           "Diego Y Torres Cuenca"),
    ("Cintrón Rivera, Sharon M",       "Marcos A Pascual Cintrón",        "Cecilia Isabel Pascual Cintrón"),
    ("Rivera Rosado, Winaida",         "Zoheryz N Morales Rivera",        "Naheryz Z Morales Rivera"),
    ("Casado Cordero, Susan J",        "Amahia Zahelis Acevedo Casado",   "Jaileen S Acevedo Casado"),
    ("Rivera Santiago, Coral M",       "Eliezer Rivera Rivera",           "Kristal Mirelys Rivera Rivera"),
    ("Feliciano Rodriguez, Yahaira M", "Yosuet Y Vaello Feliciano",       "Yeziel Y Vaello Feliciano"),
    ("Rivera Talavera, Melissa",       "Ynniel M Rodriguez Rivera",       "Mikael Rodriguez Rivera"),
    ("Davila Ortega, Joan",            "Luis Cordova Davila",             "Lourianie Cordova Davila"),
    ("Figueroa, Carlos",               "Taiyari Figueroa Ortiz",          "Carlos Yahir Figueroa Ortiz"),
]

import unicodedata, re

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())

# Build QB customer lookup
qb_customers = qb.list_customers()
qb_by_norm = {norm(c["name"]): c for c in qb_customers}

def find_qb_customer(name):
    n = norm(name)
    if n in qb_by_norm:
        return qb_by_norm[n]
    # fuzzy: all tokens present
    tokens = set(n.split())
    for cn, c in qb_by_norm.items():
        if tokens <= set(cn.split()):
            return c
    return None

# Get payment methods
methods = qb.list_payment_methods()
method_map = {norm(m["name"]): m["id"] for m in methods}

def method_id(co_method):
    k = norm(co_method or "")
    if "cash" in k or "efectivo" in k:      return method_map.get("cash")
    if "check" in k or "cheque" in k:       return method_map.get("check")
    if "card" in k or "credit" in k:        return method_map.get("credit card")
    if "bank" in k or "ach" in k:           return method_map.get("check")
    return None

results = []
errors = []

with connect() as conn:
    for parent_name, stud1_name, stud2_name in FAMILY_SPLITS:
        # Get all pending payments for this parent
        rows = conn.execute(
            "SELECT * FROM payments_received "
            "WHERE customer_name = ? AND qb_synced_at IS NULL",
            (parent_name,)
        ).fetchall()

        if not rows:
            print(f"  SKIP {parent_name} — no pending payments found")
            continue

        c1 = find_qb_customer(stud1_name)
        c2 = find_qb_customer(stud2_name)

        if not c1:
            print(f"  WARN {parent_name}: no QB customer for '{stud1_name}'")
        if not c2:
            print(f"  WARN {parent_name}: no QB customer for '{stud2_name}'")
        if not c1 and not c2:
            continue

        for row in rows:
            row = dict(row)
            total = float(row["amount"])
            half1 = round(total / 2, 2)
            half2 = round(total - half1, 2)
            date  = row["payment_date"]
            mid   = method_id(row["payment_method"])
            memo  = f"CO pago {row['collegeone_payment_id']} (split 2 hijos) {row['payment_method']} {date}"

            print(f"\n{parent_name} | {date} | ${total} → ${half1} + ${half2}")

            paid_ids = []
            for cust, half, sname in [(c1, half1, stud1_name), (c2, half2, stud2_name)]:
                if not cust:
                    print(f"    SKIP {sname} — sin QB customer")
                    continue
                # Find open invoice
                invs = qb.list_invoices_for_customer(cust["id"])
                if not invs:
                    print(f"    SKIP {sname} — sin invoice abierta")
                    continue
                # Pick the one closest to half amount
                inv = min(invs, key=lambda i: abs(i["balance"] - half))
                try:
                    p = qb.create_payment(
                        customer_id=cust["id"],
                        invoice_id=inv["id"],
                        amount=half,
                        txn_date=date,
                        payment_method_id=mid,
                        memo=memo,
                    )
                    paid_ids.append(p["id"])
                    print(f"    ✓ {sname} → QB#{p['id']} inv#{inv['id']} ${half}")
                except Exception as e:
                    print(f"    ✗ {sname} → ERROR: {e}")
                    errors.append(str(e))

            if paid_ids:
                # Mark as synced (store comma-separated if 2 payment IDs)
                qb_pid = ",".join(str(x) for x in paid_ids)
                conn.execute(
                    "UPDATE payments_received SET qb_synced_at=datetime('now'), qb_payment_id=? WHERE id=?",
                    (qb_pid, row["id"])
                )
                results.append({"parent": parent_name, "c1_id": row["collegeone_payment_id"],
                                 "qb_payments": paid_ids})

print(f"\n=== DONE: {len(results)} pagos procesados, {len(errors)} errores ===")
