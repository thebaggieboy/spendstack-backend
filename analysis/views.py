import hashlib
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from .utils.parser import (
    parse_csv_statement_detailed,
    parse_excel_statement_detailed,
    parse_pdf_statement_detailed,
    extract_statement_summary,
)
from .utils.categorizer import categorize_transactions
from transactions.models import Transaction, Account, Category
from .models import Statement, StatementPreview
from transactions.models import TransactionRule
from transactions.services import classify_transaction


MAX_STATEMENT_SIZE = 10 * 1024 * 1024


def _parse_statement_file(file_obj, content):
    if not content:
        raise ValueError(
            "The uploaded file is empty (0 bytes), so no statement data was received. "
            "Download the statement again and verify the file is non-empty before uploading."
        )
    filename = os.path.basename(file_obj.name)
    extension = os.path.splitext(filename)[1].lower()
    warnings = []
    if extension == ".csv":
        raw_data, warnings = parse_csv_statement_detailed(content)
    elif extension in (".xlsx", ".xls"):
        raw_data, warnings = parse_excel_statement_detailed(content)
    elif extension == ".pdf":
        import io
        raw_data, warnings = parse_pdf_statement_detailed(io.BytesIO(content))
    else:
        raise ValueError("Unsupported file format. Use CSV, Excel, or PDF.")

    if not raw_data:
        raise ValueError("No valid transactions were found in this statement.")
    if len(raw_data) > 10000:
        raise ValueError("Statements can contain up to 10,000 transactions per upload.")

    for index, tx in enumerate(raw_data):
        try:
            tx["date"] = timezone.datetime.strptime(tx["date"], "%Y-%m-%d").date().isoformat()
            amount = Decimal(str(tx["amount"]))
            if not amount.is_finite():
                raise InvalidOperation
            normalized_amount = amount.quantize(Decimal("0.01"))
            if normalized_amount != amount:
                warnings.append(
                    f"Transaction {tx.get('source_row', index + 1)}: amount has more than two decimal places; not imported."
                )
                tx["_invalid"] = True
            if abs(normalized_amount) >= Decimal("100000000"):
                warnings.append(
                    f"Transaction {tx.get('source_row', index + 1)}: amount exceeds the supported range; not imported."
                )
                tx["_invalid"] = True
            tx["amount"] = str(normalized_amount)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            warnings.append(
                f"Transaction {tx.get('source_row', index + 1)}: invalid date or amount; not imported."
            )
            tx["_invalid"] = True
        if not isinstance(tx.get("raw_description"), str) or not tx["raw_description"].strip():
            warnings.append(
                f"Transaction {tx.get('source_row', index + 1)}: missing description; not imported."
            )
            tx["_invalid"] = True
        tx["raw_description"] = str(tx.get("raw_description", ""))

    raw_data = [tx for tx in raw_data if not tx.pop("_invalid", False)]
    if not raw_data:
        details = " ".join(warnings[:5])
        raise ValueError(
            f"No valid transactions were found in this statement. {details}".strip()
        )
    for index, tx in enumerate(raw_data):
        tx["original_index"] = index
    categorized_data = categorize_transactions(raw_data)
    for tx in categorized_data:
        tx["merchant_name"] = str(tx.get("merchant_name") or "Unknown")[:255]
        tx["category_name"] = str(tx.get("category_name") or "Other")[:100]
    return categorized_data, warnings


def _create_statement(user, filename, file_sha256, categorized_data, summary_data=None):
    account, _ = Account.objects.get_or_create(name="Default Checking", user=user)
    statement = Statement.objects.create(
        user=user,
        filename=filename,
        file_sha256=file_sha256,
        summary_data=summary_data or {},
    )
    rules = list(
        TransactionRule.objects.filter(user=user, is_active=True).order_by("-priority", "id")
    )
    transaction_rows = []
    category_names = set()
    for tx in categorized_data:
        base_merchant = tx.get("original_merchant_name", tx.get("merchant_name", "Unknown"))
        base_category = tx.get("original_category_name", tx.get("category_name", "Other"))
        merchant_name, cat_name, is_transfer = classify_transaction(
            tx.get("raw_description", ""),
            base_merchant,
            base_category,
            rules,
        )
        category_names.add(cat_name)
        transaction_rows.append(
            {
                "date": tx["date"],
                "amount": tx["amount"],
                "merchant_name": merchant_name,
                "original_merchant_name": base_merchant,
                "original_category_name": base_category,
                "raw_description": tx["raw_description"],
                "category_name": cat_name,
                "is_transfer": is_transfer,
            }
        )

    existing_categories = {
        category.name: category
        for category in Category.objects.filter(name__in=category_names)
    }
    missing_categories = category_names - existing_categories.keys()
    if missing_categories:
        Category.objects.bulk_create(
            [
                Category(
                    name=name,
                    color="#9ca3af",
                    is_system=name == "Transfers",
                )
                for name in missing_categories
            ],
            ignore_conflicts=True,
        )
        existing_categories.update(
            {
                category.name: category
                for category in Category.objects.filter(name__in=missing_categories)
            }
        )

    transactions = [
        Transaction(
            user=user,
            account=account,
            statement=statement,
            date=row["date"],
            amount=row["amount"],
            merchant_name=row["merchant_name"],
            original_merchant_name=row["original_merchant_name"],
            original_category_name=row["original_category_name"],
            raw_description=row["raw_description"],
            category=existing_categories[row["category_name"]],
            is_transfer=row["is_transfer"],
        )
        for row in transaction_rows
    ]
    created_transactions = Transaction.objects.bulk_create(transactions, batch_size=500)
    return statement, created_transactions


def _statement_reconciliation(summary_data, credits, debits):
    summary_data = summary_data if isinstance(summary_data, dict) else {}
    ambiguous_fields = set(summary_data.get("ambiguous_fields", []))
    row_net = credits + debits
    checks = {}
    mismatch = False
    for field, row_value in (("total_credits", credits), ("total_debits", abs(debits))):
        reported = summary_data.get(field)
        if field in ambiguous_fields:
            checks[field] = {"status": "review", "reported": None, "from_transactions": float(row_value)}
        elif reported is None:
            checks[field] = {"status": "unavailable", "reported": None, "from_transactions": float(row_value)}
        else:
            reported_value = Decimal(str(reported))
            difference = (row_value - reported_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            matches = difference == 0
            mismatch = mismatch or not matches
            checks[field] = {
                "status": "match" if matches else "mismatch",
                "reported": float(reported_value),
                "from_transactions": float(row_value),
                "difference": float(difference),
            }

    opening = summary_data.get("opening_balance")
    closing = summary_data.get("closing_balance")
    balance_check = {"status": "unavailable", "opening_balance": None, "closing_balance": None}
    if "opening_balance" in ambiguous_fields or "closing_balance" in ambiguous_fields:
        balance_check["status"] = "review"
    elif opening is not None and closing is not None:
        opening_value = Decimal(str(opening))
        closing_value = Decimal(str(closing))
        movement = (closing_value - opening_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        difference = (row_net - movement).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        matches = difference == 0
        mismatch = mismatch or not matches
        balance_check = {
            "status": "match" if matches else "mismatch",
            "opening_balance": float(opening_value),
            "closing_balance": float(closing_value),
            "reported_movement": float(movement),
            "transaction_net": float(row_net),
            "difference": float(difference),
        }
    elif opening is not None or closing is not None:
        balance_check.update({
            "opening_balance": float(Decimal(str(opening))) if opening is not None else None,
            "closing_balance": float(Decimal(str(closing))) if closing is not None else None,
        })

    if mismatch:
        overall = "mismatch"
    elif "review" in [check["status"] for check in checks.values()] or balance_check["status"] == "review":
        overall = "review"
    elif any(check["status"] == "match" for check in checks.values()) or balance_check["status"] == "match":
        overall = "matched"
    else:
        overall = "unavailable"
    return {
        "status": overall,
        "statement_summary": {
            field: summary_data.get(field)
            for field in ("opening_balance", "closing_balance", "total_credits", "total_debits")
        },
        "ambiguous_fields": sorted(ambiguous_fields),
        "checks": checks,
        "balance_movement": balance_check,
    }


def _apply_user_rules(user, categorized_data):
    rules = list(
        TransactionRule.objects.filter(user=user, is_active=True).order_by("-priority", "id")
    )
    for tx in categorized_data:
        original_merchant = str(tx.get("merchant_name") or "Unknown")[:255]
        original_category = str(tx.get("category_name") or "Other")[:100]
        tx["original_merchant_name"] = original_merchant
        tx["original_category_name"] = original_category
        merchant, category, is_transfer = classify_transaction(
            tx.get("raw_description", ""),
            original_merchant,
            original_category,
            rules,
        )
        tx["merchant_name"] = merchant[:255]
        tx["category_name"] = category[:100]
        tx["is_transfer"] = is_transfer
    return categorized_data


class StatementPreviewView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        file_obj = request.data.get("file")
        if not file_obj:
            return Response(
                {"detail": "Choose a CSV, Excel, or PDF statement."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if file_obj.size > MAX_STATEMENT_SIZE:
            return Response({"detail": "Statement files must be 10 MB or smaller."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        content = file_obj.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        existing = Statement.objects.filter(user=request.user, file_sha256=file_sha256).first()
        if existing:
            return Response(
                {
                    "detail": "This statement has already been uploaded.",
                    "duplicate_statement_id": existing.id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            categorized_data, warnings = _parse_statement_file(file_obj, content)
            summary_data = extract_statement_summary(content, file_obj.name)
            categorized_data = _apply_user_rules(request.user, categorized_data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Statement preview failed.")
            return Response(
                {"detail": "We could not read this statement. Check the file and try again."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        StatementPreview.objects.filter(user=request.user, expires_at__lte=timezone.now()).delete()
        preview = StatementPreview.objects.create(
            user=request.user,
            filename=os.path.basename(file_obj.name)[:255],
            file_sha256=file_sha256,
            transactions=categorized_data,
            summary_data=summary_data,
            warnings=warnings,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return Response(
            {
                "preview_id": preview.id,
                "filename": preview.filename,
                "statement_summary": preview.summary_data,
                "transactions": [
                    {
                        "index": index,
                        "source_row": tx.get("source_row"),
                        "source_sheet": tx.get("source_sheet"),
                        "source_page": tx.get("source_page"),
                        "date": tx["date"],
                        "description": tx["raw_description"],
                        "merchant": tx.get("merchant_name", "Unknown"),
                        "category": tx.get("category_name", "Other"),
                        "is_transfer": tx.get("is_transfer", False),
                        "amount": tx["amount"],
                    }
                    for index, tx in enumerate(categorized_data)
                ],
                "warnings": warnings,
            },
            status=status.HTTP_201_CREATED,
        )


class StatementPreviewConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        preview = StatementPreview.objects.filter(user=request.user, pk=pk).first()
        if not preview:
            return Response({"detail": "This preview was not found or has expired."}, status=status.HTTP_404_NOT_FOUND)
        if preview.is_expired:
            preview.delete()
            return Response({"detail": "This preview has expired. Upload the statement again."}, status=status.HTTP_410_GONE)
        if preview.warnings:
            return Response(
                {
                    "detail": (
                        "This preview contains rows that could not be parsed without risk of data loss. "
                        "Correct the statement or upload a cleaner CSV/Excel export before importing."
                    ),
                    "warnings": preview.warnings,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if Statement.objects.filter(user=request.user, file_sha256=preview.file_sha256).exists():
            preview.delete()
            return Response({"detail": "This statement has already been uploaded."}, status=status.HTTP_409_CONFLICT)

        try:
            with transaction.atomic():
                statement, created_transactions = _create_statement(
                    request.user,
                    preview.filename,
                    preview.file_sha256,
                    preview.transactions,
                    preview.summary_data,
                )
                preview.delete()
        except IntegrityError:
            return Response({"detail": "This statement has already been uploaded."}, status=status.HTTP_409_CONFLICT)

        return Response(
            {
                "statement_id": statement.id,
                "message": f"Successfully imported {len(created_transactions)} transactions.",
                "transaction_count": len(created_transactions),
            },
            status=status.HTTP_201_CREATED,
        )


class StatementPreviewDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        deleted, _ = StatementPreview.objects.filter(user=request.user, pk=pk).delete()
        if not deleted:
            return Response({"detail": "This preview was not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StatementUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        file_obj = request.data.get("file")
        if not file_obj:
            return Response(
                {"detail": "Choose a CSV, Excel, or PDF statement."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if file_obj.size > MAX_STATEMENT_SIZE:
            return Response({"detail": "Statement files must be 10 MB or smaller."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        content = file_obj.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if Statement.objects.filter(user=request.user, file_sha256=file_sha256).exists():
            return Response({"detail": "This statement has already been uploaded."}, status=status.HTTP_409_CONFLICT)

        try:
            categorized_data, warnings = _parse_statement_file(file_obj, content)
            summary_data = extract_statement_summary(content, file_obj.name)
            if warnings:
                return Response(
                    {
                        "detail": (
                            "This statement contains rows that could not be parsed without risk of data loss. "
                            "Use the preview flow to review the warnings before importing."
                        ),
                        "warnings": warnings,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            with transaction.atomic():
                statement, created_transactions = _create_statement(
                    request.user,
                    os.path.basename(file_obj.name)[:255],
                    file_sha256,
                    categorized_data,
                    summary_data,
                )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            return Response({"detail": "This statement has already been uploaded."}, status=status.HTTP_409_CONFLICT)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Statement upload failed.")
            return Response(
                {"detail": "We could not import this statement. Please try again."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(
            {
                "statement_id": statement.id,
                "message": f"Successfully processed {len(created_transactions)} transactions.",
                "status": "success",
            },
            status=status.HTTP_201_CREATED,
        )

class StatementListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Max, Min

        statements = (
            Statement.objects.filter(user=request.user)
            .annotate(
                transaction_count=Count("transactions"),
                period_start=Min("transactions__date"),
                period_end=Max("transactions__date"),
            )
            .order_by("-upload_date")
        )
        data = []
        for stmt in statements:
            data.append({
                "id": stmt.id,
                "filename": stmt.filename,
                "upload_date": stmt.upload_date.strftime('%b %d, %Y'),
                "transaction_count": stmt.transaction_count,
                "period_start": stmt.period_start.isoformat() if stmt.period_start else None,
                "period_end": stmt.period_end.isoformat() if stmt.period_end else None,
            })
            
        return Response(data)


class StatementDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.db.models import Sum, Count, Q
        from django.shortcuts import get_object_or_404

        stmt = get_object_or_404(Statement, pk=pk, user=request.user)
        transactions = stmt.transactions.select_related('category').order_by('date', 'id')

        # Bank-statement totals include posted transfers; category spending does not.
        posted_transactions = transactions.filter(is_pending=False)
        financial_transactions = posted_transactions.filter(is_transfer=False)
        credits = posted_transactions.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or Decimal("0.00")
        expenses = posted_transactions.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or Decimal("0.00")
        highest_spend = (
            financial_transactions.filter(amount__lt=0)
            .values("date")
            .annotate(total=Sum("amount"))
            .order_by("total", "date")
            .first()
        )
        merchant_count = (
            financial_transactions.exclude(merchant_name="")
            .values('merchant_name')
            .distinct()
            .count()
        )

        # This is a cumulative flow from zero, not an account balance.
        tx_list = []
        cumulative_flow = Decimal("0.00")
        for tx in transactions:
            if not tx.is_pending:
                cumulative_flow += tx.amount
            tx_list.append({
                "id": tx.id,
                "date": tx.date.strftime('%Y-%m-%d'),
                "date_display": tx.date.strftime('%b %d, %Y'),
                "raw_description": tx.raw_description,
                "merchant_name": tx.merchant_name or tx.raw_description[:40],
                "amount": float(tx.amount),
                "category": tx.category.name if tx.category else "Uncategorized",
                "category_color": tx.category.color if tx.category else "#9ca3af",
                "cumulative_flow": float(cumulative_flow),
                "is_transfer": tx.is_transfer,
            })

        # Category breakdown (expenses only)
        category_data = []
        from django.db.models import F
        cat_qs = financial_transactions.filter(amount__lt=0).values(
            name=F('category__name'),
            color=F('category__color')
        ).annotate(total=Sum('amount')).order_by('total')
        for c in cat_qs:
            category_data.append({
                "name": c['name'] or "Uncategorized",
                "value": float(abs(c['total']).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "color": c['color'] or "#9ca3af",
            })

        daily_map = {}
        for tx in posted_transactions.values('date', 'amount').iterator():
            date_key = tx['date'].isoformat()
            if date_key not in daily_map:
                daily_map[date_key] = {
                    "date": tx['date'].strftime('%b %d, %Y'),
                    "credits": Decimal("0.00"),
                    "expenses": Decimal("0.00"),
                }
            if tx['amount'] > 0:
                daily_map[date_key]['credits'] += tx['amount']
            else:
                daily_map[date_key]['expenses'] += abs(tx['amount'])
        daily_data = [
            {
                "date": values["date"],
                "credits": float(values["credits"]),
                "expenses": float(values["expenses"]),
            }
            for _, values in sorted(daily_map.items())
        ]

        return Response({
            "id": stmt.id,
            "filename": stmt.filename,
            "upload_date": stmt.upload_date.strftime('%B %d, %Y'),
            "stats": {
                "total_credits": float(credits.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "total_expenses": float(abs(expenses).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "net": float((credits + expenses).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "transaction_count": transactions.count(),
                "merchant_count": merchant_count,
                "highest_daily_spend": float(abs(highest_spend["total"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if highest_spend else 0.0,
                "highest_daily_spend_date": highest_spend["date"].isoformat() if highest_spend else None,
            },
            "statement_reconciliation": _statement_reconciliation(
                stmt.summary_data,
                credits,
                expenses,
            ),
            "transactions": tx_list,
            "category_breakdown": category_data,
            "daily_data": daily_data,
        })
