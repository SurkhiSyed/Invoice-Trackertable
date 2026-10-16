"""Streamlit invoice tracker with review-first PDF extraction and Notion duplicate checks."""
import re
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

import requests
import streamlit as st
from pypdf import PdfReader

NOTION_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"


def notion_request(method, path, token, **kwargs):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    try:
        response = requests.request(method, f"{NOTION_BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Notion: {exc}") from exc
    if not response.ok:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Notion returned HTTP {response.status_code}: {detail}")
    return response.json()


def get_data_source_id(token, database_id):
    database = notion_request("GET", f"/databases/{database_id}", token)
    sources = database.get("data_sources", [])
    if not sources:
        raise RuntimeError("No data source returned. Check database ID and integration permissions.")
    return sources[0]["id"]


def get_all_invoices(token, data_source_id):
    invoices, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        result = notion_request("POST", f"/data_sources/{data_source_id}/query", token, json=payload)
        invoices.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
        if not cursor:
            raise RuntimeError("Notion pagination was incomplete.")
    return invoices


def property_text(prop):
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(item.get("plain_text", "") for item in prop.get(kind, []))
    if kind == "select":
        return (prop.get("select") or {}).get("name", "")
    if kind == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    if kind == "date":
        return (prop.get("date") or {}).get("start", "")
    if kind == "url":
        return prop.get("url") or ""
    return ""


def invoice_record(page):
    props = page.get("properties", {})
    return {
        "page_id": page.get("id", ""),
        "url": page.get("url", ""),
        "invoice_number": property_text(props.get("Invoice Number")),
        "company": property_text(props.get("Company")),
        "invoice_date": property_text(props.get("Invoice Date")),
        "amount": property_text(props.get("Amount")),
        "po_number": property_text(props.get("PO Number")),
        "status": property_text(props.get("Status")),
    }


def extract_pdf_text(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("This PDF requires a password.")
        except Exception as exc:
            raise ValueError("Please upload an unlocked PDF.") from exc
    text = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages)
    if not text.strip():
        raise ValueError("No selectable PDF text found. A scanned invoice needs OCR.")
    return text


def first_match(patterns, text):
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip(" \t:#-")
    return ""


MONEY = r"(?<![\w\d])(?:CAD|USD)?\s*\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{2})?|[0-9]+\.\d{2})(?!\d)"
# Labels are deliberately searched from strongest to weakest, independent of their location.
TOTAL_LABELS = [
    r"aggregated\s+balance\s+payable\s+due",
    r"grand\s+total\s+balance\s+due",
    r"(?:grand\s+total|invoice\s+total|total\s+amount|total\s+due|amount\s+due|balance\s+due|balance\s+payable)",
    r"(?<!sub)\btotal\b(?!\s+(?:tax|gst|hst|vat))",
]


def extract_amount(lines):
    """Use final-payable labels, never a blind CAD / $ match or a line-item value."""
    for label in TOTAL_LABELS:
        for line in reversed(lines):
            match = re.search(label, line, re.IGNORECASE)
            if not match:
                continue
            tail = line[match.end():]
            # Currency/parenthetical text is allowed between label and amount.
            values = list(re.finditer(MONEY, tail, re.IGNORECASE))
            if values:
                try:
                    return Decimal(values[-1].group(1).replace(",", ""))
                except InvalidOperation:
                    pass
    return None


def extract_company(lines):
    """Vendor/sender, NOT customer under Invoice To / Bill To."""
    legal_suffix = re.compile(r"\b(?:LLC|L\.L\.C\.|Inc\.?|Incorporated|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|Co\.)\b", re.I)
    skip = re.compile(r"^(?:invoice|commercial invoice|statement|bill|receipt|tax invoice|billing statement|page\s+\d+|date\b)", re.I)
    header = lines[:12]
    # Explicit vendor headers take precedence when available.
    for line in header:
        m = re.search(r"(?:vendor|supplier|seller|issued\s+by|from)\s*:\s*(.+)", line, re.I)
        if m:
            return re.split(r"\s{2,}|\s*[•|]\s*", m.group(1).strip())[0].strip()
    # Header only: exclude downstream bill-to/client columns by stopping at their markers.
    for line in header:
        if re.search(r"\b(?:invoice\s+to|bill\s+to|client\s+corporate\s+entity|ship\s+to)\s*:", line, re.I):
            break
        candidate = re.split(r"\s{2,}|\s*[•|]\s*", line.strip())[0].strip()
        if not candidate or skip.match(candidate):
            continue
        if legal_suffix.search(candidate):
            return candidate[:100]
    # Conservative fallback: header line with vendor email/address, never the title.
    for line in header[:5]:
        candidate = re.split(r"\s{2,}|\s*[•|]\s*", line.strip())[0].strip()
        if candidate and not skip.match(candidate) and ("@" in line or "•" in line):
            return candidate[:100]
    return ""  # Ask for review rather than silently recording customer as vendor.


def parse_date(value):
    value = value.strip().replace(",", "")
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y",
        "%m-%d-%Y", "%d-%m-%Y", "%b %d %Y", "%B %d %Y",
        "%d %b %Y", "%d %B %Y", "%m/%d/%y", "%d/%m/%y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def extract_invoice_fields(text):
    # Preserve line breaks and multi-column layout for accurate header/total identification.
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    clean = re.sub(r"[ \t]+", " ", text)
    invoice_number = first_match([
        r"\b(?:invoice\s*)?(?:track(?:ing)?\s*id|reference\s*(?:id|no\.?|number)|billing\s*code)\s*[:#\s-]*([A-Z0-9][A-Z0-9/_-]*)\b",
        r"\b(?:invoice\s*(?:number|no\.?|#|id)|inv\.?\s*(?:number|no\.?|#|id))\s*[:#\s-]*([A-Z0-9][A-Z0-9/_-]*)\b",
        r"\b(?:invoice|inv\.?)\s*[:#-]\s*([A-Z0-9][A-Z0-9/_-]*)\b",
    ], clean)
    po_number = first_match([
        r"\b(?:purchase\s*order|P\.?\s*O\.?\b)\s*(?:number|no\.?|#)?\s*[:#\s-]+([A-Z0-9][A-Z0-9/_-]*)\b"
    ], clean)
    date_value = first_match([
        r"\b(?:invoice\s*date|issue\s*date|date\s*generated|date\s*of\s*invoice)\s*:\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"\b(?:invoice\s*date|issue\s*date|date\s*generated|date\s*of\s*invoice)\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    ], clean)
    return {
        "invoice_number": invoice_number,
        "company": extract_company(lines),
        "invoice_date": parse_date(date_value) if date_value else "",
        "amount": extract_amount(lines),
        "po_number": po_number,
    }


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def amount_value(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return Decimal(str(value).replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def find_duplicates(candidate, existing):
    exact, possible = [], []
    candidate_number = normalize(candidate["invoice_number"])
    candidate_company = normalize(candidate["company"])
    candidate_po = normalize(candidate["po_number"])
    candidate_amount = amount_value(candidate["amount"])
    for old in existing:
        old_number = normalize(old["invoice_number"])
        old_company = normalize(old["company"])
        old_po = normalize(old["po_number"])
        old_amount = amount_value(old["amount"])
        # Preserve original behavior: any exact invoice number is flagged, even across vendors.
        if candidate_number and old_number == candidate_number:
            exact.append(old)
            continue
        same_company = bool(candidate_company and old_company and candidate_company == old_company)
        same_amount = candidate_amount is not None and old_amount is not None and candidate_amount == old_amount
        same_po = bool(candidate_po and old_po and candidate_po == old_po)
        if same_company and (same_amount or same_po):
            possible.append(old)
    return exact, possible


def upload_invoice_pdf(token, filename, pdf_bytes):
    """Upload an original invoice PDF to Notion."""

    if len(pdf_bytes) > 5 * 1024 * 1024:
        raise ValueError(
            "PDF exceeds 5 MiB. This upload function is "
            "configured for Notion's free workspace limit."
        )

    # Step 1: Create a file upload
    created = notion_request(
        "POST",
        "/file_uploads",
        token,
        json={
            "mode": "single_part",
            "filename": filename,
            "content_type": "application/pdf",
        },
    )

    upload_id = created["id"]

    # Step 2: Send the actual PDF bytes
    response = requests.post(
        f"{NOTION_BASE_URL}/file_uploads/{upload_id}/send",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        },
        files={
            "file": (filename, pdf_bytes, "application/pdf")
        },
        timeout=120,
    )

    if not response.ok:
        raise RuntimeError(
            f"PDF upload failed: {response.status_code} "
            f"{response.text}"
        )

    result = response.json()

    if result.get("status") != "uploaded":
        raise RuntimeError(
            f"PDF was not fully uploaded: {result}"
        )

    return upload_id


def create_invoice(
    token, data_source_id, fields, status,
    duplicate_url="", pdf_upload_id="", pdf_filename=""
):
    properties = {
        "Invoice Number": {"title": [{"text": {"content": fields["invoice_number"]}}]},
        "Company": {"rich_text": [{"text": {"content": fields["company"]}}]},
        "Status": {"select": {"name": status}},
    }
    if fields["invoice_date"]:
        properties["Invoice Date"] = {"date": {"start": fields["invoice_date"]}}
    if fields["amount"] is not None:
        properties["Amount"] = {"number": float(fields["amount"])}
    if fields["po_number"]:
        properties["PO Number"] = {"rich_text": [{"text": {"content": fields["po_number"]}}]}
    if duplicate_url:
        properties["Duplicate Of"] = {"url": duplicate_url}
    if pdf_upload_id:
        properties["Invoice PDF"] = {
            "files": [
                {
                    "name": pdf_filename,
                    "type": "file_upload",
                    "file_upload": {
                        "id": pdf_upload_id
                    },
                }
            ]
        }    
    return notion_request("POST", "/pages", token, json={
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    })


def app():
    # 1. KEEP YOUR PAGE CONFIG FIRST
    st.set_page_config(page_title="Invoice Duplicate Checker", page_icon="🧾", layout="wide")

    # 2. COMBINE ALL CSS TO HIDE FOOTER, MENU, HEADER, AND CLOUD ELEMENTS
    st.markdown("""
<style>
    /* Hide the official Streamlit header, footer, and hamburger menu */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* Hide the toolbar and deploy button on hosted cloud versions */
    [data-testid="stToolbar"] {visibility: hidden !important;}
    .stDeployButton {display: none;}

    /* Your original code to hide the cloud viewer badge */
    [class*="viewerBadge"],
    [class*="ViewerBadge"] {
        display: none !important;
        visibility: hidden !important;
    }
</style>
""", unsafe_allow_html=True)
    
    # 3. YOUR APP CONTINUES UNCHANGED FROM HERE
    st.title("🧾 Invoice Duplicate Checker")
    st.write("Upload an invoice, review the vendor and final payable total, check Notion, then save.")

    try:
        notion_token = os.environ["NOTION_TOKEN"]
        database_id = os.environ["NOTION_DATABASE_ID"]
    except Exception:
        st.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID in .streamlit/secrets.toml")
        st.stop()
    with st.sidebar:
        st.subheader("Connection")
        if st.button("Test Notion connection", use_container_width=True):
            try:
                ds_id = get_data_source_id(notion_token, database_id)
                st.success(f"Connected. Found {len(get_all_invoices(notion_token, ds_id))} invoice records.")
            except Exception as exc:
                st.error(str(exc))

    uploaded_pdf = st.file_uploader("Choose an invoice PDF", type=["pdf"])
    if not uploaded_pdf:
        return
    pdf_bytes = uploaded_pdf.getvalue()
    import hashlib
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    if st.session_state.get("active_file_hash") != file_hash:
        for key in ("checked_fields", "checked_exact", "checked_possible", "checked_data_source_id", "checked_file_hash", "save_status"):
            st.session_state.pop(key, None)
        st.session_state["active_file_hash"] = file_hash
    try:
        pdf_text = extract_pdf_text(pdf_bytes)
        extracted = extract_invoice_fields(pdf_text)
    except Exception as exc:
        st.error(str(exc))
        return
    st.subheader("1. Review extracted information")
    st.caption("Company means invoice issuer/vendor (not Bill To). Amount means final amount due, including tax.")
    with st.form("invoice_review_form"):
        left, right = st.columns(2)
        with left:
            invoice_number = st.text_input("Invoice Number", value=extracted["invoice_number"])
            company = st.text_input("Company (vendor)", value=extracted["company"])
            po_number = st.text_input("PO Number", value=extracted["po_number"])
        with right:
            invoice_date_input = st.text_input("Invoice Date (YYYY-MM-DD, optional)", value=extracted["invoice_date"])
            amount_input = st.text_input("Final payable amount (CAD)", value=str(extracted["amount"] or ""))
        check_clicked = st.form_submit_button("Check for duplicates", type="primary")
    if check_clicked:
        invoice_date = invoice_date_input.strip()
        amount = amount_value(amount_input)
        if not invoice_number.strip():
            st.error("Invoice number is required.")
        elif not company.strip():
            st.error("Vendor/company is required. Please correct the extraction.")
        elif amount is None or amount < 0:
            st.error("Enter a valid final amount. Do not use subtotal or tax alone.")
        elif invoice_date and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice_date) or not parse_date(invoice_date)):
            st.error("Invoice date must be a valid YYYY-MM-DD date, or left blank.")
        else:
            fields = {
                "invoice_number": invoice_number.strip(),
                "company": company.strip(),
                "invoice_date": invoice_date,
                "amount": amount,
                "po_number": po_number.strip(),
            }
            try:
                with st.spinner("Checking existing Notion invoices..."):
                    ds_id = get_data_source_id(notion_token, database_id)
                    records = [invoice_record(p) for p in get_all_invoices(notion_token, ds_id)]
                    exact, possible = find_duplicates(fields, records)
                st.session_state.update({
                    "checked_fields": fields,
                    "checked_exact": exact,
                    "checked_possible": possible,
                    "checked_data_source_id": ds_id,
                    "checked_file_hash": file_hash,
                })
            except Exception as exc:
                st.error(str(exc))
    if st.session_state.get("checked_file_hash") == file_hash and "checked_fields" in st.session_state:
        fields = st.session_state["checked_fields"]
        exact = st.session_state["checked_exact"]
        possible = st.session_state["checked_possible"]
        ds_id = st.session_state["checked_data_source_id"]
        st.subheader("2. Duplicate check results")
        st.caption(f'Checked: {fields["invoice_number"]} · {fields["company"]} · CAD {fields["amount"]}')
        if exact or possible:
            if exact:
                st.error(f"{len(exact)} exact invoice-number match(es). Saving is blocked.")
            else:
                st.warning(f"{len(possible)} possible duplicate(s). Review before saving.")
            st.dataframe([{
                "Match": "Exact invoice ID" if r in exact else "Possible",
                "Invoice Number": r["invoice_number"], "Company": r["company"],
                "Date": r["invoice_date"], "Amount": r["amount"],
                "PO Number": r["po_number"], "Status": r["status"],
                "Notion record": r["url"],
            } for r in exact + possible], use_container_width=True)
        else:
            st.success("No matching invoice found in the current Notion register.")
        st.subheader("3. Save reviewed invoice")
        if exact:
            st.info("Verify the existing Notion record instead of creating another row.")
        else:
            status_options = ["Potential Duplicate", "Needs Review", "New"] if possible else ["New", "Needs Review", "Potential Duplicate"]
            selected_status = st.selectbox("Status to save", status_options, key="save_status")
            duplicate_url = ""
            if possible:
                choices = {f'{r["invoice_number"]} — {r["company"]} ({r["url"]})': r["url"] for r in possible}
                selected = st.selectbox("Optional: link related existing record", ["None"] + list(choices))
                duplicate_url = choices.get(selected, "")
            confirm_save = st.checkbox("I reviewed the vendor, invoice number and final payable total.")
            if st.button("Save invoice to Notion", type="primary"):
                if not confirm_save:
                    st.warning("Confirm review before saving.")
                else:
                    try:
                        # Recheck immediately before saving in case register changed since initial check.
                        fresh = [invoice_record(p) for p in get_all_invoices(notion_token, ds_id)]
                        fresh_exact, fresh_possible = find_duplicates(fields, fresh)
                        if fresh_exact:
                            st.error("A matching invoice ID now exists. Save cancelled.")
                        elif fresh_possible and not possible:
                            st.error("A new possible duplicate appeared. Run duplicate check again before saving.")
                        else:        
                            with st.spinner("Uploading PDF and saving invoice to Notion..."):

                                pdf_upload_id = upload_invoice_pdf(
                                    notion_token,
                                    uploaded_pdf.name,
                                    pdf_bytes
                                )

                                saved = create_invoice(
                                    notion_token,
                                    ds_id,
                                    fields,
                                    selected_status,
                                    duplicate_url,
                                    pdf_upload_id=pdf_upload_id,
                                    pdf_filename=uploaded_pdf.name,
                                )
                            st.success("Invoice record created in Notion.")
                            st.link_button("Open new Notion record", saved["url"])
                            for key in ("checked_fields", "checked_exact", "checked_possible", "checked_data_source_id", "checked_file_hash"):
                                st.session_state.pop(key, None)
                    except Exception as exc:
                        st.error(str(exc))
    with st.expander("View extracted PDF text (troubleshooting)"):
        st.text_area("PDF text", pdf_text, height=250)


if __name__ == "__main__":
    app()
