"""Chart-of-accounts cleanup for Simplicity Learning Center.

Strategy: Path B (cutoff date). Add school-specific Spanish accounts to the
existing QB file; deactivate clearly-unused QBO defaults. Old data stays put
for audit/tax history but new activity (from cutoff date) uses the clean
structure.

Every proposed action goes through the approval queue — nothing auto-applies.
"""
import approvals
from integrations import quickbooks

# New school-specific accounts to create. Order: name, account_type, account_sub_type
NEW_INCOME_ACCOUNTS: list[tuple[str, str, str]] = [
    ("MENSUALIDAD",          "Income", "ServiceFeeIncome"),
    ("MATRICULA",            "Income", "ServiceFeeIncome"),
    ("Horario Extendido",    "Income", "ServiceFeeIncome"),
    ("Extendido Diario",     "Income", "ServiceFeeIncome"),
    ("Cuota de Construccion","Income", "ServiceFeeIncome"),
    ("Seguro Anual",         "Income", "ServiceFeeIncome"),
    ("Uniformes",            "Income", "SalesOfProductIncome"),
    ("Campamento Verano",    "Income", "ServiceFeeIncome"),
    ("Late Fee Income",      "Income", "OtherPrimaryIncome"),
]

NEW_EXPENSE_ACCOUNTS: list[tuple[str, str, str]] = [
    ("Cafetería",                "Expense", "SuppliesMaterials"),
    ("Transportación Escolar",   "Expense", "Travel"),
    ("Materiales Educativos",    "Expense", "SuppliesMaterials"),
    ("Compras Innecesarias",     "Expense", "Other"),
]

# Names of clearly-irrelevant QBO default accounts to deactivate.
# These are non-school expenses we'll never use. Conservative list — anything
# remotely plausible for a school (Office expenses, Insurance, etc.) is kept.
QBO_DEFAULTS_TO_DEACTIVATE: list[str] = [
    "Airfare",
    "Hotels",
    "Travel meals",
    "Taxis or shared rides",
    "Vehicle rental",
    "Entertainment",
    "Listing fees",
    "Social media",
    "Website ads",
    "Workers' compensation insurance",
    "Officers' life insurance",
    "Officers' salaries",
    "Group term life insurance",
    "Mortgage interest",
    "Property insurance",
    "Liability insurance",
    "Health insurance & accident plans",
    "Employee retirement plans",
    "Commissions & fees",
    "Continuing education",
    "Disposal & waste fees",
    "Building & land rent",  # have generic Rent
    "Bad Debt",
    "Discounts given",
    "Billable Expense Income",
    "Sales of Product Income",
    "Sales",
    "Long-term loans from shareholders",
    "Long-term business loans",
    "Mortgages",
]


def propose_chart_cleanup() -> dict:
    """Queue: (1) create new school accounts, (2) deactivate unused defaults.

    Returns counts + queue IDs. Each action is its own queue entry so Ivan can
    approve individually or bulk-approve.
    """
    existing = quickbooks.list_accounts(active_only=False)
    by_name = {a["name"].strip().lower(): a for a in existing}

    queued_create: list[int] = []
    queued_deactivate: list[int] = []
    skipped_existing: list[str] = []
    skipped_inactive: list[str] = []
    skipped_not_found: list[str] = []

    # 1. Create proposals
    for name, acct_type, sub in NEW_INCOME_ACCOUNTS + NEW_EXPENSE_ACCOUNTS:
        if name.strip().lower() in by_name:
            skipped_existing.append(name)
            continue
        qid = approvals.enqueue(
            agent="bookkeeper",
            action_type="qb.create_account",
            summary=f"Crear cuenta «{name}» ({acct_type} / {sub})",
            payload={"name": name, "account_type": acct_type, "account_sub_type": sub},
        )
        queued_create.append(qid)

    # 2. Deactivation proposals
    for name in QBO_DEFAULTS_TO_DEACTIVATE:
        match = by_name.get(name.strip().lower())
        if match is None:
            skipped_not_found.append(name)
            continue
        if not match["active"]:
            skipped_inactive.append(name)
            continue
        qid = approvals.enqueue(
            agent="bookkeeper",
            action_type="qb.deactivate_account",
            summary=f"Desactivar «{name}» (cuenta default de QBO no usada)",
            payload={"account_id": match["id"], "name": name},
        )
        queued_deactivate.append(qid)

    return {
        "queued_create": len(queued_create),
        "queued_deactivate": len(queued_deactivate),
        "skipped_already_exist": skipped_existing,
        "skipped_already_inactive": skipped_inactive,
        "skipped_not_in_chart": skipped_not_found,
        "queue_ids": queued_create + queued_deactivate,
    }
