"""Parse AWS credentials from various copy-paste formats.

Supported formats:
  1. export AWS_ACCESS_KEY_ID=...        (Linux/Mac shell)
  2. set AWS_ACCESS_KEY_ID=...           (Windows cmd)
  3. $env:AWS_ACCESS_KEY_ID="..."        (PowerShell)
  4. aws_access_key_id=...               (AWS config/credentials file)
  5. [profile] header lines are ignored
"""
from __future__ import annotations

import re


def parse_credentials(text: str) -> dict[str, str]:
    """Extract AWS creds from pasted text in any common format.

    Returns dict with keys: access_key_id, secret_access_key, session_token.
    Missing keys are omitted.
    """
    result: dict[str, str] = {}

    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("#"):
            continue

        m = re.match(
            r'(?:export\s+|set\s+|\$env:)?'
            r'(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN'
            r'|aws_access_key_id|aws_secret_access_key|aws_session_token)'
            r'\s*=\s*"?(.*?)"?\s*$',
            line,
            re.IGNORECASE,
        )
        if not m:
            continue

        key_raw = m.group(1).upper()
        value = m.group(2).strip().strip('"').strip("'")

        if "ACCESS_KEY_ID" in key_raw and "SECRET" not in key_raw:
            result["access_key_id"] = value
        elif "SECRET_ACCESS_KEY" in key_raw:
            result["secret_access_key"] = value
        elif "SESSION_TOKEN" in key_raw:
            result["session_token"] = value

    return result
