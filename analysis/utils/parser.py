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
_STATEMENT_SUMMARY_LABELS = {
    "opening_balance": re.compile(
        r"\b(?:opening balance|balance b\s*/\s*f|balance brought forward|brought forward balance)\b",
        re.I,
    ),
    "closing_balance": re.compile(
        r"\b(?:closing balance|balance c\s*/\s*f|balance carried forward|carried forward balance)\b",
        re.I,
    ),
    "credit_count": re.compile(
        r"\b(?:credit count|total credit count|number of credits?|no\.?\s*of credits?|credit transactions? count|number of credit transactions?)\b",
        re.I,
    ),
    "debit_count": re.compile(
        r"\b(?:debit count|total debit count|number of debits?|no\.?\s*of debits?|debit transactions? count|number of debit transactions?)\b",
        re.I,
    ),
    "total_credits": re.compile(
        r"\b(?:total credits?(?!\s*(?:count|transactions?\s*count))|total deposits?|total lodgements?|total amount credited|total inflow)\b",
        re.I,
    ),
    "total_debits": re.compile(
        r"\b(?:total debits?(?!\s*(?:count|transactions?\s*count))|total withdrawals?|total amount debited|total outflow)\b",
        re.I,
    ),
}
_SUMMARY_AMOUNT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[₦$£€]\s*)?\(?-?\d[\d,]*(?:\.\d+)?\)?"
    r"(?:\s*(?:DR|CR|DEBIT|CREDIT))?(?![A-Za-z0-9])",
    re.I,
)


def _summary_label(text, at_start=False):
    label_text = str(text or "")
    if at_start:
        label_text = label_text.lstrip()
    for field, pattern in _STATEMENT_SUMMARY_LABELS.items():
        match = (
            pattern.match(label_text)
            if at_start
            else pattern.search(label_text)
        )
        if match:
            return field, match.end()
    return None, None


def _summary_labels(text):
    return sorted(
        (
            match.start(),
            match.end(),
            field,
        )
        for field, pattern in _STATEMENT_SUMMARY_LABELS.items()
        for match in pattern.finditer(str(text or ""))
    )


def _summary_amount(value):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            return _parse_amount(value)
        except (InvalidOperation, TypeError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    match = _SUMMARY_AMOUNT_PATTERN.fullmatch(text)
    if not match:
        return None
    try:
        return _parse_amount(text)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _collect_summary_values(rows, allow_embedded_labels=False):
    found = {field: set() for field in _STATEMENT_SUMMARY_LABELS}
    for row in rows:
        cells = [str(value).strip() for value in row]
        labels = []
        for index, value in enumerate(cells):
            if allow_embedded_labels:
                labels.extend(
                    (index, start, end, field)
                    for start, end, field in _summary_labels(value)
                )
            else:
                field, label_end = _summary_label(value, at_start=True)
                if field:
                    labels.append((index, 0, label_end, field))
        labels.sort(key=lambda label: (label[0], label[1]))
        for label_index, (index, _, label_end, field) in enumerate(labels):
            next_label = labels[label_index + 1] if label_index + 1 < len(labels) else None
            if next_label and next_label[0] == index:
                fragments = [cells[index][label_end:next_label[1]]]
            else:
                next_label_index = next_label[0] if next_label else len(cells)
                fragments = [cells[index][label_end:]]
                fragments.extend(cells[index + 1:next_label_index])

            amount = None
            for fragment in fragments:
                for candidate in _SUMMARY_AMOUNT_PATTERN.finditer(fragment):
                    amount = _summary_amount(candidate.group(0))
                    if amount is not None:
                        break
                if amount is not None:
                    break
            if amount is not None:
                if field in ("total_credits", "total_debits"):
                    amount = abs(amount)
                if field in ("credit_count", "debit_count"):
                    if amount < 0 or amount != amount.to_integral_value():
                        continue
                    found[field].add(str(int(amount)))
                else:
                    found[field].add(str(amount.quantize(Decimal("0.01"))))
    return found


def _resolve_summary_values(values):
    summary = {}
    ambiguous_fields = []
    for field, candidates in values.items():
        if len(candidates) == 1:
            summary[field] = next(iter(candidates))
        elif len(candidates) > 1:
            ambiguous_fields.append(field)
    if ambiguous_fields:
        summary["ambiguous_fields"] = sorted(ambiguous_fields)
    return summary


def extract_statement_summary(file_content, filename):
    """Extract only explicitly labelled statement totals and balances."""
    extension = str(filename).rsplit(".", 1)[-1].casefold() if "." in str(filename) else ""
    if extension == "csv":
        try:
            try:
                decoded = file_content.decode("utf-8-sig")
            except UnicodeDecodeError:
                encoding = "utf-16" if file_content.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp1252"
                decoded = file_content.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError("Could not decode the statement while checking its balance summary.") from exc
        rows = csv.reader(io.StringIO(decoded), delimiter=_detect_delimiter(decoded[:8192]))
        values = _collect_summary_values(rows)
    elif extension in ("xlsx", "xls"):
        try:
            workbook = pd.read_excel(
                io.BytesIO(file_content),
                sheet_name=None,
                header=None,
                dtype=object,
                keep_default_na=False,
            )
        except Exception as exc:
            raise ValueError(f"Could not inspect the statement balance summary: {exc}") from exc
        sheet_values = {
            str(sheet_name): _collect_summary_values(frame.itertuples(index=False, name=None))
            for sheet_name, frame in workbook.items()
        }
        transaction_sheets = [
            str(sheet_name)
            for sheet_name, frame in workbook.items()
            if _find_header(frame)[0] is not None
        ]
        transaction_summaries = [
            (sheet_name, sheet_values[sheet_name])
            for sheet_name in transaction_sheets
        ]
        if any(any(candidates.values()) for _, candidates in transaction_summaries):
            relevant_summaries = transaction_summaries
        else:
            relevant_summaries = [
                (sheet_name, candidates)
                for sheet_name, candidates in sheet_values.items()
                if any(candidates.values())
            ]

        summary = {}
        ambiguous_fields = set()
        incomplete_fields = set()
        for field in _STATEMENT_SUMMARY_LABELS:
            if not relevant_summaries:
                continue
            field_values = [candidates[field] for _, candidates in relevant_summaries]
            if any(len(candidates) > 1 for candidates in field_values):
                ambiguous_fields.add(field)
            elif any(not candidates for candidates in field_values):
                if any(field_values):
                    incomplete_fields.add(field)
            else:
                total = sum(
                    (Decimal(next(iter(candidates))) for candidates in field_values),
                    Decimal("0.00"),
                )
                summary[field] = (
                    str(int(total))
                    if field in ("credit_count", "debit_count")
                    else str(total.quantize(Decimal("0.01")))
                )
        if ambiguous_fields:
            summary["ambiguous_fields"] = sorted(ambiguous_fields)
        if incomplete_fields:
            summary["incomplete_fields"] = sorted(incomplete_fields)
        if len(relevant_summaries) > 1:
            summary["sheet_summaries"] = [
                {
                    "sheet": sheet_name,
                    **_resolve_summary_values(candidates),
                }
                for sheet_name, candidates in relevant_summaries
            ]
        return summary
    elif extension == "pdf":
        try:
            with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                pages = [(page_number, page.extract_text() or "") for page_number, page in enumerate(pdf.pages, start=1)]
        except Exception:
            pages = _extract_pdfium_text(file_content)
        values = _collect_summary_values(
                (line.split("|") for _, text in pages for line in text.splitlines()),
                allow_embedded_labels=True,
            )
    elif extension == "txt":
        try:
            text = file_content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Could not decode the text statement.") from exc
        values = _collect_summary_values(
            (line.split("|") for line in text.splitlines()),
            allow_embedded_labels=True,
        )
        summary = _resolve_summary_values(values)
        currency = re.search(r"^\s*Currency\s*:\s*([A-Z]{3})\s*$", text, re.I | re.M)
        if currency:
            summary["currency"] = currency.group(1).upper()
        return summary
    else:
        return {}

    return _resolve_summary_values(values)


def parse_text_statement_detailed(file_content):
    """Parse pipe-delimited text statements with explicit transaction directions."""
    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Failed to decode text statement: {exc}") from exc
    currency_match = re.search(r"^\s*Currency\s*:\s*([A-Z]{3})\s*$", text, re.I | re.M)
    currency = currency_match.group(1).upper() if currency_match else None
    transactions = []
    warnings = []
    for row_number, line in enumerate(text.splitlines(), start=1):
        if "|" not in line:
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 4:
            warnings.append(_row_warning(None, row_number, "expected date, description, amount, and type"))
            continue
        date_text, description, amount_text, direction = fields
        try:
            parsed_date = _parse_date(date_text)
            amount = _parse_amount(amount_text)
            direction = _normalize_header(direction)
            if amount is None or not description:
                raise ValueError("missing transaction field")
            if direction not in ("credit", "debit"):
                raise ValueError("unrecognized debit/credit indicator")
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            warnings.append(_row_warning(None, row_number, "invalid date, description, amount, or type"))
            continue
        transactions.append({
            "date": parsed_date,
            "raw_description": description,
            "amount": abs(amount) if direction == "credit" else -abs(amount),
            "source_row": row_number,
            "currency": currency,
            "status": "posted",
            "is_pending": False,
        })
    if not transactions and not warnings:
        raise ValueError("No transaction rows were found in the text statement.")
    return transactions, warnings


def parse_csv_statement(file_content):
    transactions, _ = parse_csv_statement_detailed(file_content)
    return transactions


def parse_csv_statement_detailed(file_content, chunk_size=1000, progress_callback=None):
    """Parse a CSV in bounded row batches while retaining source row numbers."""
    try:
        try:
            decoded = file_content.decode("utf-8-sig")
        except UnicodeDecodeError:
            encoding = "utf-16" if file_content.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp1252"
            decoded = file_content.decode(encoding)
        delimiter = _detect_delimiter(decoded[:8192])
        reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
        header_row = None
        header_source_row = None
        for record_number, row in enumerate(reader, start=1):
            if record_number > 30:
                break
            if _header_score(row) == 3:
                header_row = row
                header_source_row = reader.line_num
                break
        if header_row is None:
            raise ValueError("Could not locate transaction table headers.")
        while header_row and not str(header_row[-1]).strip():
            header_row.pop()

        transactions = []
        warnings = []
        rows = []
        source_rows = []
        processed_rows = 0
        chunk_size = max(1, int(chunk_size))
        for row in reader:
            while row and not str(row[-1]).strip():
                row.pop()
            rows.append(row)
            source_rows.append(reader.line_num)
            if len(rows) >= chunk_size:
                chunk_transactions, chunk_warnings = _parse_csv_rows(
                    header_row, header_source_row, rows, source_rows
                )
                transactions.extend(chunk_transactions)
                warnings.extend(chunk_warnings)
                processed_rows += len(rows)
                if progress_callback:
                    progress_callback(processed_rows)
                rows = []
                source_rows = []
        if rows:
            chunk_transactions, chunk_warnings = _parse_csv_rows(
                header_row, header_source_row, rows, source_rows
            )
            transactions.extend(chunk_transactions)
            warnings.extend(chunk_warnings)
            processed_rows += len(rows)
        if progress_callback:
            progress_callback(processed_rows)
        return transactions, warnings
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError, csv.Error) as exc:
        raise ValueError(f"Failed to parse CSV statement: {exc}") from exc


def _parse_csv_rows(header_row, header_source_row, rows, source_rows):
    width = max([len(header_row)] + [len(row) for row in rows])
    frame = pd.DataFrame(
        [row + [""] * (width - len(row)) for row in [header_row] + rows]
    )
    return _parse_tabular_statement(
        frame,
        source_rows=[header_source_row] + source_rows,
    )


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
                dayfirst_dates=True,
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
    date_columns = {
        index for index, header in enumerate(headers)
        if any(_has_alias(header, alias) for alias in _DATE_HEADERS)
    }
    description_columns = {
        index for index, header in enumerate(headers)
        if any(_has_alias(header, alias) for alias in _DESCRIPTION_HEADERS)
    }
    amount_columns = {
        index for index, header in enumerate(headers)
        if any(_has_alias(header, alias) for alias in _AMOUNT_HEADERS + _DEBIT_HEADERS + _CREDIT_HEADERS)
    }
    if any(
        date_column != description_column
        and date_column != amount_column
        and description_column != amount_column
        for date_column in date_columns
        for description_column in description_columns
        for amount_column in amount_columns
    ):
        return 3
    return min(
        int(bool(date_columns)) + int(bool(description_columns)) + int(bool(amount_columns)),
        2,
    )


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


def _parse_tabular_statement(
    frame,
    source_sheet=None,
    allow_no_header=False,
    source_rows=None,
    dayfirst_dates=False,
):
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
    status_col = _select_column(column_names, ("transaction status", "status", "state"))
    currency_col = _select_column(column_names, ("currency", "currency code"))
    source_id_col = _select_identifier_column(column_names)

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
        if any(_summary_label(value, at_start=True)[0] for value in values):
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
            date_value = _parse_date(date_value, dayfirst=dayfirst_dates)
        except (ValueError, TypeError, OverflowError):
            warnings.append(_row_warning(source_sheet, row_number, "invalid date"))
            continue
        if _has_ambiguous_slash_date(row[date_col]):
            convention = "DD/MM/YYYY" if dayfirst_dates else "MM/DD/YYYY"
            location = f"Sheet '{source_sheet}', row {row_number}" if source_sheet else f"Row {row_number}"
            warnings.append(
                f"{location}: date format is ambiguous; interpreted as {convention}."
            )

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
        if status_col and not _is_blank(row[status_col]):
            status = _normalize_header(row[status_col])
            transaction["status"] = status
            transaction["is_pending"] = status in ("pending", "unsettled", "authorization hold")
        if currency_col and not _is_blank(row[currency_col]):
            transaction["currency"] = str(row[currency_col]).strip().upper()
        if source_id_col and not _is_blank(row[source_id_col]):
            transaction["source_id"] = str(row[source_id_col]).strip()
        if source_sheet is not None:
            transaction["source_sheet"] = source_sheet
        transactions.append(transaction)

    return transactions, warnings


def _select_identifier_column(columns):
    aliases = ("transaction id", "transaction reference", "reference id", "reference", "id")
    candidates = [
        column
        for column in columns
        if column in aliases
    ]
    if not candidates:
        return None
    return min(candidates, key=aliases.index)


def _is_blank(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _has_ambiguous_slash_date(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*", value)
    return bool(match and int(match.group(1)) <= 12 and int(match.group(2)) <= 12)


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


def _parse_date(value, dayfirst=False):
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
    date_formats = [
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d %b %Y %H:%M:%S",
        "%d %b %y %H:%M:%S",
        "%d %b %Y",
        "%d %b %y",
        "%b %d, %Y",
    ]
    slash_formats = (
        ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y")
        if dayfirst
        else ("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y")
    )
    for date_format in slash_formats + tuple(date_formats):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported or ambiguous date format: {text}")


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
            parsed_date = _parse_date(date_match.group(0), dayfirst=True)
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
                        transactions, warnings = _parse_tabular_statement(
                            frame,
                            dayfirst_dates=True,
                        )
                    except ValueError:
                        continue
                    for transaction in transactions:
                        transaction["source_page"] = page_number
                    table_transactions.extend(transactions)
                    table_warnings.extend(
                        warning.replace("Row ", f"PDF page {page_number}, row ", 1)
                        for warning in warnings
                    )
            if not table_transactions:
                for page_number, page in enumerate(pdf.pages, start=1):
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
