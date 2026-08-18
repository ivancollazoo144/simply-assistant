"""
Download CollegeOne invoice PDFs and extract line items using pdfplumber.
"""
import asyncio
import os
import sys
import re
import tempfile
import pdfplumber
from playwright.async_api import async_playwright

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")
BASE_URL = "https://suite.collegeone.net"

INVOICES = [
    ("293141", "Anaira Rodríguez Sierra"),
    ("293168", "Jose Hernandez Berrios"),
    ("293170", "Keriel Oquendo Rodriguez"),
    ("293901", "Kloe Gonzalez Mercado"),
    ("273698", "Sophia Roman Reyes"),
    ("294211", "Rubielys Rosado Torres"),
    ("294215", "Amaia Rivera Rivera"),
    ("294261", "Markos D Otero Ayala"),
    ("294266", "Acevedo Casado"),
    ("294307", "Anaira Rodríguez Sierra (2)"),
    ("294330", "Amira Martínez Cora"),
    ("294333", "Dakziel Camacho"),
]


def extract_items_from_pdf(path: str) -> list[str]:
    """Extract line item descriptions from a CollegeOne invoice PDF."""
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))

    # CollegeOne invoices list items like:
    # "1  MENSUALIDAD JUNIO 2025-2026  $295.00  $295.00"
    # or "MATRICULA 2026-2027  $650.00  $650.00"
    # We want to capture the item description lines
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip header/footer/total lines
        if any(kw in line.upper() for kw in [
            "FACTURA", "INVOICE", "FECHA", "DATE", "CLIENTE", "CUSTOMER",
            "DESCRIPCION", "DESCRIPTION", "CANTIDAD", "PRECIO", "TOTAL",
            "BALANCE", "AMOUNT DUE", "SUBTOTAL", "ESTUDIANTE", "STUDENT",
            "TELEFONO", "PHONE", "EMAIL", "DIRECCION", "ADDRESS",
            "GRACIAS", "THANK YOU", "SIMPLICITY",
        ]):
            continue
        # Keep lines that look like they contain a charge description
        # These usually have a $ sign (price) and some text
        if "$" in line and len(line) > 5:
            items.append(line)

    return items


async def download_and_read(inv_id: str, student: str, browser_ctx, tmp_dir: str) -> dict:
    page = await browser_ctx.new_page()
    result = {"inv_id": inv_id, "student": student, "items": [], "error": None, "raw_lines": []}

    try:
        url = f"{BASE_URL}/invoice/{inv_id}"

        # Set up download handler BEFORE navigation
        download_future = asyncio.get_event_loop().create_future()

        async def handle_download(download):
            save_path = os.path.join(tmp_dir, f"inv_{inv_id}.pdf")
            await download.save_as(save_path)
            if not download_future.done():
                download_future.set_result(save_path)

        page.on("download", handle_download)

        # Navigate — this will trigger the download
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass  # Expected: download interrupts navigation

        # Wait for the download to complete
        try:
            save_path = await asyncio.wait_for(download_future, timeout=20)

            # Extract items from PDF
            all_lines = []
            with pdfplumber.open(save_path) as pdf:
                for pg in pdf.pages:
                    text = pg.extract_text() or ""
                    all_lines.extend(text.split("\n"))

            result["raw_lines"] = all_lines
            result["items"] = extract_items_from_pdf(save_path)

        except asyncio.TimeoutError:
            result["error"] = "Download timed out"

    except Exception as e:
        result["error"] = str(e)
    finally:
        await page.close()

    return result


async def main():
    tmp_dir = tempfile.mkdtemp(prefix="co_invoices_")
    print(f"Saving PDFs to: {tmp_dir}\n")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--no-sandbox"],
            accept_downloads=True,
            downloads_path=tmp_dir,
        )

        # Process one at a time to avoid profile conflicts
        for inv_id, student in INVOICES:
            print(f"\n{'='*60}")
            print(f"Invoice {inv_id} — {student}")
            result = await download_and_read(inv_id, student, ctx, tmp_dir)

            if result["error"]:
                print(f"  ERROR: {result['error']}")
                # Print raw lines for debugging
                if result["raw_lines"]:
                    print("  Raw PDF text:")
                    for line in result["raw_lines"][:30]:
                        if line.strip():
                            print(f"    {line}")
            else:
                if result["items"]:
                    print("  ITEMS FOUND:")
                    for item in result["items"]:
                        print(f"    >> {item}")
                else:
                    print("  No items parsed. Raw text:")
                    for line in result["raw_lines"][:40]:
                        if line.strip():
                            print(f"    {line}")

            await asyncio.sleep(1)

        await ctx.close()

    print(f"\n\nPDFs saved in: {tmp_dir}")


asyncio.run(main())
