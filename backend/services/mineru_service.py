"""MinerU lightweight agent v4 API integration for local file parsing."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from backend.services.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)


def parse_file(path: Path) -> Dict[str, Any]:
    """Upload a local file to MinerU (v4 lightweight agent) and return markdown."""
    config = get_runtime_config()
    mineru = config.mineru
    base_url = mineru.api_base.rstrip("/")

    api_key = mineru.api_key
    if not api_key:
        raise RuntimeError("MinerU api_key is not configured. Set it in the admin panel under 'MinerU 配置'.")

    auth_headers = {"Authorization": f"Bearer {api_key}"}

    # Step 1: request a pre-signed upload URL
    batch_payload: Dict[str, Any] = {
        "files": [
            {
                "name": path.name,
                "is_ocr": mineru.is_ocr,
                "data_id": path.stem,
            }
        ],
        "enable_formula": mineru.enable_formula,
        "enable_table": mineru.enable_table,
        "language": mineru.language,
    }
    if mineru.page_range:
        batch_payload["page_ranges"] = [mineru.page_range]

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        submit_resp = client.post(
            f"{base_url}/file-urls/batch",
            json=batch_payload,
            headers=auth_headers,
        )
        submit_resp.raise_for_status()
        submit_data = submit_resp.json()

        code = submit_data.get("code")
        if code not in (0, 200, "0", "200"):
            raise RuntimeError(f"MinerU submit failed: {submit_data.get('msg', submit_data)}")

        batch_id = submit_data["data"]["batch_id"]
        file_url = submit_data["data"]["files"][0]["url"]

        # Step 2: upload file to pre-signed S3 URL (no auth header needed)
        with path.open("rb") as fh:
            put_resp = client.put(
                file_url,
                content=fh.read(),
                headers={"Content-Type": "application/octet-stream"},
            )
        if put_resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"MinerU S3 upload failed: HTTP {put_resp.status_code}")

        # Step 3: poll for results
        start = time.time()
        poll_data: Dict[str, Any] | None = None
        while time.time() - start < mineru.poll_timeout_sec:
            poll_resp = client.get(
                f"{base_url}/extract-results/{batch_id}",
                headers=auth_headers,
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()

            poll_code = poll_data.get("code")
            if poll_code not in (0, 200, "0", "200"):
                raise RuntimeError(f"MinerU polling failed: {poll_data.get('msg', 'unknown error')}")

            extract_list = poll_data.get("data", {}).get("extract_result", [])
            if not extract_list:
                time.sleep(mineru.poll_interval_sec)
                continue

            item = extract_list[0]
            state = item.get("state")

            if state == "done":
                markdown_url = item.get("full_zip_url") or item.get("markdown_url") or ""
                # Try to get plain markdown text URL first
                if not markdown_url:
                    raise RuntimeError("MinerU returned done state but no download URL")

                markdown_resp = client.get(markdown_url)
                markdown_resp.raise_for_status()

                # If it's a zip, we'd need to unzip — but mineru v4 often provides
                # per-file markdown URLs in result_files list
                result_files = item.get("result_files", [])
                md_text = ""
                for rf in result_files:
                    if rf.get("type") == "md" or rf.get("name", "").endswith(".md"):
                        md_resp = client.get(rf["url"])
                        md_resp.raise_for_status()
                        md_text = md_resp.text
                        break

                if not md_text:
                    # full_zip_url is a zip; we return the raw response text as best effort
                    md_text = markdown_resp.text

                return {
                    "backend": "mineru",
                    "text": md_text,
                    "used_ocr": bool(mineru.is_ocr),
                    "raw_result": {
                        "service": "mineru",
                        "mode": "agent_v4",
                        "batch_id": batch_id,
                        "submit_response": submit_data,
                        "result_response": poll_data,
                    },
                }

            if state in ("failed", "error"):
                err_msg = item.get("err_msg", "unknown error")
                raise RuntimeError(f"MinerU parse failed: {err_msg}")

            time.sleep(mineru.poll_interval_sec)

        raise TimeoutError(
            f"MinerU polling timed out after {mineru.poll_timeout_sec}s for batch {batch_id}"
        )
