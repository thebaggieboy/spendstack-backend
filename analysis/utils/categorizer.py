import re

from transactions.services import is_transfer_description


_CATEGORY_PATTERNS = (
    ("Bank Charges", re.compile(r"\b(?:bank charge|bank fee|sms alert|stamp duty|card maintenance|maintenance fee|vat|levy|commission|service charge)\b", re.I)),
    ("Utilities", re.compile(r"\b(?:electricity|electric|power|phcn|ikedc|ekedc|aedc|water bill|internet|data subscription|dstv|gotv|utility)\b", re.I)),
    ("Transportation", re.compile(r"\b(?:uber|bolt|indrive|taxi|transport|fuel|petrol|diesel|parking|toll|ride)\b", re.I)),
    ("Food & Dining", re.compile(r"\b(?:restaurant|cafe|café|food|eatery|kitchen|chicken|pizza|grocery|supermarket|market)\b", re.I)),
    ("Healthcare", re.compile(r"\b(?:pharmacy|hospital|clinic|medical|health|doctor|medicine|drug store)\b", re.I)),
    ("Housing", re.compile(r"\b(?:rent|landlord|property|estate|house)\b", re.I)),
    ("Education", re.compile(r"\b(?:school|tuition|university|college|course fee|education)\b", re.I)),
    ("Subscriptions", re.compile(r"\b(?:netflix|spotify|apple.com|google play|subscription|prime video|showmax)\b", re.I)),
    ("Shopping", re.compile(r"\b(?:shopping|store|shop|mall|clothing|fashion|online purchase|e-commerce)\b", re.I)),
)

_DESCRIPTION_PREFIX = re.compile(
    r"^\s*(?:(?:NIP|POS|WEB|ATM|USSD|MOB|MOBILE|TRF|XFER|TRANSFER)"
    r"(?:\s*[/:\-]\s*|\s+))+",
    re.I,
)
_NON_MERCHANT_SEGMENTS = {
    "nip", "pos", "pur", "web", "atm", "ussd", "mob", "mobile", "transfer",
    "trf", "xfer", "purchase", "payment", "withdrawal", "deposit",
}


def _merchant_from_description(description):
    text = re.sub(r"\s+", " ", str(description or "")).strip()
    segments = [part.strip(" -*|") for part in re.split(r"[/|;]", text)]
    for segment in segments:
        normalized = segment.casefold()
        if (
            not segment
            or normalized in _NON_MERCHANT_SEGMENTS
            or re.fullmatch(r"[\d\s.,#-]+", segment)
        ):
            continue
        candidate = _DESCRIPTION_PREFIX.sub("", segment).strip(" -*|")
        candidate = re.sub(r"^(?:to|from)\s+", "", candidate, flags=re.I)
        candidate = re.sub(r"^\d{4,}\s*", "", candidate).strip()
        if candidate and re.search(r"[A-Za-z]", candidate):
            return candidate[:255]

    text = _DESCRIPTION_PREFIX.sub("", text).strip(" -*|")
    text = re.sub(r"^(?:to|from)\s+", "", text, flags=re.I)
    text = re.sub(r"^\d{4,}\s*", "", text).strip()
    return (text or "Unknown")[:255]


def _category_for(description, amount):
    text = str(description or "")
    if is_transfer_description(text):
        return "Transfers"
    if amount > 0:
        if re.search(r"\b(?:salary|payroll|wage)\b", text, re.I):
            return "Salary"
        if re.search(r"\b(?:refund|reversal|cashback)\b", text, re.I):
            return "Refunds"
        return "Income"
    for name, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return name
    if re.search(r"\b(?:cash withdrawal|atm wdl|atm withdrawal)\b", text, re.I):
        return "Cash & ATM"
    return "Other"


def categorize_transactions(transactions_list):
    """Classify statement rows locally from narration and signed amount."""
    for transaction in transactions_list:
        description = str(transaction.get("raw_description") or "")
        try:
            amount = float(transaction.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        transaction["merchant_name"] = _merchant_from_description(description)
        transaction["category_name"] = _category_for(description, amount)
    return transactions_list
