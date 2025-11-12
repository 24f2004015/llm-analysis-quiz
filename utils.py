# utils.py
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

import pdfplumber
import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger(__name__)


def download_file(url: str, dest_dir: Path) -> str:
    """
    Download a file (supports data: URIs as well) to dest_dir and return local file path.
    dest_dir must exist.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("Downloading %s to %s", url, dest_dir)
    if url.startswith("data:"):
        # data URI
        header, b64 = url.split(",", 1)
        import base64

        # find extension if present
        m = re.search(r"data:(?P<mime>[^;]+)", header)
        mime = m.group("mime") if m else "application/octet-stream"
        ext = "bin"
        if "/" in mime:
            ext = mime.split("/")[1]
        fname = dest_dir / f"downloaded.{ext}"
        with open(fname, "wb") as f:
            f.write(base64.b64decode(b64))
        return str(fname)

    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    cd = r.headers.get("content-disposition")
    if cd:
        m = re.search(r'filename="?([^"]+)"?', cd)
        if m:
            fname = m.group(1)
        else:
            fname = url.split("/")[-1]
    else:
        fname = url.split("/")[-1] or "downloaded"
    fpath = dest_dir / fname
    with open(fpath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return str(fpath)


def extract_text_from_pdf_pages(pdf_path: str, pages: Optional[List[int]] = None) -> str:
    """
    Extract text from provided PDF pages (1-based indexing).
    If pages is None -> return whole text.
    """
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        if pages is None:
            pages_iter = range(len(pdf.pages))
        else:
            # convert to 0-based indices
            pages_iter = [p - 1 for p in pages if 1 <= p <= len(pdf.pages)]
        for i in pages_iter:
            try:
                pg = pdf.pages[i]
                text_parts.append(pg.extract_text() or "")
            except Exception as e:
                LOG.warning("PDF page extraction failed for %s page %s: %s", pdf_path, i, e)
    return "\n".join(text_parts)


def try_parse_number_from_text(text: str):
    """
    Try to locate a likely number (sum) in the provided text.
    Returns int/float or None.
    Strategy: find all numbers and if there's a clue like 'sum' or 'total' return the largest.
    """
    if not text:
        return None
    # find numbers, including decimals and commas
    nums = re.findall(r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+|\d+", text.replace("\u2013", "-"))
    parsed = []
    for s in nums:
        s2 = s.replace(",", "")
        try:
            if "." in s2:
                parsed.append(float(s2))
            else:
                parsed.append(int(s2))
        except Exception:
            continue
    if not parsed:
        return None
    # If text mentions 'sum' or 'total', choose the largest; else return the first reasonable number
    if re.search(r"\b(sum|total|subtotal|aggregate|answer)\b", text, flags=re.I):
        return max(parsed)
    return parsed[0]


def parse_submit_instruction(html: str, visible_text: str):
    """
    Try to find a submit URL and extra instructions encoded in the page.
    Returns dict with keys like submit_url, method, notes.
    """
    soup = BeautifulSoup(html, "html.parser")
    # look for explicit endpoints in script tags or visible text, often /submit endpoints
    m = re.search(r"(https?://[^\s'\"<>]+/submit[^\s'\"<>]*)", html, flags=re.I)
    submit_url = m.group(1) if m else None

    # Sometimes the submit endpoint is in JSON embedded in <pre> or script.
    pre = soup.find("pre")
    if pre:
        try:
            obj = json.loads(pre.text)
            # sample may have "submit" url nested in metadata; scan values
            for k, v in obj.items():
                if isinstance(v, str) and "/submit" in v:
                    submit_url = submit_url or v
        except Exception:
            pass

    # If not found, search visible_text
    if not submit_url:
        m2 = re.search(r"(https?://[^\s'\"<>]+/submit[^\s'\"<>]*)", visible_text, flags=re.I)
        if m2:
            submit_url = m2.group(1)

    return {"submit_url": submit_url}


def pretty_json(obj):
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return str(obj)
