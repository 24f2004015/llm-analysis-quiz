# solver.py
import json
import logging
import os
import re
import shutil
import tempfile
import time
import base64
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from utils import (
    download_file,
    extract_text_from_pdf_pages,
    try_parse_number_from_text,
    parse_submit_instruction,
    pretty_json,
)

LOG = logging.getLogger(__name__)


# -----------------------
# Helper utilities
# -----------------------
def extract_base64_from_page_html(html_text: str) -> Optional[str]:
    """
    Finds atob(...) base64 payload in page HTML/JS and returns decoded string.
    Supports backtick, double-quote and single-quote forms.
    """
    if not html_text:
        return None
    m = re.search(r'atob\(\s*`([^`]+)`\s*\)', html_text, flags=re.DOTALL)
    if not m:
        m = re.search(r'atob\(\s*"([^"]+)"\s*\)', html_text, flags=re.DOTALL)
    if not m:
        m = re.search(r'atob\(\s*\'([^\']+)\'\s*\)', html_text, flags=re.DOTALL)
    if not m:
        return None
    payload_b64 = m.group(1)
    try:
        decoded = base64.b64decode(payload_b64).decode("utf-8", errors="ignore")
        return decoded
    except Exception:
        LOG.exception("Failed to decode base64 from atob payload")
        return None


def find_json_in_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Attempts to locate and parse the first JSON-like object {...} in text.
    Returns dict or None.
    """
    if not text:
        return None
    m = re.search(r'(\{[\s\S]*\})', text)
    if not m:
        return None
    candidate = m.group(1)
    try:
        return json.loads(candidate)
    except Exception:
        # try some cleanup attempts (strip trailing commas)
        cleaned = re.sub(r",\s*}", "}", candidate)
        cleaned = re.sub(r",\s*]", "]", cleaned)
        try:
            return json.loads(cleaned)
        except Exception:
            LOG.exception("Failed to parse JSON from text")
            return None


def download_file_to_bytes(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def sum_csv_value_column_from_bytes(bts: bytes) -> Optional[float]:
    """
    Try to parse CSV from bytes and return sum of 'value' column (case-insensitive)
    or the first numeric column.
    """
    import pandas as pd

    try:
        df = pd.read_csv(io.BytesIO(bts))
    except Exception:
        try:
            df = pd.read_csv(io.StringIO(bts.decode("utf-8", errors="ignore")))
        except Exception:
            LOG.exception("Failed to parse CSV from bytes")
            return None

    # prefer 'value' column
    for col in df.columns:
        if str(col).strip().lower() == "value":
            try:
                return float(df[col].dropna().astype(float).sum())
            except Exception:
                continue

    # otherwise pick first numeric column
    try:
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols:
            return float(df[numeric_cols[0]].dropna().astype(float).sum())
    except Exception:
        LOG.exception("Failed to compute numeric column sum from CSV")

    # last resort: coerce first column to numeric
    try:
        nums = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
        return float(nums.sum())
    except Exception:
        return None


def sum_pdf_value_like_from_bytes(bts: bytes) -> Optional[float]:
    """
    Heuristic PDF parser: tries to extract numbers and table cells from a PDF
    and returns the largest sensible sum found.
    """
    try:
        import pdfplumber
        import pandas as pd
    except Exception:
        LOG.exception("pdfplumber/pandas not available to parse PDF")
        return None

    try:
        with pdfplumber.open(io.BytesIO(bts)) as pdf:
            sums = []
            for page in pdf.pages:
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []
                # try table sums
                for table in tables:
                    try:
                        if not table or len(table) < 2:
                            continue
                        df = pd.DataFrame(table[1:], columns=table[0])
                        for col in df.columns:
                            try:
                                s = pd.to_numeric(df[col], errors="coerce").dropna().astype(float).sum()
                                if s != 0:
                                    sums.append(s)
                            except Exception:
                                continue
                    except Exception:
                        continue
                # fallback: sum numbers from page text
                try:
                    text = page.extract_text() or ""
                    nums = re.findall(r'[-+]?[0-9]*\.?[0-9]+', text)
                    nums = [float(n) for n in nums] if nums else []
                    if nums:
                        sums.append(sum(nums))
                except Exception:
                    continue
            if sums:
                # heuristically pick the max sum
                return float(max(sums))
    except Exception:
        LOG.exception("Failed to parse PDF bytes")
    return None


def compute_answer_from_page_content(page, page_html: Optional[str] = None) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Attempt to compute an answer given a Playwright page and optional HTML for #result.
    Returns (answer_or_None, debug_info)
    """
    debug: Dict[str, Any] = {"steps": []}
    try:
        if page_html is None:
            try:
                el = page.query_selector("#result")
                page_html = el.inner_html() if el else page.content()
            except Exception:
                page_html = page.content()

        debug["got_page_html"] = True

        # Try decode atob payload
        decoded = extract_base64_from_page_html(page_html)
        if decoded:
            debug["steps"].append("decoded_atob")
            debug["decoded_snippet"] = decoded[:500]
            parsed = find_json_in_text(decoded)
            if parsed:
                debug["steps"].append("found_json_in_decoded")
                # if JSON already contains answer field, use it
                if "answer" in parsed and parsed["answer"] is not None:
                    return parsed["answer"], {"debug": debug, "parsed": parsed}
                # if JSON includes url to asset, try to download and compute
                if "url" in parsed:
                    file_url = parsed["url"]
                    debug["steps"].append("decoded_json_has_url")
                    try:
                        bts = download_file_to_bytes(file_url)
                        if file_url.lower().endswith(".csv"):
                            ans = sum_csv_value_column_from_bytes(bts)
                            if ans is not None:
                                return ans, {"debug": debug, "parsed": parsed}
                        if file_url.lower().endswith(".pdf"):
                            ans = sum_pdf_value_like_from_bytes(bts)
                            if ans is not None:
                                return ans, {"debug": debug, "parsed": parsed}
                        # generic attempt
                        ans = sum_csv_value_column_from_bytes(bts)
                        if ans is not None:
                            return ans, {"debug": debug, "parsed": parsed}
                    except Exception:
                        LOG.exception("Failed to download/compute from decoded url")

            # find raw URLs in decoded text
            urls = re.findall(r'https?://[^\s\'"<>]+', decoded)
            for u in urls:
                debug["steps"].append("found_url_in_decoded")
                try:
                    bts = download_file_to_bytes(u)
                    if u.lower().endswith(".csv"):
                        ans = sum_csv_value_column_from_bytes(bts)
                        if ans is not None:
                            return ans, {"debug": debug, "url": u}
                    if u.lower().endswith(".pdf"):
                        ans = sum_pdf_value_like_from_bytes(bts)
                        if ans is not None:
                            return ans, {"debug": debug, "url": u}
                except Exception:
                    continue

        # Fallback: inspect visible text for numbers and simple Q/A
        try:
            body_text = page.inner_text("body") or ""
        except Exception:
            body_text = page.content() or ""
        debug["body_excerpt"] = (body_text[:1000] + "...") if body_text else ""
        # collect numbers from visible text
        nums = re.findall(r'[-+]?[0-9]*\.?[0-9]+', body_text)
        if nums:
            debug["steps"].append("fallback_sum_numbers_in_body")
            nums = [float(n) for n in nums]
            return float(sum(nums)), {"debug": debug}

    except Exception:
        LOG.exception("compute_answer_from_page_content failed")
        debug["error"] = "exception"

    return None, {"debug": debug}


# -----------------------
# Main solver class
# -----------------------
class QuizSolver:
    def __init__(self, log_dir: Path = Path("logs")):
        self.log_dir = log_dir
        self.temp_root = Path(tempfile.gettempdir()) / "llm_quiz"
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entrypoint.
        Expects payload containing "email","secret","url".
        Returns a dict with status and debug info.
        """
        email = payload["email"]
        url = payload["url"]
        started = datetime.utcnow()
        deadline = started + timedelta(seconds=160)  # keep margin under 3 minutes

        debug: Dict[str, Any] = {"steps": []}

        LOG.info("Solver starting for %s -> %s", email, url)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(120000)  # 120s default for operations

                LOG.info("Navigating to %s", url)
                page.goto(url, wait_until="networkidle")

                # snapshot html/text
                html = page.content()
                text = ""
                try:
                    text = page.inner_text("body")
                except Exception:
                    text = ""

                debug["steps"].append("page_loaded")

                # parse submit instruction if present
                submit_info = parse_submit_instruction(html, text)
                debug["submit_info"] = submit_info

                # attempt to compute an answer using robust helper
                page_html_for_result = None
                try:
                    el = page.query_selector("#result")
                    if el:
                        page_html_for_result = el.inner_html()
                except Exception:
                    page_html_for_result = None

                answer, info = compute_answer_from_page_content(page, page_html=page_html_for_result)
                debug["compute_info"] = info
                if answer is not None:
                    debug["steps"].append("answer_computed")
                    debug["answer_value"] = answer
                else:
                    debug["steps"].append("no_answer_computed")

                # If that failed, try fallback heuristics you had previously (CSV/PDF links, tables etc.)
                candidate_answer = None
                if answer is not None:
                    candidate_answer = answer
                else:
                    # previous heuristics (pdf/csv links)
                    soup = BeautifulSoup(html, "html.parser")
                    links = [a.get("href") for a in soup.find_all("a", href=True)]
                    pdf_links = [l for l in links if l and l.lower().endswith(".pdf")]
                    csv_links = [l for l in links if l and l.lower().endswith(".csv")]

                    if pdf_links or csv_links:
                        debug["steps"].append("found_assets")
                        asset_url = (pdf_links + csv_links)[0]
                        LOG.info("Found asset %s", asset_url)
                        try:
                            tmpdir = Path(tempfile.mkdtemp(prefix="llmquiz_"))
                            out = download_file(asset_url, tmpdir)
                            debug["downloaded"] = out
                            if out.lower().endswith(".pdf"):
                                extracted = extract_text_from_pdf_pages(out, pages=[2])
                                debug["pdf_page2_text"] = extracted[:2000]
                                num = try_parse_number_from_text(extracted)
                                if num is not None:
                                    candidate_answer = num
                                    debug["answer_source"] = "pdf_infer_number"
                            elif out.lower().endswith(".csv"):
                                import pandas as pd

                                df = pd.read_csv(out)
                                candidate = None
                                for c in df.columns:
                                    if c.lower() == "value":
                                        candidate = c
                                        break
                                if candidate is None:
                                    numeric_cols = df.select_dtypes(include="number").columns.tolist()
                                    if numeric_cols:
                                        candidate = numeric_cols[0]
                                if candidate:
                                    candidate_answer = float(df[candidate].sum())
                                    debug["answer_source"] = f"csv_sum:{candidate}"
                        except Exception:
                            LOG.exception("Failed to download/process asset")
                        finally:
                            # keep file for debugging
                            pass
                    else:
                        # DOM table heuristic
                        tables = page.query_selector_all("table")
                        if tables:
                            debug["steps"].append("dom_table_detected")
                            try:
                                html_table = tables[0].inner_html()
                                import pandas as pd

                                dfs = pd.read_html(f"<table>{html_table}</table>")
                                if dfs:
                                    df = dfs[0]
                                    candidate = None
                                    for c in df.columns:
                                        if str(c).lower() == "value":
                                            candidate = c
                                            break
                                    if candidate is None:
                                        numcols = df.select_dtypes(include="number").columns.tolist()
                                        if numcols:
                                            candidate = numcols[0]
                                    if candidate is not None:
                                        candidate_answer = float(df[candidate].sum())
                                        debug["answer_source"] = f"dom_table_sum:{candidate}"
                            except Exception:
                                LOG.warning("Failed parsing table")
                                candidate_answer = None
                        else:
                            # textual inference heuristics
                            debug["steps"].append("text_inference")
                            m = re.search(
                                r"sum of the [\"']?(?P<col>[A-Za-z0-9 _-]+)[\"']? column.*page\s*(?P<page>\d+)",
                                text,
                                flags=re.IGNORECASE | re.DOTALL,
                            )
                            if m:
                                col = m.group("col")
                                pg = int(m.group("page"))
                                debug["inferred_col"] = col
                                debug["inferred_page"] = pg
                                if pdf_links:
                                    try:
                                        out = download_file(pdf_links[0], Path(tempfile.mkdtemp()))
                                        extracted = extract_text_from_pdf_pages(out, pages=[pg])
                                        num = try_parse_number_from_text(extracted)
                                        if num is not None:
                                            candidate_answer = num
                                            debug["answer_source"] = f"pdf_page{pg}:{col}:approx"
                                    except Exception:
                                        pass
                            else:
                                if re.search(r"\btrue or false\b", text, flags=re.I):
                                    candidate_answer = True  # fallback guess

                debug["attempted_answer"] = candidate_answer

                # find submit URL
                submit_url = None
                if submit_info and submit_info.get("submit_url"):
                    submit_url = submit_info["submit_url"]
                else:
                    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", html, flags=re.I)
                    if m:
                        submit_url = m.group(0)

                debug["submit_url"] = submit_url

                result = {"status": "no_action", "debug": debug}

                if submit_url:
                    submit_payload = {
                        "email": email,
                        "secret": payload.get("secret"),
                        "url": url,
                        "answer": candidate_answer,
                    }

                    # Only submit if we have a non-null answer
                    if candidate_answer is None:
                        LOG.warning("No answer computed; skipping submit to avoid 400. Debug: %s", pretty_json(debug))
                        result = {"status": "no_answer", "debug": debug}
                    else:
                        LOG.info("Submitting answer to %s payload=%s", submit_url, pretty_json(submit_payload))
                        try:
                            r = requests.post(submit_url, json=submit_payload, timeout=60)
                            debug["submit_status_code"] = r.status_code
                            try:
                                debug["submit_response"] = r.json()
                            except Exception:
                                debug["submit_response_text"] = r.text[:2000]
                            result = {"status": "submitted", "submit_code": r.status_code, "debug": debug}
                        except Exception as e:
                            LOG.exception("Failed to submit to %s: %s", submit_url, e)
                            result = {"status": "submit_failed", "debug": debug}
                else:
                    LOG.warning("No submit URL found; returning debug info")
                    result = {"status": "no_submit_url", "debug": debug}

                # cleanup
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass

                return result
        except PlaywrightTimeoutError as e:
            LOG.exception("Playwright timeout: %s", e)
            return {"status": "playwright_timeout", "error": str(e)}
        except Exception as e:
            LOG.exception("Solver error: %s", e)
            return {"status": "error", "error": str(e)}
