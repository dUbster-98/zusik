from __future__ import annotations
"""HTML 파일 → PDF 변환 — 시스템에 있는 백엔드 자동 감지.

우선순위: 헤드리스 Chrome/Chromium(`--print-to-pdf`) → wkhtmltopdf.
백엔드가 없으면 None 반환(HTML 은 그대로 유효 — 무예외 fail-safe). 새 파이썬 의존성 0.
Chrome 헤드리스는 시스템 폰트(Noto CJK 등)로 렌더하므로 한글이 깨지지 않는다.
"""

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_CHROME_BINS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _find(bins) -> str:
    for b in bins:
        p = shutil.which(b)
        if p:
            return p
    return ""


def pdf_backend() -> str:
    """사용 가능한 변환 백엔드 이름('chrome'|'wkhtmltopdf') 또는 ''(없음)."""
    if _find(_CHROME_BINS):
        return "chrome"
    if shutil.which("wkhtmltopdf"):
        return "wkhtmltopdf"
    return ""


def html_to_pdf(html_path: str, pdf_path: str, timeout: int = 90):
    """html_path 를 pdf_path 로 변환. 성공 시 pdf_path, 백엔드 없음/실패 시 None(무예외)."""
    try:
        html_path = os.path.abspath(html_path)
        if not os.path.exists(html_path):
            return None
        out_dir = os.path.dirname(pdf_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        chrome = _find(_CHROME_BINS)
        if chrome:
            prof = tempfile.mkdtemp(prefix="zusik_pdf_")
            try:
                cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                       "--no-first-run", f"--user-data-dir={prof}",
                       "--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}",
                       f"file://{html_path}"]
                subprocess.run(cmd, capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            finally:
                shutil.rmtree(prof, ignore_errors=True)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                return pdf_path
            logger.warning("Chrome PDF 변환 실패 — HTML 만 유지")
            return None

        wk = shutil.which("wkhtmltopdf")
        if wk:
            subprocess.run([wk, "-q", html_path, pdf_path], capture_output=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                return pdf_path
            return None

        logger.info("PDF 변환 백엔드 없음(chrome/chromium/wkhtmltopdf) — HTML 만 생성")
        return None
    except Exception as e:
        logger.warning("HTML→PDF 변환 예외: %s", e)
        return None
