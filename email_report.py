"""
Email report builder and sender for the Disco Validator daily report.
Reads SMTP config from .env and sends an HTML email with Excel attachment.
"""

import os
import smtplib
from collections import Counter
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST           = os.getenv("SMTP_HOST", "")
SMTP_PORT           = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER           = os.getenv("SMTP_USER", "")
SMTP_PASSWORD       = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM           = os.getenv("SMTP_FROM", "")
SMTP_TO             = os.getenv("SMTP_TO", "")
SMTP_SENDER_NAME    = os.getenv("SMTP_SENDER_NAME", "Disco Validator")
SMTP_SUBJECT_PREFIX = os.getenv("SMTP_SUBJECT_PREFIX", "Disconnect Validation Suite - Report")


def build_html_body(date_label: str, aria_list: list, mtx_list: list, sf_list: list) -> str:
    records = len(aria_list)

    from collections import defaultdict

    def _has_action(field_to_review: str) -> bool:
        return any(
            f.strip() and f.strip() != "No_Action_Required"
            for f in (field_to_review or "").split("|")
        )

    # Group Aria plan instances by MSISDN (multiple plans per MSISDN possible)
    msisdn_aria = defaultdict(list)
    for r in aria_list:
        msisdn_aria[r.get("msisdn")].append(r)

    msisdn_mtx = {r.get("msisdn"): r for r in mtx_list}
    msisdn_sf  = {r.get("msisdn"): r for r in sf_list}

    # All unique MSISDNs across all systems
    all_msisdns = set(msisdn_aria.keys()) | set(msisdn_mtx.keys()) | set(msisdn_sf.keys())
    total = len(all_msisdns)

    clean = warn = err = 0
    for m in all_msisdns:
        aria_plans = msisdn_aria.get(m, [])
        mtx_row    = msisdn_mtx.get(m, {})
        sf_row     = msisdn_sf.get(m, {})

        has_error = (
            any(p.get("error_msg") for p in aria_plans) or
            bool(mtx_row.get("mtx_error")) or
            bool(sf_row.get("sf_error"))
        )
        has_action = (
            any(_has_action(p.get("field_to_review")) for p in aria_plans) or
            _has_action(mtx_row.get("mtx_field_to_review")) or
            _has_action(sf_row.get("sf_field_to_review"))
        )

        if has_error:
            err += 1
        elif has_action:
            warn += 1
        else:
            clean += 1

    def _combo_rows(lst, ftr_key, tag_bg, tag_fg):
        """Build HTML table rows for field_to_review combination counts."""
        combo_counter = Counter()
        for r in lst:
            ftr = (r.get(ftr_key) or "").strip()
            if ftr and ftr != "No_Action_Required" and "BANCAN_Mismatch" not in ftr:
                combo_counter[ftr] += 1

        if not combo_counter:
            return '<tr><td colspan="2" style="padding:14px 16px;color:#4caf50;text-align:center">✅ All records clean</td></tr>'

        rows = ""
        for combo, count in combo_counter.most_common():
            flags = [f.strip() for f in combo.split("|")]
            tags_html = ""
            for i, flag in enumerate(flags):
                if i > 0:
                    tags_html += '<span style="color:#4d5663;font-size:12px;margin:0 2px">|</span>'
                tags_html += (
                    f'<span style="display:inline-block;font-size:11px;padding:2px 7px;'
                    f'border-radius:4px;font-weight:600;background:{tag_bg};color:{tag_fg}">{flag}</span>'
                )
            rows += (
                f'<tr>'
                f'<td style="padding:10px 16px;border-bottom:1px solid #2d333b">{tags_html}</td>'
                f'<td style="padding:10px 16px;border-bottom:1px solid #2d333b;text-align:center;'
                f'font-weight:700;color:#ffaa00;font-size:15px;white-space:nowrap">{count}</td>'
                f'</tr>'
            )
        return rows

    def _sys_section(label, lst, ftr_key, val_key, tag_bg, tag_fg):
        sc = sum(1 for r in lst if r.get(val_key) == "✅")
        sw = sum(1 for r in lst if r.get(val_key) == "⚠️")
        se = sum(1 for r in lst if r.get(val_key) not in ("✅", "⚠️") and r.get(val_key))
        rows = _combo_rows(lst, ftr_key, tag_bg, tag_fg)
        sub = (
            f'<span style="color:#4caf50;font-size:11px;font-weight:400">{sc} ✅</span>'
            f'&nbsp;&nbsp;'
            f'<span style="color:#ffaa00;font-size:11px;font-weight:400">{sw} ⚠️</span>'
            f'&nbsp;&nbsp;'
            f'<span style="color:#f44336;font-size:11px;font-weight:400">{se} ❌</span>'
        )
        return f"""
        <tr><td style="padding:20px 24px 8px">
          <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px">
            {label} &nbsp; {sub}
          </div>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#1c2128;border-radius:8px;border:1px solid #30363d;overflow:hidden">
            <tr style="background:#21262d">
              <th style="padding:10px 16px;text-align:left;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase">Field to Review</th>
              <th style="padding:10px 16px;text-align:center;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase;width:70px">Count</th>
            </tr>
            {rows}
          </table>
        </td></tr>"""

    aria_section = _sys_section("🔵 Aria Validation",     aria_list, "field_to_review",         "validation",     "#1a2d3d", "#79b8ff")
    mtx_section  = _sys_section("🟠 Matrixx Validation",  mtx_list,  "mtx_field_to_review",      "mtx_validation", "#2d2200", "#ffcc40")
    sf_section   = _sys_section("🟢 SF Asset Validation", sf_list,   "sf_field_to_review",        "sf_validation",  "#1a2d1a", "#81c784")

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:Arial,Helvetica,sans-serif;color:#e6edf3">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0d1117;padding:32px 16px">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" style="background:#161b22;border-radius:12px;overflow:hidden;border:1px solid #30363d">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#1e3a5f,#0d2137);padding:28px 28px 24px">
          <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-0.5px">⚡ Disconnect Validation Suite</div>
          <div style="font-size:13px;color:#90caf9;margin-top:6px">Daily Report &nbsp;·&nbsp; {date_label}</div>
        </td></tr>

        <!-- Overall Stats Cards -->
        <tr><td style="padding:24px 24px 8px">
          <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px">Overall Summary</div>
          <table width="100%" cellpadding="0" cellspacing="4">
            <tr>
              <td width="19%" style="text-align:center;padding:2px">
                <div style="background:#1c2128;border-radius:10px;padding:18px 8px;border:1px solid #30363d">
                  <div style="font-size:28px;font-weight:800;color:#58a6ff;line-height:1">{total}</div>
                  <div style="font-size:10px;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:1px">MSISDNs</div>
                </div>
              </td>
              <td width="1%"></td>
              <td width="19%" style="text-align:center;padding:2px">
                <div style="background:#1c2128;border-radius:10px;padding:18px 8px;border:1px solid #30363d">
                  <div style="font-size:28px;font-weight:800;color:#8b949e;line-height:1">{records}</div>
                  <div style="font-size:10px;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:1px">Records</div>
                </div>
              </td>
              <td width="1%"></td>
              <td width="19%" style="text-align:center;padding:2px">
                <div style="background:#1c2128;border-radius:10px;padding:18px 8px;border:1px solid #30363d">
                  <div style="font-size:28px;font-weight:800;color:#4caf50;line-height:1">{clean}</div>
                  <div style="font-size:10px;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:1px">Clean ✅</div>
                </div>
              </td>
              <td width="1%"></td>
              <td width="19%" style="text-align:center;padding:2px">
                <div style="background:#1c2128;border-radius:10px;padding:18px 8px;border:1px solid #30363d">
                  <div style="font-size:28px;font-weight:800;color:#ffaa00;line-height:1">{warn}</div>
                  <div style="font-size:10px;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:1px">Action Needed ⚠️</div>
                </div>
              </td>
              <td width="1%"></td>
              <td width="19%" style="text-align:center;padding:2px">
                <div style="background:#1c2128;border-radius:10px;padding:18px 8px;border:1px solid #30363d">
                  <div style="font-size:28px;font-weight:800;color:#f44336;line-height:1">{err}</div>
                  <div style="font-size:10px;color:#8b949e;margin-top:6px;text-transform:uppercase;letter-spacing:1px">Errors ❌</div>
                </div>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- Per System Sections -->
        {aria_section}
        {mtx_section}
        {sf_section}

        <!-- Footer -->
        <tr><td style="padding:20px 24px 24px">
          <div style="border-top:1px solid #30363d;padding-top:16px">
            <div style="font-size:12px;color:#8b949e">📎 Full validation report attached as Excel.</div>
            <div style="font-size:11px;color:#4d5663;margin-top:4px">Generated by ITOps Automation · Disconnect Validation Suite</div>
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_report(
    subject_suffix: str,
    html_body: str,
    excel_bytes: bytes,
    filename: str,
    smtp_to: str = "",
    sender_name: str = "",
    subject_prefix: str = "",
):
    if not SMTP_HOST or not SMTP_FROM:
        raise ValueError("SMTP not configured — check SMTP_HOST, SMTP_FROM in .env")

    to_str  = smtp_to or SMTP_TO
    name    = sender_name or SMTP_SENDER_NAME
    prefix  = subject_prefix or SMTP_SUBJECT_PREFIX
    if not to_str:
        raise ValueError("No recipients configured — set SMTP_TO in .env or in Config page")

    recipients = [r.strip() for r in to_str.split(",") if r.strip()]

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"{prefix} — {subject_suffix}"
    msg["From"]    = f"{name} <{SMTP_FROM}>"
    msg["To"]      = ", ".join(recipients)

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if filename.endswith(".csv"):
        part = MIMEBase("text", "csv")
    else:
        part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    part.set_payload(excel_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if SMTP_USER and SMTP_PASSWORD:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, recipients, msg.as_bytes())


def build_connectivity_alert_html(date_label: str, issues: list[str]) -> str:
    rows = "".join(
        f'<tr><td style="padding:10px 16px;border-bottom:1px solid #2d333b;color:#f44336;font-size:14px">⚠️ {issue}</td></tr>'
        for issue in issues
    )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:Arial,Helvetica,sans-serif;color:#e6edf3">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0d1117;padding:32px 16px">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="background:#161b22;border-radius:12px;overflow:hidden;border:1px solid #30363d">

        <tr><td style="background:linear-gradient(135deg,#5c1e1e,#2d0f0f);padding:28px 28px 24px">
          <div style="font-size:20px;font-weight:800;color:#fff">🔴 Scheduled Report — Connection Alert</div>
          <div style="font-size:13px;color:#f48fb1;margin-top:6px">Scheduled run for {date_label} could not start</div>
        </td></tr>

        <tr><td style="padding:24px 24px 8px">
          <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px">Connection Issues Detected</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#1c2128;border-radius:8px;border:1px solid #30363d;overflow:hidden">
            {rows}
          </table>
        </td></tr>

        <tr><td style="padding:20px 24px 24px">
          <div style="background:#1c2128;border:1px solid #f4433640;border-radius:8px;padding:16px 20px">
            <div style="font-size:13px;color:#e6edf3;font-weight:600;margin-bottom:8px">Action Required</div>
            <div style="font-size:13px;color:#8b949e;line-height:1.7">
              Please restore the connection(s) listed above, then use the
              <strong style="color:#58a6ff">⚙️ Config → Run Now</strong> button in the
              Disconnect Validation Suite to manually trigger today's report.
            </div>
          </div>
        </td></tr>

        <tr><td style="padding:0 24px 20px">
          <div style="border-top:1px solid #30363d;padding-top:14px">
            <div style="font-size:11px;color:#4d5663">Generated by ITOps Automation · Disconnect Validation Suite</div>
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_alert(
    subject_suffix: str,
    html_body: str,
    smtp_to: str = "",
    sender_name: str = "",
    subject_prefix: str = "",
):
    """Send an alert email without an Excel attachment."""
    if not SMTP_HOST or not SMTP_FROM:
        raise ValueError("SMTP not configured — check SMTP_HOST, SMTP_FROM in .env")

    to_str = smtp_to or SMTP_TO
    name   = sender_name or SMTP_SENDER_NAME
    prefix = subject_prefix or SMTP_SUBJECT_PREFIX
    if not to_str:
        raise ValueError("No recipients configured")

    recipients = [r.strip() for r in to_str.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{prefix} — {subject_suffix}"
    msg["From"]    = f"{name} <{SMTP_FROM}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if SMTP_USER and SMTP_PASSWORD:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, recipients, msg.as_bytes())
