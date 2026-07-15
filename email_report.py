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

    # Group plan instances by MSISDN for MSISDN-level summary
    from collections import defaultdict
    msisdn_plans = defaultdict(list)
    for r in aria_list:
        msisdn_plans[r.get("msisdn")].append(r)

    total = len(msisdn_plans)  # unique MSISDNs
    # MSISDN is clean only if ALL its plan instances are ✅
    clean = sum(1 for plans in msisdn_plans.values() if all(p.get("validation") == "✅" for p in plans))
    # MSISDN needs action if ANY plan instance is ⚠️
    warn  = sum(1 for plans in msisdn_plans.values() if any(p.get("validation") == "⚠️" for p in plans))
    err   = sum(1 for plans in msisdn_plans.values() if any(p.get("validation") not in ("✅", "⚠️") and p.get("validation") for p in plans))

    systems = [
        ("🔵 Aria",     aria_list, "validation"),
        ("🟠 Matrixx",  mtx_list,  "mtx_validation"),
        ("🟢 SF Asset", sf_list,   "sf_validation"),
    ]

    flag_counter = Counter()
    for lst, key in [
        (aria_list, "field_to_review"),
        (mtx_list,  "mtx_field_to_review"),
        (sf_list,   "sf_field_to_review"),
    ]:
        for r in lst:
            for f in (r.get(key) or "").split("|"):
                f = f.strip()
                if f and f not in ("No_Action_Required", "BANCAN_Mismatch"):
                    flag_counter[f] += 1

    # Per-system rows
    sys_rows = ""
    for name, lst, vk in systems:
        sc = sum(1 for r in lst if r.get(vk) == "✅")
        sw = sum(1 for r in lst if r.get(vk) == "⚠️")
        se = sum(1 for r in lst if r.get(vk) not in ("✅", "⚠️") and r.get(vk))
        sys_rows += f"""
            <tr>
              <td style="padding:10px 16px;border-bottom:1px solid #2d333b;font-weight:600;color:#e6edf3">{name}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #2d333b;text-align:center;color:#4caf50;font-weight:700;font-size:16px">{sc}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #2d333b;text-align:center;color:#ffaa00;font-weight:700;font-size:16px">{sw}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #2d333b;text-align:center;color:#f44336;font-weight:700;font-size:16px">{se}</td>
            </tr>"""

    # Top flags rows
    flag_rows = ""
    if flag_counter:
        for flag, count in flag_counter.most_common(10):
            flag_rows += f"""
            <tr>
              <td style="padding:9px 16px;border-bottom:1px solid #2d333b;color:#e6edf3">{flag}</td>
              <td style="padding:9px 16px;border-bottom:1px solid #2d333b;text-align:center;font-weight:700;color:#ffaa00">{count}</td>
            </tr>"""
    else:
        flag_rows = '<tr><td colspan="2" style="padding:14px 16px;color:#4caf50;text-align:center">✅ No flags — all records clean</td></tr>'

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

        <!-- Per System Breakdown -->
        <tr><td style="padding:20px 24px 8px">
          <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px">Per System Breakdown</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#1c2128;border-radius:8px;border:1px solid #30363d;overflow:hidden">
            <tr style="background:#21262d">
              <th style="padding:10px 16px;text-align:left;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase">System</th>
              <th style="padding:10px 16px;text-align:center;font-size:11px;color:#4caf50;font-weight:600;text-transform:uppercase">Clean</th>
              <th style="padding:10px 16px;text-align:center;font-size:11px;color:#ffaa00;font-weight:600;text-transform:uppercase">Action Needed</th>
              <th style="padding:10px 16px;text-align:center;font-size:11px;color:#f44336;font-weight:600;text-transform:uppercase">Errors</th>
            </tr>
            {sys_rows}
          </table>
        </td></tr>

        <!-- Top Flags -->
        <tr><td style="padding:20px 24px 8px">
          <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px">Top Flags</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#1c2128;border-radius:8px;border:1px solid #30363d;overflow:hidden">
            <tr style="background:#21262d">
              <th style="padding:10px 16px;text-align:left;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase">Flag</th>
              <th style="padding:10px 16px;text-align:center;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase">Count</th>
            </tr>
            {flag_rows}
          </table>
        </td></tr>

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
