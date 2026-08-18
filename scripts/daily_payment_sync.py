#!/usr/bin/env python3
"""Daily payment sync script — designed to run from cron.

What it does:
  1. Scrape CollegeOne payments report (local browser)
  2. Import new payments into simply.db
  3. Sync unsynced payments to QuickBooks
  4. Send Telegram summary

If QB tokens are stale locally, calls the remote server's /admin/payment-sync
endpoint instead so QB writes always go through valid tokens.

Usage:
  python scripts/daily_payment_sync.py              # full cycle
  python scripts/daily_payment_sync.py --dry-run    # no QB writes
  python scripts/daily_payment_sync.py --sync-only  # skip scrape, just sync
  python scripts/daily_payment_sync.py --headed     # show browser window
"""
import argparse
import os
import sys
from pathlib import Path

# Make sure we can import from backend/
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")


def send_telegram(text: str) -> None:
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram] No configurado, saltando notificación")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        print("[Telegram] Notificación enviada")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


def _try_local_qb_sync(dry_run: bool) -> dict | None:
    """Try to sync via local QB tokens. Returns result or None if tokens stale."""
    try:
        from integrations import quickbooks as qb
        if not qb.is_connected():
            return None
        # Quick token validity check
        qb.list_accounts()  # will raise if tokens are bad
        import payment_sync as ps
        return ps.sync_all(dry_run=dry_run)
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err or "401" in err or "token" in err.lower():
            return None
        raise


def _fetch_remote_tokens() -> bool:
    """Pull fresh QB tokens from the remote server and save locally.

    Returns True if tokens were fetched and saved.
    """
    import httpx, json
    from pathlib import Path
    remote_url = os.getenv("REMOTE_SERVER_URL", "https://assistant.simplicity-lc.com")
    token = os.getenv("SIMPLY_API_TOKEN", "")
    try:
        r = httpx.get(
            f"{remote_url}/qb/tokens/export",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  No se pudieron obtener tokens del remoto: HTTP {r.status_code}")
            return False
        tokens = r.json()
        if "error" in tokens:
            print(f"  Remoto no tiene tokens: {tokens['error']}")
            return False
        tokens_path = Path(__file__).parent.parent / "data" / "qb_tokens.json"
        tokens_path.write_text(json.dumps(tokens, indent=2))
        print(f"  Tokens QB sincronizados del servidor remoto")
        return True
    except Exception as e:
        print(f"  Error obteniendo tokens remotos: {e}")
        return False


def run_scrape(headed: bool = False) -> dict:
    print("→ Descargando reporte de pagos de CollegeOne...")
    try:
        from integrations.collegeone_scraper import refresh_and_import
        stats = refresh_and_import()
        new = (stats.get("payments") or {}).get("inserted", 0)
        print(f"  Pagos nuevos importados: {new}")
        return stats
    except Exception as e:
        print(f"  ✗ Scrape falló: {e}")
        return {"error": str(e)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="No escribe en QB")
    ap.add_argument("--sync-only", action="store_true", help="Salta scrape")
    ap.add_argument("--headed", action="store_true", help="Browser visible")
    args = ap.parse_args(argv)

    import_stats: dict = {}
    new_payments = 0

    # Step 1: Scrape
    if not args.sync_only:
        import_stats = run_scrape(headed=args.headed)
        new_payments = (import_stats.get("payments") or {}).get("inserted", 0)
    else:
        print("→ Modo --sync-only: saltando scrape")

    # Step 2: QB sync — try local tokens, then pull from remote server if stale
    print("→ Sincronizando a QuickBooks...")
    sync_result = _try_local_qb_sync(args.dry_run)
    if sync_result is None:
        print("  Tokens locales vencidos. Obteniendo tokens frescos del servidor remoto...")
        if _fetch_remote_tokens():
            sync_result = _try_local_qb_sync(args.dry_run)
        if sync_result is None:
            sync_result = {"error": "QB no conectado. Visita https://assistant.simplicity-lc.com/qb/connect-browser"}

    print(f"  Resultado: {sync_result}")

    # Step 3: Telegram
    lines = []
    if new_payments:
        lines.append(f"📥 {new_payments} pago(s) nuevos de CollegeOne")
    if sync_result.get("error"):
        lines.append(f"❌ *Payment Sync* error: {sync_result['error']}")
    else:
        lines.append(f"✅ *Payment Sync* completado")
        lines.append(f"• QB sincronizados: {sync_result.get('synced', 0)}")
        lines.append(f"• En cola de aprobación: {sync_result.get('queued', 0)}")
        if sync_result.get("errors"):
            lines.append(f"• Errores: {sync_result['errors']}")
    if sync_result.get("queued") or sync_result.get("errors"):
        lines.append("→ Revisa el portal para los pendientes")

    send_telegram("\n".join(lines))

    errors = sync_result.get("errors", 0)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
