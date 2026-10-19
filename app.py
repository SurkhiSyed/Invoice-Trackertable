"""Streamlit invoice tracker with review-first PDF extraction and Notion duplicate checks."""

import re

from datetime import date, datetime

from decimal import Decimal, InvalidOperation

from io import BytesIO

import os



import requests

import streamlit as st

from pypdf import PdfReader

import fitz  # PyMuPDF; layout-aware reading



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

    """Layout-aware selectable text. Keep page 1 as the primary field source."""

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if doc.needs_pass:

        raise ValueError("Please upload an unlocked PDF.")

    pages = [p.get_text("text", sort=True) for p in doc]

    if not any(p.strip() for p in pages):

        raise ValueError("No selectable text found. This scanned PDF needs OCR.")

    return "\n\n--- PAGE BREAK ---\n\n".join(pages)





def first_match(patterns, text):

    for pattern in patterns:

        match = re.search(pattern, text, re.IGNORECASE)

        if match:

            return match.group(1).strip(" \t:#-\u00a0")

    return ""





MONEY = r"(?:\\$\s*)?(-?\d{1,3}(?:,\d{3})+(?:\\.\d{2})?|-?\d+\\.\d{2})(?![\d])"





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

            pass

    return ""





def extract_invoice_fields(text):

    """Conservative selection: page-one metadata; current charges, not account arrears."""

    first_page = text.split("--- PAGE BREAK ---")[0]

    lines = [re.sub(r"\s+", " ", l).strip() for l in first_page.splitlines() if l.strip()]

    header = "\n".join(lines[:55])

    whole = "\n".join(lines)



    # Invoice No/Number on a single row takes precedence over the other IDs.

    invoice_number = first_match([

        r"(?m)^\s*invoice\s*(?:track(?:ing)?\s*id|number|no\\.?|#|id)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]*)\b",

        r"(?m)^\s*invoice\s*[:#-]?\s+([A-Z0-9][A-Z0-9/_-]*\d)\b",

        r"(?m)^\s*#\s*([A-Z0-9][A-Z0-9/_-]*)\b",

        r"(?m)^\s*inv\\.?\s*(?:number|no\\.?|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]*)\b",

    ], header)

    if not invoice_number:

        invoice_number = first_match([

            r"\bInvoice\s+Track\s+ID\s*:\s*([A-Z0-9][A-Z0-9/_-]*)\b",

            r"\bInvoice\s+(?:Number|No\\.?|ID)\s*:\s*([A-Z0-9][A-Z0-9/_-]*)\b",

        ], header)

    if not invoice_number:

        # In some line-oriented PDF renderings the ID follows 'Invoice' on next line.

        invoice_number = first_match([r"(?im)^invoice\s*\n\s*#?\s*([A-Z0-9][A-Z0-9/_-]*\d)\b"], first_page)



    # Normalize vendor brand using grounded document markers and explicit legal entity.

    lwr = first_page.lower()

    if "mettler-toledo" in lwr or "mettler toledo" in lwr:

        company = "Mettler-Toledo, LLC"

    elif "ups.com" in lwr and "delivery service invoice" in lwr:

        company = "UPS"

    elif "fayettejanitorial.com" in lwr or "fayetteindustrial.com" in lwr:

        company = "Fayette"  # Visible logo brand; legal entity not stated in text.

    else:

        company = first_match([

            r"(?im)^\s*(?:vendor|supplier|seller|issued by|from)\s*:\s*([^|•\n]+)",

            r"(?im)^\s*([\w&., '-]+?\b(?:LLC|Inc\\.?|Ltd\\.?|GmbH|Corporation))\s*(?:[•|]|$)",

        ], first_page)



    # Non-PO headings (such as 'PO #' next to a date) should remain blank.

    po_number = first_match([

        r"(?im)^\s*(?:customer\s+)?(?:purchase order|p\\.?o\\.?)\s*(?:number|no\\.?|#)?\s*[:#]?\s+([A-Z0-9][A-Z0-9/_-]*\d)\s*$",

    ], whole)

    # Inline PO Number: PO-998241-A, with neighboring metadata columns.

    if not po_number:

        po_number = first_match([

            r"\bPO\s+Number\s*:\s*([A-Z0-9][A-Z0-9/_-]*\d[A-Z0-9/_-]*)\b",

            r"\bPurchase\s+Order\s+Number\s*:\s*([A-Z0-9][A-Z0-9/_-]*\d[A-Z0-9/_-]*)\b",

        ], whole)

    # 'Customer PO No.' can share a row with other data; allow this labeled form.

    if not po_number:

        po_number = first_match([r"\bCustomer\s+PO\s+No\\.?\s*[:#]?\s*(\d{5,})\b"], whole)



    date_value = first_match([

        r"\b(?:invoice date|issue date|date generated|date of invoice)\s*[:]?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",

    ], header)

    if not date_value:

        # Unlabeled dated logo/header; exclude later due dates and line-item periods.

        date_value = first_match([r"(?m)^\s*(\d{1,2}/\d{1,2}/\d{4})\s*$"], "\n".join(lines[:12]))



    # Payment obligation labels, ranked above generic total (especially UPS).

    ranked = [

        r"\bamount due this period\b",

        r"\baggregated balance payable due\b",

        r"\bgrand total balance due\b",

        r"\bbalance due\b",

        r"\b(?:invoice total|grand total|amount due|total due|balance payable)\b",

        r"\btotal\s+(?:USD|CAD|EUR)\b",

        r"(?<!\w)total(?!\s+(?:net|tax|outstanding|incentive|service|adjustment|charges|amount outstanding))\b",

    ]

    amount = None

    # Only match amounts on the SAME printed line as selected label.

    for pattern in ranked:

        for line in reversed(lines):

            m = re.search(pattern, line, re.I)

            if not m:

                continue

            tail = line[m.end():]

            # reject cross-column additional labels and pick nearest value

            money = re.search(MONEY, tail)

            if money:

                try:

                    amount = Decimal(money.group(1).replace(",", ""))

                    break

                except InvalidOperation:

                    pass

        if amount is not None:

            break



    return {

        "invoice_number": invoice_number,

        "company": company,

        "invoice_date": parse_date(date_value) if date_value else "",

        "amount": amount,

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





# PDF ATTACHMENT FEATURE DISABLED. Uncomment the following function to restore it.
# def upload_invoice_pdf(token, filename, pdf_bytes):
#
#     """Upload a PDF to Notion; return the upload ID for attachment to a page."""
#
#     if not pdf_bytes.startswith(b"%PDF-"):
#
#         raise ValueError("The selected file is not a valid PDF.")
#
#     if len(pdf_bytes) > 5 * 1024 * 1024:
#
#         raise ValueError("PDF exceeds the 5 MiB limit configured for this application.")
#
#     filename = os.path.basename(filename.replace("\\\\", "/")) or "invoice.pdf"
#
#     created = notion_request("POST", "/file_uploads", token, json={
#
#         "mode": "single_part", "filename": filename,
#
#         "content_type": "application/pdf",
#
#     })
#
#     upload_id = created["id"]
#
#     try:
#
#         response = requests.post(
#
#             f"{NOTION_BASE_URL}/file_uploads/{upload_id}/send",
#
#             headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
#
#             files={"file": (filename, pdf_bytes, "application/pdf")},
#
#             timeout=120,
#
#         )
#
#         response.raise_for_status()
#
#         result = response.json()
#
#     except requests.RequestException as exc:
#
#         raise RuntimeError(f"Notion PDF upload failed: {exc}") from exc
#
#     if result.get("status") != "uploaded":
#
#         raise RuntimeError(f"Notion did not confirm the PDF upload: {result.get('status')}")
#
#     return upload_id


def create_invoice(token, data_source_id, fields, status, duplicate_url="",

                   pdf_upload_id="", pdf_filename=""):

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

    # PDF ATTACHMENT PROPERTY DISABLED:
    # if pdf_upload_id:
    # 
    #     properties["Invoice PDF"] = {"files": [{
    # 
    #         "name": pdf_filename,
    # 
    #         "type": "file_upload",
    # 
    #         "file_upload": {"id": pdf_upload_id},
    # 
    #     }]}

    return notion_request("POST", "/pages", token, json={

        "parent": {"type": "data_source_id", "data_source_id": data_source_id},

        "properties": properties,

    })





def app():

    st.set_page_config(page_title="Invoice Duplicate Checker", page_icon="🧾", layout="wide")

    st.title("🧾 Invoice Duplicate Checker")

    st.write("Upload an invoice, review the vendor and final payable total, check Notion, then save.")

    try:

        notion_token = os.environ.get("NOTION_TOKEN") or st.secrets["NOTION_TOKEN"]

        database_id = os.environ.get("NOTION_DATABASE_ID") or st.secrets["NOTION_DATABASE_ID"]

    except Exception:

        st.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID in Render environment variables or Streamlit secrets.")

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

    st.caption("Company means invoice issuer/vendor (not Bill To). If the invoice amount is missing or unreadable, leave it blank and review the record manually.")

    with st.form("invoice_review_form"):

        left, right = st.columns(2)

        with left:

            invoice_number = st.text_input("Invoice Number", value=extracted["invoice_number"])

            company = st.text_input("Company (vendor)", value=extracted["company"])

            po_number = st.text_input("PO Number", value=extracted["po_number"])

        with right:

            invoice_date_input = st.text_input("Invoice Date (YYYY-MM-DD, optional)", value=extracted["invoice_date"])

            amount_input = st.text_input("Invoice amount (optional; original currency, verify in PDF)", value=str(extracted["amount"] or ""))

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

        st.caption(f'Checked: {fields["invoice_number"]} · {fields["company"]} · {fields["amount"] if fields["amount"] is not None else "Amount not provided"}')

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

            status_options = (["Potential Duplicate", "Needs Review", "New"] if possible
                              else ["Needs Review", "New", "Potential Duplicate"] if fields["amount"] is None
                              else ["New", "Needs Review", "Potential Duplicate"])

            selected_status = st.selectbox("Status to save", status_options, key="save_status")

            duplicate_url = ""

            if possible:

                choices = {f'{r["invoice_number"]} — {r["company"]} ({r["url"]})': r["url"] for r in possible}

                selected = st.selectbox("Optional: link related existing record", ["None"] + list(choices))

                duplicate_url = choices.get(selected, "")

            confirm_save = st.checkbox("I reviewed the vendor and invoice number, and verified the amount if available.")

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

                            # PDF UPLOAD DISABLED — retain for future use:
                            # pdf_upload_id = upload_invoice_pdf(
                            #     notion_token, uploaded_pdf.name, pdf_bytes
                            # )
                            # saved = create_invoice(
                            #     notion_token, ds_id, fields, selected_status, duplicate_url,
                            #     pdf_upload_id=pdf_upload_id, pdf_filename=uploaded_pdf.name,
                            # )
                            with st.spinner("Creating invoice record in Notion (no PDF attachment)..."):
                                saved = create_invoice(
                                    notion_token, ds_id, fields, selected_status, duplicate_url
                                )
                            st.success("Invoice details saved in Notion (PDF not uploaded).")
                            st.link_button("Open new Notion record", saved["url"])

                            for key in ("checked_fields", "checked_exact", "checked_possible", "checked_data_source_id", "checked_file_hash"):

                                st.session_state.pop(key, None)

                    except Exception as exc:

                        st.error(str(exc))

    with st.expander("View extracted PDF text (troubleshooting)"):

        st.text_area("PDF text", pdf_text, height=250)





if __name__ == "__main__":

    app()
