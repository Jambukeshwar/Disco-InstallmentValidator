"""
Nokia InstantLink (SOA) automations.

Exposes a single high-level helper, ``nokia_lookup_msisdn_summary``, that:
  1) Calls GetStatus by MSISDN  -> number_status
  2) Calls GetIccidDetails       -> IMSI / ICCID / SIM status

Only a safe whitelist of fields is returned. Sensitive SIM material
(PIN/PUK/KI/ADM/activation code) is parsed internally but never exposed.

Configuration is read from environment variables:
  NOKIA_ENDPOINT_URL   e.g. https://host:43043/ilws/InstantLinkSOA
  NOKIA_USERNAME       e.g. bss
  NOKIA_PASSWORD       real password
  NOKIA_TIMEOUT        seconds (default 30)
  NOKIA_VERIFY_TLS     1/0 (default 1)
"""
from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

NOKIA_NS = "http://soa.comptel.com/2011/02/instantlink"
SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
DISPLAY_ACTION = "http://soa.comptel.com/2011/02/instantlink/InstantLinkSOA/display"


@dataclass(frozen=True)
class NokiaConfig:
    endpoint_url: str
    username: str
    password: str
    timeout_seconds: int = 30
    verify_tls: bool = True


def _normalize_endpoint(url: str) -> str:
    """El POST SOAP va al servicio, no al descriptor — se tolera pegar la URL con ?wsdl."""
    url = (url or "").strip()
    if url.lower().endswith("?wsdl"):
        url = url[:-5].rstrip("?")
    return url


def get_nokia_config() -> NokiaConfig:
    """Build the Nokia configuration from environment variables (.env / secret).

    Nota 2026-07-14: se intentó migrar al DR fodr1.mobnokiaprdr-01:43043 pero
    no hay ruta de red desde el server (ConnectTimeout) — se sigue usando el
    endpoint del .env (foprod1) hasta que networking abra el flujo al DR.
    """
    load_dotenv()
    return NokiaConfig(
        endpoint_url=_normalize_endpoint(os.getenv("NOKIA_ENDPOINT_URL", "")),
        username=os.getenv("NOKIA_USERNAME", "") or "bss",
        password=os.getenv("NOKIA_PASSWORD", ""),
        timeout_seconds=int(os.getenv("NOKIA_TIMEOUT", "30")),
        verify_tls=os.getenv("NOKIA_VERIFY_TLS", "1").strip().lower() in {"1", "true", "yes", "on"},
    )


# --------------------------------------------------------------------------- #
# SOAP builders
# --------------------------------------------------------------------------- #
def _build_wsse_header(username: str, password: str) -> str:
    return f"""
    <wsse:Security soap:mustUnderstand="1"
        xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
        xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
      <wsse:UsernameToken wsu:Id="SOAI_req_SOAI">
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
    """.strip()


def _soap_envelope(action_url: str, wsse_header_xml: str, body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP12_NS}"
               xmlns:ins="{NOKIA_NS}"
               xmlns:wsa="http://www.w3.org/2005/08/addressing">
  <soap:Header>
    <wsa:Action>{action_url}</wsa:Action>
    {wsse_header_xml}
  </soap:Header>
  <soap:Body>
    {body_xml}
  </soap:Body>
</soap:Envelope>
""".strip()


def _display_request_xml(order_no: str, request_parameters_xml: str) -> str:
    return f"""
<ins:DisplayRequest>
  <ins:RequestHeader>
    <ins:NeType>ORDER</ins:NeType>
    <ins:OrderNo>{order_no}</ins:OrderNo>
    <ins:Priority>5</ins:Priority>
    <ins:ReqUser>User</ins:ReqUser>
  </ins:RequestHeader>
  <ins:RequestParameters>
    {request_parameters_xml}
  </ins:RequestParameters>
</ins:DisplayRequest>
""".strip()


def _param(name: str, value: str) -> str:
    return f'<ins:Parameter name="{name}" value="{value}"/>'


def _post_soap(config: NokiaConfig, xml_payload: str) -> str:
    headers = {
        "Content-Type": "application/soap+xml; charset=utf-8",
        "Accept": "application/soap+xml",
    }
    resp = requests.post(
        config.endpoint_url,
        data=xml_payload.encode("utf-8"),
        headers=headers,
        timeout=config.timeout_seconds,
        verify=config.verify_tls,
    )
    if resp.status_code >= 400:
        body = (resp.text or "")[:1500]
        raise RuntimeError(f"Nokia HTTP {resp.status_code}: {body}")
    return resp.text


def _parse_parameters_from_response(xml_text: str) -> Dict[str, str]:
    """Extract <ResponseParameters><Parameter name= value=/> into a flat dict."""
    root = ET.fromstring(xml_text)
    ns = {"soap": SOAP12_NS, "ins": NOKIA_NS}
    params: Dict[str, str] = {}
    for p in root.findall(".//ins:ResponseParameters/ins:Parameter", ns):
        name = p.attrib.get("name")
        value = p.attrib.get("value")
        if name is not None and value is not None:
            params[name] = value
    return params


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
def nokia_get_msisdn_number_status(config: NokiaConfig, msisdn: str) -> Dict[str, Any]:
    """GetStatus by MSISDN -> LINE_1_RESP_NUMBER_STATUS (ASSIGNED/FREE/QUARANTINE/...)."""
    wsse = _build_wsse_header(config.username, config.password)
    order_no = f"msisdn_status_{uuid.uuid4().hex[:8]}"
    request_params = "\n".join([
        _param("REQ_TYPE", "4"),
        _param("REQ_OBJ", "1"),
        _param("ORDER_TYPE", "Display"),
        _param("PRODUCT", "NUMBERS"),
        _param("LINE_1_ACTION", "GetStatus"),
        _param("LINE_1_NAME", "Numbers"),
        _param("LINE_1_PS_PARAM_MSISDN", msisdn),
        _param("LINE_1_PS_PARAM_NUMBER_TYPE", "MSISDN"),
    ])
    envelope = _soap_envelope(
        action_url=DISPLAY_ACTION,
        wsse_header_xml=wsse,
        body_xml=_display_request_xml(order_no, request_params),
    )
    xml_resp = _post_soap(config, envelope)
    params = _parse_parameters_from_response(xml_resp)
    return {
        "ok": True,
        "msisdn": msisdn,
        "order_no": order_no,
        "number_status": params.get("LINE_1_RESP_NUMBER_STATUS"),
    }


def nokia_get_iccid_details_by_related_msisdn(config: NokiaConfig, msisdn: str) -> Dict[str, Any]:
    """GetIccidDetails by related MSISDN -> IMSI/ICCID/SIM status (safe subset only)."""
    wsse = _build_wsse_header(config.username, config.password)
    order_no = f"iccid_details_{uuid.uuid4().hex[:8]}"
    request_params = "\n".join([
        _param("REQ_TYPE", "4"),
        _param("REQ_OBJ", "1"),
        _param("ORDER_TYPE", "Display"),
        _param("PRODUCT", "NUMBERS"),
        _param("LINE_1_ACTION", "GetIccidDetails"),
        _param("LINE_1_NAME", "Numbers"),
        _param("LINE_1_PS_PARAM_RELATED_MSISDN", msisdn),
    ])
    envelope = _soap_envelope(
        action_url=DISPLAY_ACTION,
        wsse_header_xml=wsse,
        body_xml=_display_request_xml(order_no, request_params),
    )
    xml_resp = _post_soap(config, envelope)
    params = _parse_parameters_from_response(xml_resp)
    safe = {
        "imsi": params.get("LINE_1_RESP_IMSI"),
        "iccid": params.get("LINE_1_RESP_ICCID"),
        "sim_status": params.get("LINE_1_RESP_STATUS"),
        "sim_type": params.get("LINE_1_RESP_SIM_TYPE"),
    }
    return {"ok": True, "msisdn": msisdn, "order_no": order_no, "safe": safe}


def nokia_get_iccid_details_by_iccid(config: NokiaConfig, iccid: str) -> Dict[str, Any]:
    """GetIccidDetails by ICCID -> IMSI/ICCID/SIM status (safe subset only)."""
    wsse = _build_wsse_header(config.username, config.password)
    order_no = f"iccid_details_{uuid.uuid4().hex[:8]}"
    request_params = "\n".join([
        _param("REQ_TYPE", "4"),
        _param("REQ_OBJ", "1"),
        _param("ORDER_TYPE", "Display"),
        _param("PRODUCT", "NUMBERS"),
        _param("LINE_1_ACTION", "GetIccidDetails"),
        _param("LINE_1_NAME", "Numbers"),
        _param("LINE_1_PS_PARAM_ICCID", iccid),
    ])
    envelope = _soap_envelope(
        action_url=DISPLAY_ACTION,
        wsse_header_xml=wsse,
        body_xml=_display_request_xml(order_no, request_params),
    )
    xml_resp = _post_soap(config, envelope)
    params = _parse_parameters_from_response(xml_resp)
    safe = {
        "imsi": params.get("LINE_1_RESP_IMSI"),
        "iccid": params.get("LINE_1_RESP_ICCID"),
        "sim_status": params.get("LINE_1_RESP_STATUS"),
        "sim_type": params.get("LINE_1_RESP_SIM_TYPE"),
        "msisdn": params.get("LINE_1_RESP_MSISDN"),
    }
    return {"ok": True, "iccid": iccid, "order_no": order_no, "safe": safe}


def nokia_lookup_iccid_summary(config: NokiaConfig, iccid: str) -> Dict[str, Any]:
    """SIM details for a single ICCID (safe dict), resilient to Nokia being down."""
    out: Dict[str, Any] = {
        "ok": False,
        "iccid": iccid,
        "msisdn": None,
        "imsi": None,
        "sim_status": None,
        "sim_type": None,
    }
    try:
        safe = nokia_get_iccid_details_by_iccid(config, iccid).get("safe", {})
        out["msisdn"] = safe.get("msisdn")
        out["imsi"] = safe.get("imsi")
        out["sim_status"] = safe.get("sim_status")
        out["sim_type"] = safe.get("sim_type")
        out["ok"] = True
        return out
    except requests.HTTPError as e:
        out["error"] = f"http_error: {e}"
        return out
    except ET.ParseError as e:
        out["error"] = f"xml_parse_error: {e}"
        return out
    except Exception as e:
        out["error"] = f"unexpected_error: {e}"
        return out


def nokia_lookup_msisdn_summary(config: NokiaConfig, msisdn: str) -> Dict[str, Any]:
    """Merge number status + SIM details into one safe dict for a single MSISDN."""
    out: Dict[str, Any] = {
        "ok": False,
        "msisdn": msisdn,
        "number_status": None,
        "sim_status": None,
        "iccid": None,
        "imsi": None,
    }
    try:
        r1 = nokia_get_msisdn_number_status(config, msisdn)
        out["number_status"] = r1.get("number_status")

        safe = nokia_get_iccid_details_by_related_msisdn(config, msisdn).get("safe", {})
        out["sim_status"] = safe.get("sim_status")
        out["iccid"] = safe.get("iccid")
        out["imsi"] = safe.get("imsi")

        out["ok"] = True
        return out
    except requests.HTTPError as e:
        out["error"] = f"http_error: {e}"
        return out
    except ET.ParseError as e:
        out["error"] = f"xml_parse_error: {e}"
        return out
    except Exception as e:
        out["error"] = f"unexpected_error: {e}"
        return out


def nokia_check_status(config: NokiaConfig) -> Dict[str, Any]:
    """Lightweight connectivity check for the Nokia SOA endpoint."""
    if not config.endpoint_url:
        return {"ok": False, "online": False, "error": "not_configured"}
    try:
        wsse = _build_wsse_header(config.username, config.password)
        order_no = f"status_ping_{uuid.uuid4().hex[:8]}"
        request_params = "\n".join([
            _param("REQ_TYPE", "4"),
            _param("REQ_OBJ", "1"),
            _param("ORDER_TYPE", "Display"),
            _param("PRODUCT", "NUMBERS"),
            _param("LINE_1_ACTION", "GetStatus"),
            _param("LINE_1_NAME", "Numbers"),
            _param("LINE_1_PS_PARAM_MSISDN", "10000000000"),
            _param("LINE_1_PS_PARAM_NUMBER_TYPE", "MSISDN"),
        ])
        envelope = _soap_envelope(
            action_url=DISPLAY_ACTION,
            wsse_header_xml=wsse,
            body_xml=_display_request_xml(order_no, request_params),
        )
        headers = {
            "Content-Type": "application/soap+xml; charset=utf-8",
            "Accept": "application/soap+xml",
        }
        resp = requests.post(
            config.endpoint_url,
            data=envelope.encode("utf-8"),
            headers=headers,
            timeout=min(config.timeout_seconds, 8),
            verify=config.verify_tls,
        )
        return {"ok": True, "online": resp.status_code < 500}
    except Exception as e:
        return {"ok": False, "online": False, "error": str(e)}


def nokia_multiple_lookup(config: NokiaConfig, msisdns: List[str]) -> List[Dict[str, Any]]:
    """Run ``nokia_lookup_msisdn_summary`` for several MSISDNs and return safe rows."""
    results: List[Dict[str, Any]] = []
    for msisdn in msisdns:
        msisdn = (msisdn or "").strip()
        if not msisdn:
            continue
        r = nokia_lookup_msisdn_summary(config, msisdn)
        row = {
            "ok": bool(r.get("ok", False)),
            "msisdn": r.get("msisdn", msisdn),
            "number_status": r.get("number_status"),
            "sim_status": r.get("sim_status"),
            "iccid": r.get("iccid"),
            "imsi": r.get("imsi"),
        }
        if r.get("error"):
            row["error"] = r["error"]
        results.append(row)
    return results
