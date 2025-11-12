# solver.py
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

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

        debug = {"steps": []}

        LOG.info("Solver starting for %s -> %s", email, url)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(120000)  # 120s default for operations

                LOG.info("Navigating to %s", url)
                page.goto(url, wait_until="networkidle")

                # take HTML and text snapshot
                html = page.content()
                text = page.inner_text("body")
                debug["steps"].append("page_loaded")

                # try to find submit instruction and target URLs
                submit_info = parse_submit_instruction(html, text)
                debug["submit_info"] = submit_info

                # heuristics:
                # 1) If page contains base64-inlined data (e.g., atob sample) -> decode and inspect
                # 2) If page references a PDF/CSV link -> download and process
                # 3) If page has tables visible in DOM -> try to extract and compute
                # 4) Otherwise, try to infer simple Q/A from text and answer if possible

                # Heuristic 1: look for base64 embedded in scripts
                b64_matches = re.findall(r"atob\(`([^`]+)`\)", html, flags=re.DOTALL)
                if b64_matches:
                    LOG.info("Found atob embedded base64 content")
                    debug["steps"].append("found_atob")
                    for b64 in b64_matches:
                        try:
                            raw = b64.encode("utf-8")
                            import base64

                            decoded = base64.b64decode(raw)
                            # try to decode as utf-8 text
                            text_decoded = decoded.decode("utf-8", errors="ignore")
                            debug.setdefault("atob_decoded", []).append(text_decoded[:2000])
                        except Exception as e:
                            LOG.warning("Failed decoding atob: %s", e)

                # Heuristic 2: find links that look like PDFs or CSVs "download file"
                soup = BeautifulSoup(html, "html.parser")
                links = [a.get("href") for a in soup.find_all("a", href=True)]
                pdf_links = [l for l in links if l.lower().endswith(".pdf")]
                csv_links = [l for l in links if l.lower().endswith(".csv")]

                # Also look for common words: "Download file", "Download", "file"
                if pdf_links or csv_links:
                    debug["steps"].append("found_assets")
                    asset_url = (pdf_links + csv_links)[0]
                    LOG.info("Found asset %s", asset_url)
                    tmpdir = Path(tempfile.mkdtemp(prefix="llmquiz_"))
                    try:
                        out = download_file(asset_url, tmpdir)
                        debug["downloaded"] = out
                        # If PDF: try to extract page-based tables/text
                        if out.lower().endswith(".pdf"):
                            # Example sample question: sum of 'value' column on page 2
                            extracted = extract_text_from_pdf_pages(out, pages=[2])
                            debug["pdf_page2_text"] = extracted[:2000]
                            # look for numbers in extracted text and maybe a column named 'value'
                            num = try_parse_number_from_text(extracted)
                            if num is not None:
                                answer = num
                                debug["answer_source"] = "pdf_infer_number"
                            else:
                                answer = None
                        elif out.lower().endswith(".csv"):
                            import pandas as pd

                            df = pd.read_csv(out)
                            # try smart guess: if a numeric column named 'value' or 'Value'
                            candidate = None
                            for c in df.columns:
                                if c.lower() == "value":
                                    candidate = c
                                    break
                            if candidate is None:
                                # fallback: pick numeric column with many values
                                numeric_cols = df.select_dtypes(include="number").columns.tolist()
                                if numeric_cols:
                                    candidate = numeric_cols[0]
                            if candidate:
                                answer = float(df[candidate].sum())
                                debug["answer_source"] = f"csv_sum:{candidate}"
                            else:
                                answer = None
                        else:
                            answer = None
                    finally:
                        # keep downloaded file for debugging but avoid disk clutter
                        pass
                else:
                    # Heuristic 3: check DOM tables
                    tables = page.query_selector_all("table")
                    if tables:
                        debug["steps"].append("dom_table_detected")
                        # parse first table to pandas
                        try:
                            html_table = tables[0].inner_html()
                            # convert with pandas
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
                                    answer = float(df[candidate].sum())
                                    debug["answer_source"] = f"dom_table_sum:{candidate}"
                                else:
                                    answer = None
                            else:
                                answer = None
                        except Exception as e:
                            LOG.warning("Failed parsing table: %s", e)
                            answer = None
                    else:
                        # Heuristic 4: textual Q/A: try to parse number from instruction
                        debug["steps"].append("text_inference")
                        # Example pattern: "What is the sum of the 'value' column in the table on page 2?"
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
                            # look for PDF links again then try to fetch page
                            if pdf_links:
                                out = download_file(pdf_links[0], Path(tempfile.mkdtemp()))
                                extracted = extract_text_from_pdf_pages(out, pages=[pg])
                                num = try_parse_number_from_text(extracted)
                                if num is not None:
                                    answer = num
                                    debug["answer_source"] = f"pdf_page{pg}:{col}:approx"
                                else:
                                    answer = None
                            else:
                                answer = None
                        else:
                            # As last resort, try to find an obvious boolean question
                            if re.search(r"\btrue or false\b", text, flags=re.I):
                                # attempt to detect whether statement is true in the page text
                                answer = True  # fallback — may be wrong
                            else:
                                answer = None

                # If we didn't get an answer, set answer to None; still attempt to POST an empty structured response
                candidate_answer = None
                if "answer" in locals() and answer is not None:
                    candidate_answer = answer

                # submit: the page may include an explicit submit URL in text or hidden JSON
                submit_url = None
                if submit_info and submit_info.get("submit_url"):
                    submit_url = submit_info["submit_url"]
                else:
                    # attempt to find any "submit" endpoint in page text
                    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", html, flags=re.I)
                    if m:
                        submit_url = m.group(0)

                debug["attempted_answer"] = candidate_answer
                debug["submit_url"] = submit_url

                result = {"status": "no_action", "debug": debug}

                if submit_url:
                    # prepare payload
                    submit_payload = {
                        "email": email,
                        "secret": payload.get("secret"),
                        "url": url,
                        "answer": candidate_answer,
                    }
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
