import hashlib
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from .utils.parser import parse_csv_statement_detailed, parse_pdf_statement
from .utils.ai_categorizer import categorize_transactions
from transactions.models import Transaction, Account, Category
from .models import Statement, StatementPreview
from transactions.models import TransactionRule
from transactions.services import classify_transaction


MAX_STATEMENT_SIZE = 10 * 1024 * 1024


def _parse_statement_file(file_obj, content):
    filename = os.path.basename(file_obj.name)
    extension = os.path.splitext(filename)[1].lower()
    warnings = []
    if extension == ".csv":
        raw_data, warnings = parse_csv_statement_detailed(content)
    elif extension == ".pdf":
        import io
        raw_data = parse_pdf_statement(io.BytesIO(content))
    else:
        raise ValueError("Unsupported file format. Use CSV or PDF.")

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
            tx["amount"] = str(amount.quantize(Decimal("0.01")))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            warnings.append(f"Transaction {index + 1}: invalid date or amount; skipped.")
            tx["_invalid"] = True
        if not isinstance(tx.get("raw_description"), str) or not tx["raw_description"].strip():
            warnings.append(f"Transaction {index + 1}: missing description; skipped.")
            tx["_invalid"] = True
        tx["raw_description"] = str(tx.get("raw_description", ""))[:500]

    raw_data = [tx for tx in raw_data if not tx.pop("_invalid", False)]
    if not raw_data:
        raise ValueError("No valid transactions were found in this statement.")
    for index, tx in enumerate(raw_data):
        tx["original_index"] = index
    categorized_data = categorize_transactions(raw_data)
    for tx in categorized_data:
        tx["merchant_name"] = str(tx.get("merchant_name") or "Unknown")[:255]
        tx["category_name"] = str(tx.get("category_name") or "Other")[:100]
    return categorized_data, warnings


def _create_statement(user, filename, file_sha256, categorized_data):
    account, _ = Account.objects.get_or_create(name="Default Checking", user=user)
    statement = Statement.objects.create(
        user=user,
        filename=filename,
        file_sha256=file_sha256,
    )
    rules = list(
        TransactionRule.objects.filter(user=user, is_active=True).order_by("-priority", "id")
    )
    created_transactions = []
    for tx in categorized_data:
        base_merchant = tx.get("original_merchant_name", tx.get("merchant_name", "Unknown"))
        base_category = tx.get("original_category_name", tx.get("category_name", "Other"))
        merchant_name, cat_name, is_transfer = classify_transaction(
            tx.get("raw_description", ""),
            base_merchant,
            base_category,
            rules,
        )
        cat, _ = Category.objects.get_or_create(
            name=cat_name,
            defaults={"color": "#9ca3af", "is_system": cat_name == "Transfers"},
        )
        obj = Transaction.objects.create(
            user=user,
            account=account,
            statement=statement,
            date=tx["date"],
            amount=tx["amount"],
            merchant_name=merchant_name,
            original_merchant_name=base_merchant,
            original_category_name=base_category,
            raw_description=tx["raw_description"],
            category=cat,
            is_transfer=is_transfer,
        )
        created_transactions.append(obj.id)
    return statement, created_transactions


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
            return Response({"detail": "Choose a CSV or PDF statement."}, status=status.HTTP_400_BAD_REQUEST)
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
            warnings=warnings,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        return Response(
            {
                "preview_id": preview.id,
                "filename": preview.filename,
                "transactions": [
                    {
                        "index": index,
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
            return Response({"detail": "Choose a CSV or PDF statement."}, status=status.HTTP_400_BAD_REQUEST)
        if file_obj.size > MAX_STATEMENT_SIZE:
            return Response({"detail": "Statement files must be 10 MB or smaller."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        content = file_obj.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if Statement.objects.filter(user=request.user, file_sha256=file_sha256).exists():
            return Response({"detail": "This statement has already been uploaded."}, status=status.HTTP_409_CONFLICT)

        try:
            categorized_data, _ = _parse_statement_file(file_obj, content)
            with transaction.atomic():
                statement, created_transactions = _create_statement(
                    request.user,
                    os.path.basename(file_obj.name)[:255],
                    file_sha256,
                    categorized_data,
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
        statements = Statement.objects.filter(user=request.user).order_by('-upload_date')
        data = []
        for stmt in statements:
            tx_count = stmt.transactions.count()
            data.append({
                "id": stmt.id,
                "filename": stmt.filename,
                "upload_date": stmt.upload_date.strftime('%b %d, %Y'),
                "transaction_count": tx_count
            })
            
        return Response(data)


class StatementDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.db.models import Sum, Count, Q
        from django.shortcuts import get_object_or_404

        stmt = get_object_or_404(Statement, pk=pk, user=request.user)
        transactions = stmt.transactions.select_related('category').order_by('date', 'id')

        # Aggregate stats
        financial_transactions = transactions.filter(is_transfer=False)
        income = financial_transactions.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or 0
        expenses = financial_transactions.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or 0
        merchant_count = financial_transactions.values('merchant_name').distinct().count()

        # Build running balance timeline
        tx_list = []
        running_balance = 0.0
        for tx in transactions:
            running_balance += float(tx.amount)
            tx_list.append({
                "id": tx.id,
                "date": tx.date.strftime('%Y-%m-%d'),
                "date_display": tx.date.strftime('%b %d'),
                "raw_description": tx.raw_description,
                "merchant_name": tx.merchant_name or tx.raw_description[:40],
                "amount": float(tx.amount),
                "category": tx.category.name if tx.category else "Uncategorized",
                "category_color": tx.category.color if tx.category else "#9ca3af",
                "running_balance": round(running_balance, 2),
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
                "value": round(float(abs(c['total'])), 2),
                "color": c['color'] or "#9ca3af",
            })

        # Daily spending aggregation
        daily_map: dict = {}
        for tx in tx_list:
            if tx['is_transfer']:
                continue
            d = tx['date_display']
            if d not in daily_map:
                daily_map[d] = {"date": d, "income": 0.0, "expenses": 0.0}
            if tx['amount'] > 0:
                daily_map[d]['income'] += tx['amount']
            else:
                daily_map[d]['expenses'] += abs(tx['amount'])
        daily_data = list(daily_map.values())

        return Response({
            "id": stmt.id,
            "filename": stmt.filename,
            "upload_date": stmt.upload_date.strftime('%B %d, %Y'),
            "stats": {
                "total_income": round(float(income), 2),
                "total_expenses": round(float(abs(expenses)), 2),
                "net": round(float(income) + float(expenses), 2),
                "transaction_count": transactions.count(),
                "merchant_count": merchant_count,
            },
            "transactions": tx_list,
            "category_breakdown": category_data,
            "daily_data": daily_data,
        })
