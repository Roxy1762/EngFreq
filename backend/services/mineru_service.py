"""MinerU lightweight agent API integration for local file parsing."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from backend.services.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)


def parse_file(path: Path) -> Dict[str, Any]:
    """Upload a local file to MinerU and return markdown plus raw responses."""
    config = get_runtime_config()
    mineru = config.mineru
    base_url = mineru.api_base.rstrip("/")

    payload: Dict[str, Any] = {
        "file_name": path.name,
        "language": mineru.language,
        "enable_table": mineru.enable_table,
        "is_ocr": mineru.is_ocr,
        "enable_formula": mineru.enable_formula,
    }
    if mineru.page_range:
        payload["page_range"] = mineru.page_range

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        submit_resp = client.post(f"{base_url}/parse/file", json=payload)
        submit_resp.raise_for_status()
        submit_data = submit_resp.json()
        if submit_data.get("code") != 0:
            raise RuntimeError(f"MinerU submit failed: {submit_data.get('msg', 'unknown error')}")

        task_id = submit_data["data"]["task_id"]
        file_url = submit_data["data"]["file_url"]

        with path.open("rb") as fh:
            put_resp = client.put(file_url, content=fh.read())
        if put_resp.status_code not in (200, 201):
            raise RuntimeError(f"MinerU upload failed: HTTP {put_resp.status_code}")

        start = time.time()
        poll_data: Dict[str, Any] | None = None
        while time.time() - start < mineru.poll_timeout_sec:
            poll_resp = client.get(f"{base_url}/parse/{task_id}")
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
            if poll_data.get("code") != 0:
                raise RuntimeError(f"MinerU polling failed: {poll_data.get('msg', 'unknown error')}")

            state = poll_data.get("data", {}).get("state")
            if state == "done":
                markdown_url = poll_data["data"]["markdown_url"]
                markdown_resp = client.get(markdown_url)
                markdown_resp.raise_for_status()
                markdown_text = markdown_resp.text
                return {
                    "backend": "mineru",
                    "text": markdown_text,
                    "used_ocr": bool(mineru.is_ocr),
                    "raw_result": {
                        "service": "mineru",
                        "mode": "agent_file",
                        "submit_response": submit_data,
                        "result_response": poll_data,
                        "markdown_url": markdown_url,
                        "markdown_text": markdown_text,
                    },
                }

            if state == "failed":
                err_msg = poll_data.get("data", {}).get("err_msg", "unknown error")
                err_code = poll_data.get("data", {}).get("err_code")
                raise RuntimeError(f"MinerU parse failed ({err_code}): {err_msg}")

            time.sleep(mineru.poll_interval_sec)

        raise TimeoutError(
            f"MinerU polling timed out after {mineru.poll_timeout_sec}s for task {task_id}"
        )
