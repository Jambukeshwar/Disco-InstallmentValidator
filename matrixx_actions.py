"""
Matrixx action executor for Disconnect Validation Suite.
Provides one corrective action callable from the drill-down modal:
  1. deactivate_device — MtxRequestMulti (DeviceModify status=2 + DeviceDelete)
"""

import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

MATRIXX_BASE_URL = os.getenv("MATRIXX_BASE_URL", "http://localhost:8080/rsgateway/data/json")

HEADERS = {"Content-Type": "application/json"}
TIMEOUT = 30


def deactivate_device(msisdn: str, channel: str) -> dict:
    """
    Deactivate and delete a Matrixx device by MSISDN.
    Uses MtxRequestMulti — first modifies device status to 2 (inactive),
    then deletes the device session.

    Args:
        msisdn  : The subscriber MSISDN
        channel : Caller name used as the Channel field (alt_caller_id equivalent)
    """
    payload = {
        "$": "MtxRequestMulti",
        "ApiEventData": {
            "$": "LLAApiEventdata",
            "TransactionId": "ITOPS",
            "Channel": channel
        },
        "RequestList": [
            {
                "$": "MtxRequestDeviceModify",
                "Status": "2",
                "DeviceSearchData": {
                    "$": "MtxDeviceSearchData",
                    "AccessNumber": msisdn
                }
            },
            {
                "$": "MtxRequestDeviceDelete",
                "DeleteSession": "True",
                "DeviceSearchData": {
                    "$": "MtxDeviceSearchData",
                    "MultiRequestIndex": "0"
                }
            }
        ]
    }

    response = requests.post(
        MATRIXX_BASE_URL,
        headers=HEADERS,
        data=json.dumps(payload),
        timeout=TIMEOUT
    )
    response.raise_for_status()
    result = response.json()

    # Check for Matrixx error response
    result_text = result.get("_resultText", "") or result.get("ResultText", "")
    if result_text and result_text.upper() != "OK":
        raise ValueError(f"Matrixx deactivate failed: {result_text}")

    return result