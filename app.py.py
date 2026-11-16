import streamlit as st
import pandas as pd
import pdfplumber
import hashlib
import io
import re
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet

st.set_page_config(page_title="SARS AI Tax Engine — Multi-Doc Prototype", page_icon="📊", layout="wide")
st.title("🇿🇦 SARS AI Tax Engine — Multi-Document Ingestion Prototype")

# --- SIDEBAR: TAXPAYER PROFILE ---
st.sidebar.header("1. Taxpayer Profile Overrides")
age = st.sidebar.number_input("Taxpayer Age", min_value=18, max_value=100, value=35)
is_sa_resident = st.sidebar.checkbox("South African Tax Resident", value=True)
manual_gross_salary = st.sidebar.number_input("Fallback Gross Remuneration (ZAR)", value=450000.0, step=10000.0)
manual_ra = st.sidebar.number_input("Fallback RA Contributions (ZAR)", value=36000.0, step=5000.0)
has_medical = st.sidebar.checkbox("Belong to Medical Aid Scheme", value=True)
medical_deps = st.sidebar.number_input("Medical Scheme Dependants", min_value=0, max_value=10, value=1)

# --- DOCUMENT CLASSIFIER & PARSER ---
def classify_and_parse_pdf(uploaded_file, password: str = "") -> dict:
    full_text = ""
    with pdfplumber.open(uploaded_file, password=password or None) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

    file_bytes = uploaded_file.getvalue()
    sha256_hash = hashlib.sha256(file_bytes).hexdigest()
    upper_text = full_text.upper()

    # 1. Detect Document Type
    if any(k in upper_text for k in ["IRP5", "IT3(A)", "EMPLOYEE TAX CERTIFICATE", "3601", "4102"]):
        doc_type = "IRP5 / PAYSLIP"
        gross_match = re.search(r"(?:3601|GROSS REMUNERATION)[:\s]+R?\s*([\d,\s]+\.\d{2})", upper_text)
        paye_match = re.search(r"(?:4102|PAYE|EMPLOYEES TAX)[:\s]+R?\s*([\d,\s]+\.\d{2})", upper_text)
        extracted = {
            "gross_3601": float(gross_match.group(1).replace(",", "").replace(" ", "")) if gross_match else 0.0,
            "paye_4102": float(paye_match.group(1).replace(",", "").replace(" ", "")) if paye_match else 0.0,
        }
    elif any(k in upper_text for k in ["MEDICAL SCHEME", "TAX CERTIFICATE", "4005", "CONTRIBUTIONS RECEIVED"]):
        doc_type = "MEDICAL CERTIFICATE"
        amt_match = re.search(r"(?:4005|TOTAL CONTRIBUTIONS)[:\s]+R?\s*([\d,\s]+\.\d{2})", upper_text)
        extracted = {
            "med_4005": float(amt_match.group(1).replace(",", "").replace(" ", "")) if amt_match else 0.0
        }
    elif any(k in upper_text for k in ["IT3(F)", "RETIREMENT ANNUITY", "4006"]):
        doc_type = "RA CERTIFICATE (IT3f)"
        amt_match = re.search(r"(?:4006|TOTAL CONTRIBUTIONS)[:\s]+R?\s*([\d,\s]+\.\d{2})", upper_text)
        extracted = {
            "ra_4006": float(amt_match.group(1).replace(",", "").replace(" ", "")) if amt_match else 0.0
        }
    elif any(k in upper_text for k in ["TAX INVOICE", "VAT NO", "VAT REG", "INVOICE NO", "RECEIPT"]):
        doc_type = "TAX INVOICE / RECEIPT"
        tot_match = re.search(r"(?:TOTAL|AMOUNT DUE|INCLUSIVE)[:\s]+R?\s*([\d,\s]+\.\d{2})", upper_text)
        extracted = {
            "invoice_total": float(tot_match.group(1).replace(",", "").replace(" ", "")) if tot_match else 0.0
        }
    else:
        doc_type = "BANK STATEMENT"
        tx_pattern = re.compile(
            r"^(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{2,4}|\d{2}\s+[A-Za-z]{3}(?:\s+\d{2,4})?)\s+(.+?)\s+(-?[\d\s,]+\.\d{2})(?:\s*(Cr|CR|Dr|DR))?(?:\s+-?[\d\s,]+\.\d{2}(?:\s*(?:Cr|CR|Dr|DR))?)?$"
        )
        rows = []
        for line in full_text.split("\n"):
            m = tx_pattern.search(line.strip())
            if m:
                d_str, desc, raw_amt, ind = m.groups()
                amt = abs(float(raw_amt.replace(",", "").replace(" ", "")))
                is_cr = (ind and ind.upper() == "CR") or any(k in desc.upper() for k in ["SALARY", "INTEREST", "DEPOSIT"])
                rows.append({"Date": d_str, "Description": desc.strip(), "Amount": amt, "Type": "CREDIT" if is_cr else "DEBIT"})
        extracted = {"transactions": rows}

    return {
        "file_name": uploaded_file.name,
        "doc_type": doc_type,
        "sha256": sha256_hash,
        "extracted": extracted,
        "raw_text": full_text
    }

# --- MULTI-FILE UPLOADER ---
st.header("2. Batch Document Upload (Bank Statements, IRP5s, ITR12s, Medical & Trade Invoices)")
col_up, col_pw = st.columns([3, 1])
with col_up:
    uploaded_files = st.file_uploader(
        "Drop all your tax documents here (multiple PDFs or CSVs)",
        type=["pdf", "csv"],
        accept_multiple_files=True
    )
with col_pw:
    pdf_password = st.text_input("Shared PDF Password (if encrypted)", type="password")

if uploaded_files:
    parsed_docs = []
    all_transactions = []
    invoices_pool = []
    detected_gross = 0.0
    detected_paye = 0.0
    detected_ra = 0.0

    for f in uploaded_files:
        if f.name.lower().endswith(".csv"):
            df_csv = pd.read_csv(f)
            sha = hashlib.sha256(f.getvalue()).hexdigest()
            parsed_docs.append({"File": f.name, "Detected Type": "BANK STATEMENT (CSV)", "TAA S102 SHA-256": sha[:16] + "...", "Summary": f"{len(df_csv)} rows"})
            for _, r in df_csv.iterrows():
                all_transactions.append(dict(r))
        else:
            try:
                res = classify_and_parse_pdf(f, pdf_password)
                ext = res["extracted"]
                summary = ""
                if res["doc_type"] == "IRP5 / PAYSLIP":
                    detected_gross += ext["gross_3601"]
                    detected_paye += ext["paye_4102"]
                    summary = f"Gross (3601): R{ext['gross_3601']:,.2f} | PAYE (4102): R{ext['paye_4102']:,.2f}"
                elif res["doc_type"] == "RA CERTIFICATE (IT3f)":
                    detected_ra += ext["ra_4006"]
                    summary = f"RA Contributions (4006): R{ext['ra_4006']:,.2f}"
                elif res["doc_type"] == "MEDICAL CERTIFICATE":
                    summary = f"Medical Contributions (4005): R{ext['med_4005']:,.2f}"
                elif res["doc_type"] == "TAX INVOICE / RECEIPT":
                    invoices_pool.append({"file": f.name, "amount": ext["invoice_total"], "sha256": res["sha256"]})
                    summary = f"Invoice Total: R{ext['invoice_total']:,.2f}"
                elif res["doc_type"] == "BANK STATEMENT":
                    all_transactions.extend(ext["transactions"])
                    summary = f"{len(ext['transactions'])} transactions extracted"

                parsed_docs.append({
                    "File": res["file_name"],
                    "Detected Type": res["doc_type"],
                    "TAA S102 SHA-256": res["sha256"][:16] + "...",
                    "Summary": summary
                })
            except Exception as e:
                st.error(f"Error reading `{f.name}`: {e}")

    # 1. Document Vault Table
    st.subheader("3. Cryptographic Document Vault (TAA Section 102 Proof Index)")
    st.dataframe(pd.DataFrame(parsed_docs), use_container_width=True)

    # 2. Cross-Link Invoices to Bank Statement Debits
    if all_transactions:
        linked_rows = []
        for tx in all_transactions:
            amt = float(tx["Amount"])
            desc = str(tx["Description"]).upper()
            matched_proof = "—"

            # Match debit amount against uploaded invoices
            if str(tx["Type"]).upper() == "DEBIT":
                for inv in invoices_pool:
                    if abs(inv["amount"] - amt) < 0.05:
                        matched_proof = f"✅ Linked: {inv['file']} ({inv['sha256'][:8]})"
                        break

            code = "9999"
            if any(k in desc for k in ["DISCOVERY", "MED", "BONITAS"]): code = "4005"
            elif any(k in desc for k in ["ALLAN GRAY", "SANLAM", "RA"]): code = "4006"
            elif any(k in desc for k in ["SALARY", "WAGES"]): code = "3601"
            elif any(k in desc for k in ["FIBRE", "INTERNET", "ESKOM", "AFRIHOST"]): code = "4028"

            linked_rows.append({
                "Date": tx["Date"],
                "Description": tx["Description"],
                "Amount": amt,
                "Type": tx["Type"],
                "SARS Code": code,
                "Supporting Invoice Link": matched_proof
            })

        st.subheader("4. Consolidated Ledger & Automated Invoice Reconciliation")
        st.dataframe(pd.DataFrame(linked_rows), use_container_width=True)

    # 3. Calculate Using Extracted IRP5/RA Values (or Sidebar Fallbacks)
    effective_gross = detected_gross if detected_gross > 0 else manual_gross_salary
    effective_ra = detected_ra if detected_ra > 0 else manual_ra
    primary_rebate = 17235 if is_sa_resident else 0
    secondary_rebate = 9444 if (is_sa_resident and age >= 65) else 0
    total_credits = (primary_rebate + secondary_rebate) + ((364 * 12) + (364 * 12 * medical_deps) if has_medical else 0) + detected_paye
    ra_deduction = min(effective_ra, effective_gross * 0.275, 350000)

    low_net = total_credits - (max(0, effective_gross - ra_deduction) * 0.22)
    med_net = total_credits - (max(0, effective_gross - (ra_deduction + 15000)) * 0.21)
    high_net = total_credits - (max(0, effective_gross - (ra_deduction + 30000)) * 0.19)

    st.markdown("---")
    st.header("5. Multi-Tier Risk Strategy Output")
    tier = st.radio("Select Strategy Tier:", ["LOW RISK", "MEDIUM RISK", "HIGH RISK"], horizontal=True)
    net_val = low_net if tier == "LOW RISK" else med_net if tier == "MEDIUM RISK" else high_net
    audit_pct = 12 if tier == "LOW RISK" else 35 if tier == "MEDIUM RISK" else 82

    c1, c2, c3 = st.columns(3)
    c1.metric("Effective Gross Income (3601)", f"R {effective_gross:,.2f}")
    c2.metric("Estimated Net Tax Outcome", f"R {net_val:,.2f}", delta="Refund" if net_val > 0 else "Payable")
    c3.metric("SARS Audit Risk Score", f"{audit_pct}%")