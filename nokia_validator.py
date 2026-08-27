"""
Nokia InstantLink validator.
Fetches number_status and SIM details (sim_status, iccid, imsi) per MSISDN.

Validation:
  ✅  Data collected   — Nokia responded OK (no flags raised yet)
  ❌  Error            — Nokia unreachable or returned an error
"""

from nokia_api import get_nokia_config, nokia_lookup_msisdn_summary


def process_single(msisdn: str) -> dict:
    """Fetch Nokia data for one MSISDN. No validation flags raised for now."""
    base = {
        "msisdn":              msisdn,
        "nok_number_status":   None,
        "nok_sim_status":      None,
        "nok_iccid":           None,
        "nok_imsi":            None,
        "nok_validation":      None,
        "nok_field_to_review": None,
        "nok_error":           None,
    }

    try:
        config = get_nokia_config()
        result = nokia_lookup_msisdn_summary(config, msisdn)

        base["nok_number_status"] = result.get("number_status")
        base["nok_sim_status"]    = result.get("sim_status")
        base["nok_iccid"]         = result.get("iccid")
        base["nok_imsi"]          = result.get("imsi")

        if not result.get("ok"):
            base["nok_error"]           = result.get("error", "Unknown error")
            base["nok_validation"]      = "❌"
            base["nok_field_to_review"] = "Nokia_Error"
            return base

        # No validation rules yet — data collection only
        base["nok_field_to_review"] = "No_Action_Required"
        base["nok_validation"]      = "✅"
        return base

    except Exception as e:
        base["nok_error"]           = str(e)
        base["nok_validation"]      = "❌"
        base["nok_field_to_review"] = "Nokia_Error"
        return base
