"""Generate and verify synthetic statement fixtures for parser accuracy testing."""

import argparse
import csv
import json
import sys
import tracemalloc
from decimal import Decimal
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR.parent.parent / "statements" / "accuracy_test"

FIXTURES = {
    "dummy_01_checking.csv": {
        "opening": "1000.00",
        "currency": "USD",
        "credits": "2500.00",
        "debits": "1295.50",
        "closing": "2204.50",
        "posted": 4,
        "rows": [
            ("2025-01-02", "Payroll", "2500.00", "credit"),
            ("2025-01-03", "Rent", "1200.00", "debit"),
            ("2025-01-04", "Groceries", "85.50", "debit"),
            ("2025-01-05", "Monthly fee", "10.00", "debit"),
        ],
    },
    "dummy_02_refunds.csv": {
        "opening": "250.00",
        "currency": "USD",
        "credits": "850.00",
        "debits": "140.75",
        "closing": "959.25",
        "posted": 5,
        "rows": [
            ("2025-02-01", "Salary", "800.00", "credit"),
            ("2025-02-02", "Returned purchase", "50.00", "credit"),
            ("2025-02-03", "Utilities", "25.75", "debit"),
            ("2025-02-04", "Transfer", "100.00", "debit"),
            ("2025-02-05", "Service fee", "15.00", "debit"),
        ],
    },
    "dummy_03_savings.csv": {
        "opening": "5000.00",
        "currency": "USD",
        "credits": "312.50",
        "debits": "1000.00",
        "closing": "4312.50",
        "posted": 3,
        "rows": [
            ("2025-03-01", "Interest earned", "12.50", "credit"),
            ("2025-03-02", "Transfer in", "300.00", "credit"),
            ("2025-03-03", "Transfer out", "1000.00", "debit"),
        ],
    },
    "dummy_05_eur.csv": {
        "opening": "1200.00",
        "currency": "EUR",
        "credits": "1500.00",
        "debits": "320.49",
        "closing": "2379.51",
        "posted": 4,
        "rows": [
            ("2025-05-02", "Salary", "1500.00", "credit", "EUR"),
            ("2025-05-03", "Market", "249.99", "debit", "EUR"),
            ("2025-05-04", "Train", "67.50", "debit", "EUR"),
            ("2025-05-05", "Account fee", "3.00", "debit", "EUR"),
        ],
    },
    "dummy_06_overdraft.csv": {
        "opening": "100.00",
        "currency": "USD",
        "credits": "60.00",
        "debits": "205.00",
        "closing": "-45.00",
        "posted": 3,
        "rows": [
            ("2025-06-02", "Utility bill", "180.00", "debit"),
            ("2025-06-03", "Overdraft fee", "25.00", "debit"),
            ("2025-06-04", "Deposit", "60.00", "credit"),
        ],
    },
    "dummy_07_duplicates.csv": {
        "opening": "1000.00",
        "currency": "USD",
        "credits": "1200.00",
        "debits": "180.00",
        "closing": "2020.00",
        "posted": 4,
        "headers": ("date", "description", "amount", "type", "reference"),
        "rows": [
            ("2025-07-01", "Payroll", "1200.00", "credit", "A701"),
            ("2025-07-02", "Coffee shop", "40.00", "debit", "B702"),
            ("2025-07-02", "Coffee shop", "40.00", "debit", "B703"),
            ("2025-07-03", "Pharmacy", "100.00", "debit", "C704"),
        ],
    },
    "dummy_08_pending.csv": {
        "opening": "400.00",
        "currency": "USD",
        "credits": "200.00",
        "debits": "50.00",
        "closing": "550.00",
        "posted": 2,
        "pending": 1,
        "headers": ("date", "description", "amount", "type", "status"),
        "rows": [
            ("2025-08-02", "Posted purchase", "50.00", "debit", "posted"),
            ("2025-08-03", "Posted deposit", "200.00", "credit", "posted"),
            ("2025-08-04", "Authorization hold", "99.00", "debit", "pending"),
        ],
    },
    "dummy_09_dates.csv": {
        "opening": "0.00",
        "currency": "USD",
        "credits": "115.00",
        "debits": "32.50",
        "closing": "82.50",
        "posted": 4,
        "headers": ("date", "description", "amount", "type"),
        "rows": [
            ("09/01/2025", "Deposit", "100.00", "credit"),
            ("09/02/2025", "Snack", "2.50", "debit"),
            ("09/03/2025", "Transit", "30.00", "debit"),
            ("09/04/2025", "Refund", "15.00", "credit"),
        ],
    },
}

FIXTURES["dummy_04_card.txt"] = {
    "opening": "760.00",
    "currency": "USD",
    "credits": "520.00",
    "debits": "671.24",
    "closing": "608.76",
    "posted": 6,
    "text": (
        "Statement period: 2025-04-01 to 2025-04-30\n"
        "Currency: USD\n"
        "Opening balance: 760.00\n"
        "2025-04-02 | Grocery Market | 42.99 | debit\n"
        "2025-04-04 | Online purchase | 120.00 | debit\n"
        "2025-04-10 | Card payment | 500.00 | credit\n"
        "2025-04-12 | Purchase refund | 20.00 | credit\n"
        "2025-04-25 | Interest charge | 8.25 | debit\n"
        "2025-04-30 | Annual card fee | 500.00 | debit\n"
    ),
}


def _write_csv(path, fixture):
    headers = fixture.get("headers")
    if headers is None:
        headers = ("date", "description", "amount", "type")
        if fixture["currency"] == "EUR":
            headers += ("currency",)
    if fixture.get("pending") and "status" not in headers:
        headers += ("status",)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in fixture["rows"]:
            writer.writerow(row)


def _large_rows(count):
    yield ("date", "description", "amount", "type", "id")
    for sequence in range(1, count + 1):
        yield (
            "2025-10-01",
            f"Load test {sequence}",
            "1.00",
            "credit" if sequence % 2 else "debit",
            f"L{sequence:05d}",
        )


def generate(output_dir, stress_rows):
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, fixture in FIXTURES.items():
        path = output_dir / filename
        if "text" in fixture:
            path.write_text(fixture["text"], encoding="utf-8", newline="")
        else:
            _write_csv(path, fixture)

    large_path = output_dir / "dummy_10_large.csv"
    with large_path.open("w", newline="", encoding="utf-8") as output:
        csv.writer(output).writerows(_large_rows(10001))

    stress_path = output_dir / "dummy_11_stress.csv"
    with stress_path.open("w", newline="", encoding="utf-8") as output:
        csv.writer(output).writerows(_large_rows(stress_rows))

    expected = {
        filename: {
            key: value
            for key, value in fixture.items()
            if key in ("opening", "currency", "credits", "debits", "closing", "posted", "pending")
        }
        for filename, fixture in FIXTURES.items()
    }
    expected["dummy_10_large.csv"] = {
        "opening": "1000.00",
        "currency": "USD",
        "credits": "5001.00",
        "debits": "5000.00",
        "closing": "1001.00",
        "posted": 10001,
        "first_id": "L00001",
        "last_id": "L10001",
    }
    expected["dummy_11_stress.csv"] = _large_oracle(stress_rows)
    (output_dir / "expected.json").write_text(
        json.dumps(expected, indent=2) + "\n",
        encoding="utf-8",
    )


def _large_oracle(count):
    credits = (count + 1) // 2
    debits = count // 2
    return {
        "opening": "1000.00",
        "currency": "USD",
        "credits": f"{credits:.2f}",
        "debits": f"{debits:.2f}",
        "closing": f"{1000 + credits - debits:.2f}",
        "posted": count,
        "first_id": f"L{1:05d}",
        "last_id": f"L{count:05d}",
    }


def _parse(path, chunk_size):
    from analysis.utils.parser import parse_csv_statement_detailed, parse_text_statement_detailed

    content = path.read_bytes()
    if path.suffix.casefold() == ".txt":
        return parse_text_statement_detailed(content)
    return parse_csv_statement_detailed(content, chunk_size=chunk_size)


def verify(output_dir, stress_rows):
    sys.path.insert(0, str(SCRIPT_DIR.parent))
    expected = json.loads((output_dir / "expected.json").read_text(encoding="utf-8"))
    failures = []
    for filename, oracle in expected.items():
        path = output_dir / filename
        count = oracle["posted"] + oracle.get("pending", 0)
        tracemalloc.start()
        parsed, warnings = _parse(path, chunk_size=257)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        unexpected_warnings = warnings
        if filename == "dummy_09_dates.csv":
            unexpected_warnings = [
                warning for warning in warnings
                if "date format is ambiguous; interpreted as MM/DD/YYYY" not in warning
            ]
            if len(warnings) != 4:
                failures.append(
                    f"{filename}: expected four visible ambiguous-date warnings, got {len(warnings)}"
                )
        if unexpected_warnings:
            failures.append(f"{filename}: unexpected parser warnings: {unexpected_warnings[:3]}")
        if len(parsed) != count:
            failures.append(f"{filename}: expected {count} rows, parsed {len(parsed)}")
        posted = [row for row in parsed if not row.get("is_pending", False)]
        pending = [row for row in parsed if row.get("is_pending", False)]
        credits = sum((Decimal(str(row["amount"])) for row in posted if Decimal(str(row["amount"])) > 0), Decimal("0.00"))
        debits = -sum((Decimal(str(row["amount"])) for row in posted if Decimal(str(row["amount"])) < 0), Decimal("0.00"))
        closing = Decimal(oracle["opening"]) + credits - debits
        if len(posted) != oracle["posted"] or len(pending) != oracle.get("pending", 0):
            failures.append(
                f"{filename}: expected {oracle['posted']} posted/{oracle.get('pending', 0)} pending, "
                f"got {len(posted)} posted/{len(pending)} pending"
            )
        for field, actual in (
            ("credits", credits),
            ("debits", debits),
            ("closing", closing),
        ):
            if actual != Decimal(oracle[field]):
                failures.append(f"{filename}: {field} expected {oracle[field]}, got {actual}")

        if filename in ("dummy_09_dates.csv",):
            dates = [row["date"] for row in parsed]
            if dates != ["2025-09-01", "2025-09-02", "2025-09-03", "2025-09-04"]:
                failures.append(f"{filename}: dates were not interpreted as MM/DD/YYYY: {dates}")
        if oracle.get("currency") == "EUR":
            if any(row.get("currency") != "EUR" for row in parsed):
                failures.append(f"{filename}: EUR currency was not preserved")
        if filename == "dummy_07_duplicates.csv":
            ids = [row.get("source_id") for row in parsed]
            if ids != ["A701", "B702", "B703", "C704"]:
                failures.append(f"{filename}: duplicate-like rows or references were lost: {ids}")
        if filename in ("dummy_10_large.csv", "dummy_11_stress.csv"):
            source_ids = [row.get("source_id") for row in parsed]
            if len(set(source_ids)) != count:
                failures.append(f"{filename}: source IDs are not unique")
            if source_ids and (
                source_ids[0] != oracle["first_id"]
                or source_ids[-1] != oracle["last_id"]
            ):
                failures.append(f"{filename}: first/last source IDs do not match the oracle")
            single_pass, _ = _parse(path, chunk_size=max(count + 1, 1))
            if parsed != single_pass:
                failures.append(f"{filename}: chunked and single-pass records differ")
            print(
                f"{filename}: {len(parsed):,} rows; peak traced memory "
                f"{peak_bytes / 1024 / 1024:.2f} MiB"
            )
        if filename == "dummy_04_card.txt":
            from analysis.utils.parser import extract_statement_summary

            summary = extract_statement_summary(path.read_bytes(), path.name)
            if summary.get("opening_balance") != oracle["opening"]:
                failures.append(f"{filename}: opening balance was not extracted")
            if summary.get("currency") != oracle["currency"]:
                failures.append(f"{filename}: statement currency was not preserved")

    from analysis.utils.parser import parse_csv_statement_detailed

    malformed, malformed_warnings = parse_csv_statement_detailed(
        b"date,description,amount,type\n"
        b"2025-01-01,valid,1.00,credit\n"
        b"not-a-date,invalid,2.00,debit\n",
        chunk_size=1,
    )
    if len(malformed) != 1 or not any("Row 3" in warning for warning in malformed_warnings):
        failures.append("Malformed-row handling did not retain the valid row and report row 3")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Accuracy checks passed for {len(expected)} fixtures.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stress-rows", type=int, default=25000)
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()
    if args.stress_rows < 10002:
        parser.error("--stress-rows must be greater than 10,001")
    generate(args.output_dir, args.stress_rows)
    print(f"Generated accuracy fixtures in {args.output_dir}")
    if not args.skip_verify:
        verify(args.output_dir, args.stress_rows)


if __name__ == "__main__":
    main()
