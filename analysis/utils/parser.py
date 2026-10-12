import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd
import pdfplumber


_DATE_HEADERS = ("transaction date", "trans date", "date", "value date", "posting date")
_DESCRIPTION_HEADERS = (
    "description", "narration", "narrative", "transaction details", "particulars",
    "details", "remarks",
)
_AMOUNT_HEADERS = ("amount", "transaction amount", "value")
_DEBIT_HEADERS = ("debit", "withdrawal", "withdrawn", "money out", "outflow", "paid out", "dr")
_CREDIT_HEADERS = ("credit", "deposit", "lodgement", "money in", "inflow", "paid in", "cr")
_DIRECTION_HEADERS = ("dr/cr", "debit/credit", "type", "direction")


def parse_csv_statement(file_content):
    transactions, _ = parse_csv_statement_detailed(file_content)
    return transactions


def parse_csv_statement_detailed(file_content):
    """Parse a CSV statement while retaining source row numbers and row-level issues."""
    try:
        try:
            decoded = file_content.decode("utf-8-sig")
        except UnicodeDecodeError:
            encoding = "utf-16" if file_content.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp1252"
            decoded = file_content.decode(encoding)
        delimiter = _detect_delimiter(decoded[:8192])
        reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
        rows = []
        source_rows = []
        for row in reader:
            rows.append(row)
            source_rows.append(reader.line_num)
        for row in rows:
            while row and not row[-1].strip():
                row.pop()
        width = max((len(row) for row in rows), default=0)
        frame = pd.DataFrame([
            row + [""] * (width - len(row))
            for row in rows
        ])
        return _parse_tabular_statement(frame, source_rows=source_rows)
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError, csv.Error) as exc:
        raise ValueError(f"Failed to parse CSV statement: {exc}") from exc


def _detect_delimiter(sample):
    lines = sample.splitlines()[:100]
    best_delimiter = ","
    best_score = (0, 0, 0)
    for delimiter in (",", ";", "\t", "|"):
        try:
            widths = [
                len(row)
                for row in csv.reader(lines, delimiter=delimiter)
                if len(row) > 1
            ]
        except csv.Error:
            continue
        if not widths:
            continue
        counts = {}
        for width in widths:
            counts[width] = counts.get(width, 0) + 1
        modal_width, modal_count = max(counts.items(), key=lambda pair: (pair[1], pair[0]))
        score = (modal_count, modal_width, len(widths))
        if score > best_score:
            best_delimiter = delimiter
            best_score = score
    return best_delimiter


def parse_excel_statement_detailed(file_content):
    """Parse transaction tables on every worksheet in an Excel statement."""
    try:
        workbook = pd.read_excel(
            io.BytesIO(file_content),
            sheet_name=None,
            header=None,
            dtype=object,
            keep_default_na=False,
        )
    except Exception as exc:
        raise ValueError(f"Failed to parse Excel statement: {exc}") from exc

    transactions = []
    warnings = []
    for sheet_name, frame in workbook.items():
        try:
            sheet_transactions, sheet_warnings = _parse_tabular_statement(
                frame,
                source_sheet=str(sheet_name),
                allow_no_header=True,
            )
        except ValueError:
            continue
        transactions.extend(sheet_transactions)
        warnings.extend(sheet_warnings)

    if not transactions:
        if warnings:
            raise ValueError(
                "No valid transactions were found in the Excel file. "
                + " ".join(warnings[:5])
            )
        raise ValueError("No transaction table with recognizable columns was found in the Excel file.")
    return transactions, warnings


def _normalize_header(value):
    if _is_blank(value):
        return ""
    text = str(value).strip().casefold()
    text = re.sub(r"[\n\r]+", " ", text)
    text = re.sub(r"[^a-z0-9/ ]+", " ", text)
    return " ".join(text.split())


def _header_score(values):
    headers = [_normalize_header(value) for value in values]
    has_date = any(
        any(_has_alias(header, alias) for alias in _DATE_HEADERS)
        for header in headers
    )
    has_description = any(
        any(_has_alias(header, alias) for alias in _DESCRIPTION_HEADERS)
        for header in headers
    )
    has_amount = (
        any(any(_has_alias(header, alias) for alias in _AMOUNT_HEADERS) for header in headers)
        or any(any(_has_alias(header, alias) for alias in _DEBIT_HEADERS) for header in headers)
        or any(any(_has_alias(header, alias) for alias in _CREDIT_HEADERS) for header in headers)
    )
    return int(has_date) + int(has_description) + int(has_amount)


def _has_alias(header, alias):
    if alias in ("dr", "cr"):
        return header == alias or header.startswith(f"{alias} ")
    return alias in header


def _find_header(frame):
    for row_index in range(min(len(frame), 30)):
        values = frame.iloc[row_index].tolist()
        if _header_score(values) == 3:
            headers = [_normalize_header(value) for value in values]
            while headers and not headers[-1]:
                headers.pop()
            return row_index, headers
    return None, []


def _select_column(columns, aliases, exclude=()):
    candidates = [
        column for column in columns
        if any(_has_alias(column, alias) for alias in aliases)
        and not any(term in column for term in exclude)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda column: min(
            (len(column) - len(alias), aliases.index(alias))
            for alias in aliases
            if _has_alias(column, alias)
        ),
    )


def _parse_tabular_statement(frame, source_sheet=None, allow_no_header=False, source_rows=None):
    header_index, headers = _find_header(frame)
    if header_index is None:
        if allow_no_header:
            raise ValueError("No recognizable transaction header.")
        raise ValueError("Could not locate transaction table headers.")

    header_width = len(headers)
    column_names = []
    seen_headers = {}
    for index in range(frame.shape[1]):
        header = headers[index] if index < header_width else ""
        name = header or f"column_{index}"
        seen_headers[name] = seen_headers.get(name, 0) + 1
        if seen_headers[name] > 1:
            name = f"{name}_{seen_headers[name]}"
        column_names.append(name)
    records = frame.iloc[header_index + 1:].copy()
    records.columns = column_names

    date_col = _select_column(column_names, _DATE_HEADERS)
    desc_col = _select_column(column_names, _DESCRIPTION_HEADERS)
    amount_col = _select_column(column_names, _AMOUNT_HEADERS, exclude=("debit", "credit"))
    debit_col = _select_column(column_names, _DEBIT_HEADERS)
    credit_col = _select_column(column_names, _CREDIT_HEADERS)
    direction_col = _select_column(column_names, _DIRECTION_HEADERS)

    transactions = []
    warnings = []
    for record_index, row in records.iterrows():
        row_number = (
            source_rows[record_index]
            if source_rows and record_index < len(source_rows)
            else record_index + 1
        )
        extra_values = [
            row[column_names[index]]
            for index in range(header_width, len(column_names))
            if not _is_blank(row[column_names[index]])
        ]
        if extra_values:
            warnings.append(_row_warning(
                source_sheet,
                row_number,
                "unexpected values appear outside the transaction columns",
            ))
            continue
        values = [value for value in row.tolist() if not _is_blank(value)]
        if not values:
            continue

        date_value = row[date_col]
        description_value = row[desc_col]
        if _is_blank(date_value):
            if _has_amount_value(row, amount_col, debit_col, credit_col):
                warnings.append(_row_warning(source_sheet, row_number, "amount found but date is missing"))
            continue
        if _is_blank(description_value):
            warnings.append(_row_warning(source_sheet, row_number, "date found but description is missing"))
            continue

        try:
            date_value = _parse_date(date_value)
        except (ValueError, TypeError, OverflowError):
            warnings.append(_row_warning(source_sheet, row_number, "invalid date"))
            continue

        try:
            amount_value, sign_is_known = _read_row_amount(
                row,
                amount_col,
                debit_col,
                credit_col,
                direction_col,
                description_value,
            )
        except (InvalidOperation, TypeError, ValueError):
            warnings.append(_row_warning(source_sheet, row_number, "invalid amount"))
            continue
        if amount_value is None:
            warnings.append(_row_warning(source_sheet, row_number, "date and description found but amount is missing"))
            continue
        if not sign_is_known:
            warnings.append(_row_warning(
                source_sheet,
                row_number,
                "amount sign is ambiguous; add a Debit/Credit indicator or use separate debit and credit columns",
            ))

        transaction = {
            "date": date_value,
            "raw_description": str(description_value),
            "amount": amount_value,
            "source_row": row_number,
        }
        if source_sheet is not None:
            transaction["source_sheet"] = source_sheet
        transactions.append(transaction)

    return transactions, warnings


def _is_blank(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _has_amount_value(row, amount_col, debit_col, credit_col):
    return any(
        column and not _is_blank(row[column])
        for column in (amount_col, debit_col, credit_col)
    )


def _row_warning(sheet, row_number, reason):
    location = f"Row {row_number}"
    if sheet:
        location = f"Sheet '{sheet}', row {row_number}"
    return f"{location}: {reason}; not imported."


def _parse_date(value):
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and 1 <= value <= 2958465:
        parsed = pd.to_datetime(value, unit="D", origin="1899-12-30")
        return parsed.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        raise ValueError("Date is empty.")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    parsed = pd.to_datetime(text, dayfirst=True, errors="raise")
    return parsed.strftime("%Y-%m-%d")


def _read_row_amount(row, amount_col, debit_col, credit_col, direction_col, description):
    debit = _parse_amount(row[debit_col]) if debit_col else None
    credit = _parse_amount(row[credit_col]) if credit_col else None
    if debit not in (None, Decimal("0")) and credit not in (None, Decimal("0")):
        raise ValueError("Both debit and credit amounts are populated.")
    if debit not in (None, Decimal("0")):
        return -abs(debit), True
    if credit not in (None, Decimal("0")):
        return abs(credit), True

    if not amount_col or _is_blank(row[amount_col]):
        return None, True
    amount = _parse_amount(row[amount_col])
    if amount is None:
        return None, True
    if direction_col and not _is_blank(row[direction_col]):
        direction = _normalize_header(row[direction_col])
        if direction in ("dr", "debit", "withdrawal", "withdraw"):
            return -abs(amount), True
        if direction in ("cr", "credit", "deposit", "lodgement"):
            return abs(amount), True
        raise ValueError("Unrecognized debit/credit indicator.")
    amount, known = _infer_amount_sign(amount, description)
    return amount, known


def _parse_amount(value):
    if _is_blank(value):
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float)):
        amount = Decimal(str(value))
    else:
        raw = str(value).strip()
        raw = re.sub(r"[\u00a0\u202f]", " ", raw).strip()
        if re.fullmatch(r"[-\u2013\u2014\u2212]+", raw):
            return None
        raw = raw.replace("−", "-").replace("﹣", "-")
        indicator = re.search(r"\b(DR|DEBIT|CR|CREDIT)\s*$", raw, re.I)
        raw = re.sub(r"\b(?:DR|DEBIT|CR|CREDIT)\s*$", "", raw, flags=re.I).strip()
        raw = re.sub(r"\b(?:NGN|NAIRA)\b", "", raw, flags=re.I)
        raw = re.sub(r"[₦$£€]", "", raw)
        raw = re.sub(r"^N\s*(?=\d)", "", raw, flags=re.I).strip()
        negative = (
            (raw.startswith("(") and raw.endswith(")"))
            or raw.startswith("-")
            or raw.endswith("-")
            or (indicator and indicator.group(1).casefold() in ("dr", "debit"))
        )
        raw = raw.strip("()").replace(" ", "")
        if raw.endswith("-"):
            raw = raw[:-1]
        if not re.fullmatch(r"[-+]?\d[\d.,]*", raw):
            raise InvalidOperation(f"Invalid amount: {value}")
        if "," in raw and "." in raw:
            decimal_separator = "," if raw.rfind(",") > raw.rfind(".") else "."
            grouping_separator = "." if decimal_separator == "," else ","
            raw = raw.replace(grouping_separator, "")
            if decimal_separator == ",":
                raw = raw.replace(",", ".")
        elif "," in raw:
            last_group = raw.rsplit(",", 1)[1]
            if len(last_group) in (1, 2):
                raw = raw.replace(",", ".")
            else:
                raw = raw.replace(",", "")
        amount = Decimal(raw)
        if negative:
            amount = -abs(amount)
    if not amount.is_finite():
        raise InvalidOperation("Amount must be finite.")
    return amount


def _infer_amount_sign(amount, description):
    if amount <= 0:
        return amount, True
    text = str(description or "")
    if re.search(
        r"\b(?:debit|dr|withdrawal|withdraw|purchase|payment|pos|fee|charge)\b",
        text,
        re.I,
    ):
        return -abs(amount), True
    if re.search(
        r"\b(?:credit|cr|deposit|salary|payroll|refund|reversal|cashback|received|interest)\b",
        text,
        re.I,
    ):
        return abs(amount), True
    if re.search(r"\btransfer\s+to\b", text, re.I):
        return -abs(amount), True
    if re.search(r"\btransfer\s+from\b", text, re.I):
        return abs(amount), True
    return amount, False


_PDF_DATE_PATTERN = re.compile(
    r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})(?!\d)"
)
_PDF_AMOUNT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[₦$£€]\s*)?\(?-?\d[\d,]*(?:\.\d+)?\)?"
)


def _parse_pdf_text(text):
    transactions = []
    warnings = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        date_matches = list(_PDF_DATE_PATTERN.finditer(line))
        if not date_matches:
            continue
        date_match = date_matches[0]
        remainder = line[date_match.end():]
        searchable_remainder = _PDF_DATE_PATTERN.sub(
            lambda match: " " * len(match.group(0)),
            remainder,
        )
        amounts = list(_PDF_AMOUNT_PATTERN.finditer(searchable_remainder))
        if not amounts:
            if remainder.strip(" \t|,:;-"):
                warnings.append(
                    f"PDF text line {line_number}: date-like row has no recognizable amount; not imported."
                )
            continue

        try:
            parsed_date = _parse_date(date_match.group(0))
        except (ValueError, TypeError, OverflowError):
            warnings.append(f"PDF text line {line_number}: invalid transaction date; not imported.")
            continue

        directions = list(re.finditer(r"\b(DR|DEBIT|CR|CREDIT)\b", searchable_remainder, re.I))
        direction = next(
            (item for item in directions if any(amount.start() < item.start() for amount in amounts)),
            None,
        )
        candidates = [
            item for item in amounts
            if direction is None or item.start() < direction.start()
        ]
        if not candidates:
            warnings.append(f"PDF text line {line_number}: transaction amount is ambiguous; not imported.")
            continue
        formatted = [item for item in candidates if re.search(r"[,.₦$£€]", item.group(0))]
        amount_match = (formatted or candidates)[0]
        try:
            amount = _parse_amount(amount_match.group(0))
        except (InvalidOperation, TypeError, ValueError):
            warnings.append(f"PDF text line {line_number}: invalid transaction amount; not imported.")
            continue
        if direction:
            amount = -abs(amount) if direction.group(1).casefold() in ("dr", "debit") else abs(amount)
        else:
            amount, sign_known = _infer_amount_sign(amount, remainder[:amount_match.start()])
            if not sign_known:
                warnings.append(
                    f"PDF text line {line_number}: amount sign is ambiguous; not imported until reviewed."
                )

        description = _PDF_DATE_PATTERN.sub("", remainder[:amount_match.start()])
        description = description.strip(" \t|,:;-")
        if not description:
            warnings.append(f"PDF text line {line_number}: transaction description is missing; not imported.")
            continue
        transactions.append({
            "date": parsed_date,
            "raw_description": description,
            "amount": amount,
            "source_row": line_number,
        })
    return transactions, warnings


def parse_pdf_statement_detailed(file_obj):
    """Extract searchable PDF tables/text locally and report rows that need review."""
    file_bytes = file_obj.read()
    table_transactions = []
    table_warnings = []
    extracted_pages = []
    used_pdfium_fallback = False
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                for table in page.extract_tables() or []:
                    if len(table) < 2:
                        continue
                    frame = pd.DataFrame(table, dtype=object)
                    try:
                        transactions, warnings = _parse_tabular_statement(frame)
                    except ValueError:
                        continue
                    for transaction in transactions:
                        transaction["source_page"] = page_number
                    table_transactions.extend(transactions)
                    table_warnings.extend(
                        warning.replace("Row ", f"PDF page {page_number}, row ", 1)
                        for warning in warnings
                    )
                extracted = page.extract_text()
                if extracted:
                    extracted_pages.append((page_number, extracted))
    except Exception:
        extracted_pages = _extract_pdfium_text(file_bytes)
        used_pdfium_fallback = True

    if table_transactions:
        return table_transactions, table_warnings
    if not extracted_pages and not used_pdfium_fallback:
        extracted_pages = _extract_pdfium_text(file_bytes)
    if not extracted_pages:
        raise ValueError(
            "This PDF has no selectable text. It may be a scanned image. Upload a searchable PDF or export the statement as CSV/Excel."
        )
    transactions = []
    warnings = []
    for page_number, text in extracted_pages:
        page_transactions, page_warnings = _parse_pdf_text(text)
        for transaction in page_transactions:
            transaction["source_page"] = page_number
        transactions.extend(page_transactions)
        warnings.extend(
            warning.replace("PDF text line ", f"PDF page {page_number}, line ", 1)
            for warning in page_warnings
        )
    if not transactions:
        raise ValueError(
            "Could not identify transaction rows in this PDF. Upload a CSV/Excel statement or check the PDF layout."
        )
    return transactions, warnings


def _extract_pdfium_text(file_bytes):
    """Fallback text extraction for PDFs whose structure pdfplumber cannot read."""
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(file_bytes)
        try:
            extracted_pages = []
            for page_number in range(len(document)):
                page = document[page_number]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                if text and text.strip():
                    extracted_pages.append((page_number + 1, text))
            return extracted_pages
        finally:
            document.close()
    except Exception as exc:
        raise ValueError(
            "We couldn't open this PDF. It may be incomplete, damaged, password-protected, "
            "or not a valid PDF. Download it again from your bank, or export the statement as CSV/Excel."
        ) from exc


def parse_pdf_statement(file_obj):
    transactions, _ = parse_pdf_statement_detailed(file_obj)
    return transactions
