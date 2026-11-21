"""Text helpers: HTML stripping, PII scrubbing, line / replacement-bus extraction."""
from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/div|/li)\s*/?>", re.I)
_WS_RE = re.compile(r"[ \t ]+")

# PII patterns (staff data must never reach the passenger view or the LLM)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?<![\w/])(?:\+|00)\s?\d{2}[\s./-]?(?:\(0\)\s?)?\d{2}[\s./-]?\d{3}[\s./-]?\d{2}[\s./-]?\d{2}(?!\d)"  # +41 79 123 45 67
    r"|(?<![\w/])0\d{2}[\s./-]?\d{3}[\s./-]?\d{2}[\s./-]?\d{2}(?!\d)"  # 079 123 45 67
    r"|(?<![\w/])0\d{9}(?!\d)"  # 0791234567
)
STAFF_ID_RE = re.compile(r"\b[ue]\d{6}\b")  # SBB personnel numbers like u238251
CONTACT_TAIL_RE = re.compile(r"(?i)\b(tel\.?|telefon|natel|handy|kontakt|contact|ansprechperson|personne de contact)\s*[:.]?\s*[^\n]*")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_PUBLIC_URL_HOSTS = ("www.sbb.ch", "sbb.ch", "www.swisspass.ch", "swisspass.ch", "www.cff.ch", "www.ffs.ch")

BUS_KEYWORDS = (
    "bahnersatz", "ersatzbus", "ersatzbusse", "busersatz", "bus de remplacement", "bus de substitution",
    "bus sostitutiv", "autobus sostitutiv", "replacement bus", "rail replacement", "bus replacement",
)

LINE_RE = re.compile(r"\b(ICE|ICN|IC|IR|RE|EC|EN|NJ|TGV|TER|RL|SN|PE|S|R)\s?(\d{1,3})\b")
LINE_SOLO_RE = re.compile(r"\b(ICE|EC|TGV|NJ|EN)\b")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    s = _BR_RE.sub("\n", str(text))
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _scrub_url(m: re.Match) -> str:
    url = m.group(0)
    host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
    return url if host in _PUBLIC_URL_HOSTS else "[internal link removed]"


def scrub_pii(text: str | None) -> str:
    """Remove e-mail addresses, phone numbers, staff ids, contact lines and internal links."""
    if not text:
        return ""
    s = EMAIL_RE.sub("[email removed]", text)
    s = CONTACT_TAIL_RE.sub("[contact removed]", s)
    s = PHONE_RE.sub("[phone removed]", s)
    s = STAFF_ID_RE.sub("[staff]", s)
    s = URL_RE.sub(_scrub_url, s)
    return s


def sanitize_operational(text: str | None, max_len: int = 600) -> str:
    s = scrub_pii(strip_html(text))
    s = re.sub(r"\s*\n\s*", " / ", s).strip(" /")
    return s[:max_len]


def mentions_bus(*texts: str | None) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    if any(k in joined for k in BUS_KEYWORDS):
        return True
    # "(EV)" = Ersatzverkehr in SBB texts
    return bool(re.search(r"\(EV\)|\bErsatzverkehr\b|\bBusse?\b.{0,40}\b(vor Ort|im Einsatz|bestellt|unterwegs)", " ".join(t for t in texts if t)))


def extract_lines(*texts: str | None) -> list[str]:
    found: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in LINE_RE.finditer(t):
            code = f"{m.group(1)}{m.group(2)}"
            if code not in found:
                found.append(code)
        for m in LINE_SOLO_RE.finditer(t):
            if m.group(1) not in found and not any(f.startswith(m.group(1)) for f in found):
                found.append(m.group(1))
    return found


def split_line_list(value: str | None) -> list[str]:
    """VIA ``stoerungs_liniennummer`` like 'ICE, IC6, IC8, RE1, S1'."""
    if not value or not isinstance(value, str):
        return []
    out = []
    for part in re.split(r"[,;/]", value):
        p = part.strip().replace(" ", "")
        if p and len(p) <= 8 and p not in out:
            out.append(p)
    return out
