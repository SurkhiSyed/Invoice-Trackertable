
import re
from datetime import date, datetime
from io import BytesIO

import requests
import streamlit as st
from pypdf import PdfReader


# -------------------- App setup --------------------

st.set_page_config(
    page_title="Invoice Duplicate Checker",
    page_icon="🧾",
    layout="wide",
)

NOTION_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"


# -------------------- Notion API helpers --------------------

def notion_request(method, path, token, **kwargs):
    """Send a request to Notion and show a useful error if it fails."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(
            method,
            f"{NOTION_BASE_URL}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Notion: {exc}") from exc

    if not response.ok:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"Notion returned HTTP {response.status_code}: {detail}"
        )

    return response.json()


def get_data_source_id(token, database_id):
    """Find the data source belonging to the Invoice Tracker database."""
    database = notion_request(
        "GET", f"/databases/{database_id}", token
    )
    sources = database.get("data_sources", [])

    if not sources:
        raise RuntimeError(
            "No data source was returned for this database. "
            "Check the database ID and Notion integration access."
        )

    # This app expects one invoice table/data source.
    return sources[0]["id"]


def get_all_invoices(token, data_source_id):
    """Fetch all existing rows, following Notion pagination."""
    invoices = []
    cursor = None

    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor

        result = notion_request(
            "POST",
            f"/data_sources/{data_source_id}/query",
            token,
            json=payload,
        )
        invoices.extend(result.get("results", []))

        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return invoices


def property_text(prop):
    """Read common Notion text-like property values."""
    if not prop:
        return ""

    kind = prop.get("type")

    if kind == "title":
        items = prop.get("title", [])
    elif kind == "rich_text":
        items = prop.get("rich_text", [])
    elif kind == "select":
        return (prop.get("select") or {}).get("name", "")
    elif kind == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    elif kind == "date":
        return (prop.get("date") or {}).get("start", "")
    elif kind == "url":
        return prop.get("url") or ""
    else:
        return ""

    return "".join(item.get("plain_text", "") for item in items)


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


# -------------------- PDF extraction --------------------

def extract_pdf_text(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError(
                "This PDF is password-protected. Please use an "
                "unlocked PDF for this test."
            ) from exc

    #text = "\n".join(page.extract_text() or "" for page in reader.pages)
    text = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages)    
    if not text.strip():
        raise ValueError(
            "No selectable text was found. This may be a scanned PDF; "
            "OCR support is not included in this first version."
        )

    return text


def first_match(patterns, text, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip(" \t:#-")
    return ""

def extract_invoice_fields(text):
    """Best-effort extraction. Handles 'Track ID' variants and spatial column formatting.""" 
    
    # Pre-clean excessive horizontal gaps created by layout extraction mode
    text_clean = re.sub(r' +', ' ', text)

    invoice_number = first_match(
        [
            # 1. Matches Invoice Track ID / Tracking ID / Reference ID variants
            r"\b(?:invoice\s*)?(?:track(?:ing)?\s*id|reference\s*no\.?|billing\s*code)\s*[:#\s-]*\s*([A-Z0-9][A-Z0-9/_-]*)\b",
            
            # 2. Standard variants (Invoice Number, Inv #, No.)
            r"\b(?:invoice\s*(?:number|no\.?|#|id)|inv\.?\s*(?:number|no\.?|#|id))\s*[:#\s-]*\s*([A-Z0-9][A-Z0-9/_-]*)\b",
            
            # 3. Aggressive loose keyword fallback (Invoice: VALUE)
            r"\b(?:invoice|inv\.?)\s*[:#\s-]*\s*([A-Z0-9][A-Z0-9/_-]*)\b",
        ],
        text_clean,
    )

    po_number = first_match(
        [
            r"\b(?:purchase\s*order|P\.?\s*O\.?)\s*(?:number|no\.?|#)?\s*[:#\s-]*\s*([A-Z0-9][A-Z0-9/_-]*)\b"
        ],
        text_clean,
    )

    date_text = first_match(
        [
            r"\b(?:invoice\s*date|date\s*of\s*invoice|date)\s*[:#\s-]*\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            r"\b(?:invoice\s*date|date\s*of\s*invoice|date)\s*[:#\s-]*\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
        ],
        text_clean,
    )

    invoice_date = ""
    if date_text:
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d",
            "%m/%d/%Y", "%d/%m/%Y",
            "%m-%d-%Y", "%d-%m-%Y",
            "%m/%d/%y", "%d/%m/%y",
        ):
            try:
                invoice_date = datetime.strptime(date_text, fmt).date().isoformat()
                break
            except ValueError:
                pass

    amount_text = first_match(
        [
            r"\b(?:total\s*amount|invoice\s*total|amount\s*due|balance\s*due|grand\s*total|total)\s*[:$\s]*\s*(?:CAD\s*)?\$?\s*([\d,]+\.\d{2})",
            r"\b(?:CAD|USD)\s*\$?\s*([\d,]+\.\d{2})",
        ],
        text_clean,
    )

    amount = None
    if amount_text:
        try:
            amount = float(amount_text.replace(",", ""))
        except ValueError:
            amount = None

    lines = [line.strip() for line in text_clean.splitlines() if line.strip()]
    company = lines[0][:100] if lines else ""

    return {
        "invoice_number": invoice_number,
        "company": company,
        "invoice_date": invoice_date,
        "amount": amount,
        "po_number": po_number,
    }



# -------------------- Duplicate checking --------------------

def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def amount_value(value):
    try:
        return round(float(str(value).replace(",", "").replace("$", "")), 2)
    except (TypeError, ValueError):
        return None


def find_duplicates(candidate, existing):
    exact = []
    possible = []

    candidate_number = normalize(candidate["invoice_number"])
    candidate_company = normalize(candidate["company"])
    candidate_po = normalize(candidate["po_number"])
    candidate_amount = amount_value(candidate["amount"])

    for old in existing:
        old_number = normalize(old["invoice_number"])
        old_company = normalize(old["company"])
        old_po = normalize(old["po_number"])
        old_amount = amount_value(old["amount"])

        # Exact invoice-number match, regardless of company.
        if candidate_number and old_number == candidate_number:
            exact.append(old)
            continue

        # Potential duplicate: same company and amount, optionally same PO.
        same_company = (
            candidate_company
            and old_company
            and candidate_company == old_company
        )
        same_amount = (
            candidate_amount is not None
            and old_amount is not None
            and candidate_amount == old_amount
        )
        same_po = candidate_po and old_po and candidate_po == old_po

        if same_company and same_amount:
            possible.append(old)
        elif same_company and same_po:
            possible.append(old)

    return exact, possible


# -------------------- Save to Notion --------------------

def create_invoice(token, data_source_id, fields, status, duplicate_url=""):
    properties = {
        "Invoice Number": {
            "title": [
                {"text": {"content": fields["invoice_number"]}}
            ]
        },
        "Company": {
            "rich_text": [
                {"text": {"content": fields["company"]}}
            ]
        },
        "Status": {"select": {"name": status}},
    }

    if fields["invoice_date"]:
        properties["Invoice Date"] = {
            "date": {"start": fields["invoice_date"]}
        }

    if fields["amount"] is not None:
        properties["Amount"] = {"number": float(fields["amount"])}

    if fields["po_number"]:
        properties["PO Number"] = {
            "rich_text": [
                {"text": {"content": fields["po_number"]}}
            ]
        }

    # Duplicate Of is a URL property in the current database schema.
    if duplicate_url:
        properties["Duplicate Of"] = {"url": duplicate_url}

    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    }

    return notion_request("POST", "/pages", token, json=payload)


# -------------------- User interface --------------------

st.title("🧾 Invoice Duplicate Checker")
st.write(
    "Upload a PDF, review the extracted invoice details, "
    "compare them with Notion, and decide whether to save."
)

try:
    notion_token = st.secrets["NOTION_TOKEN"]
    database_id = st.secrets["NOTION_DATABASE_ID"]
except Exception:
    st.error(
        "Notion credentials were not found. Check that "
        ".streamlit/secrets.toml exists and contains NOTION_TOKEN "
        "and NOTION_DATABASE_ID. Restart Streamlit after editing it."
    )
    st.stop()

with st.sidebar:
    st.subheader("Connection")
    if st.button("Test Notion connection", use_container_width=True):
        try:
            ds_id = get_data_source_id(notion_token, database_id)
            rows = get_all_invoices(notion_token, ds_id)
            st.success(f"Connected. Found {len(rows)} invoice record(s).")
        except Exception as exc:
            st.error(str(exc))

uploaded_pdf = st.file_uploader("Choose an invoice PDF", type=["pdf"])

if uploaded_pdf:
    try:
        pdf_text = extract_pdf_text(uploaded_pdf.getvalue())
        extracted = extract_invoice_fields(pdf_text)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    st.subheader("1. Review extracted information")
    st.caption(
        "These values are guesses from the PDF. Correct them before "
        "checking or saving."
    )

    with st.form("invoice_review_form"):
        col1, col2 = st.columns(2)

        with col1:
            invoice_number = st.text_input(
                "Invoice Number", extracted["invoice_number"]
            )
            company = st.text_input("Company", extracted["company"])
            po_number = st.text_input("PO Number", extracted["po_number"])

        with col2:
            date_default = None
            if extracted["invoice_date"]:
                try:
                    date_default = date.fromisoformat(
                        extracted["invoice_date"]
                    )
                except ValueError:
                    pass

            invoice_date = st.date_input(
                "Invoice Date",
                value=date_default or date.today(),
            )

            amount = st.number_input(
                "Amount",
                min_value=0.0,
                value=float(extracted["amount"] or 0.0),
                step=0.01,
                format="%.2f",
            )

        check_clicked = st.form_submit_button(
            "Check for duplicates", type="primary"
        )

    if check_clicked:
        if not invoice_number.strip():
            st.error("Enter an invoice number before checking.")
            st.stop()

        fields = {
            "invoice_number": invoice_number.strip(),
            "company": company.strip(),
            "invoice_date": invoice_date.isoformat(),
            "amount": amount,
            "po_number": po_number.strip(),
        }

        try:
            with st.spinner("Checking the Notion invoice register..."):
                data_source_id = get_data_source_id(
                    notion_token, database_id
                )
                pages = get_all_invoices(notion_token, data_source_id)
                records = [invoice_record(p) for p in pages]
                exact, possible = find_duplicates(fields, records)

            st.session_state["checked_fields"] = fields
            st.session_state["checked_exact"] = exact
            st.session_state["checked_possible"] = possible
            st.session_state["checked_data_source_id"] = data_source_id

        except Exception as exc:
            st.error(str(exc))

if "checked_fields" in st.session_state:
    fields = st.session_state["checked_fields"]
    exact = st.session_state["checked_exact"]
    possible = st.session_state["checked_possible"]
    data_source_id = st.session_state["checked_data_source_id"]

    st.subheader("2. Duplicate check results")

    if exact:
        st.error(
            f"Exact invoice-number match found: {len(exact)} existing record(s)."
        )
        st.dataframe(
            [
                {
                    "Invoice Number": r["invoice_number"],
                    "Company": r["company"],
                    "Date": r["invoice_date"],
                    "Amount": r["amount"],
                    "Status": r["status"],
                    "Open Notion record": r["url"],
                }
                for r in exact
            ],
            use_container_width=True,
        )
    elif possible:
        st.warning(
            f"{len(possible)} possible duplicate(s) found. "
            "Review them before deciding."
        )
        st.dataframe(
            [
                {
                    "Invoice Number": r["invoice_number"],
                    "Company": r["company"],
                    "Date": r["invoice_date"],
                    "Amount": r["amount"],
                    "PO Number": r["po_number"],
                    "Status": r["status"],
                    "Open Notion record": r["url"],
                }
                for r in possible
            ],
            use_container_width=True,
        )
    else:
        st.success("No matching invoice was found in the current register.")

    st.subheader("3. Save reviewed invoice")
    st.caption(
        "Saving creates a new Notion row. This does not upload or attach "
        "the PDF itself."
    )

    if exact:
        st.error(
            "Saving is disabled because an exact invoice-number match exists. "
            "Verify the existing record first."
        )
    else:
        if possible:
            status_options = ["Potential Duplicate", "Needs Review", "New"]
            default_status = "Potential Duplicate"
        else:
            status_options = ["New", "Needs Review", "Potential Duplicate"]
            default_status = "New"

        selected_status = st.selectbox(
            "Status to save", status_options,
            index=0,
            key="save_status",
        )

        duplicate_url = ""
        if possible:
            duplicate_choices = {
                f'{r["invoice_number"]} — {r["company"]} — '
                f'{r["amount"]} ({r["url"]})': r["url"]
                for r in possible
            }
            selected_duplicate = st.selectbox(
                "Optional: choose the matching existing record",
                ["None"] + list(duplicate_choices.keys()),
            )
            if selected_duplicate != "None":
                duplicate_url = duplicate_choices[selected_duplicate]

        confirm_save = st.checkbox(
            "I reviewed the extracted details and want to create a Notion record."
        )

        if st.button("Save invoice to Notion", type="primary"):
            if not confirm_save:
                st.warning("Check the confirmation box before saving.")
            elif not fields["invoice_number"].strip():
                st.error("Invoice Number cannot be blank.")
            else:
                try:
                    result = create_invoice(
                        notion_token,
                        data_source_id,
                        fields,
                        selected_status,
                        duplicate_url,
                    )
                    st.success("Invoice record created in Notion.")
                    st.link_button(
                        "Open the new Notion record", result["url"]
                    )
                    # Clear the saved check results to avoid accidental resubmission.
                    for key in (
                        "checked_fields",
                        "checked_exact",
                        "checked_possible",
                        "checked_data_source_id",
                    ):
                        st.session_state.pop(key, None)
                except Exception as exc:
                    st.error(str(exc))

with st.expander("View extracted PDF text (for troubleshooting)"):
    if uploaded_pdf:
        st.text_area(
            "PDF text",
            pdf_text if "pdf_text" in locals() else "",
            height=250,
        )
    else:
        st.write("Upload a PDF to view its extracted text.")
