from decimal import Decimal
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Case, CharField, Count, F, Max, Q, Sum, Value, When
from django.utils import timezone
from analysis.models import Statement, StatementImportAttempt
from transactions.models import Account, Transaction


def confirmed_non_transfer_transactions(user_id):
    return Transaction.objects.filter(
        user_id=user_id,
        is_transfer=False,
        is_pending=False,
    )


def latest_statement_report(user_id):
    statement = Statement.objects.filter(user_id=user_id).order_by("-upload_date", "-id").first()
    summary = statement.summary_data if statement and isinstance(statement.summary_data, dict) else {}
    unavailable = set(summary.get("ambiguous_fields", []))
    unavailable.update(summary.get("incomplete_fields", []))
    fields = (
        "opening_balance",
        "closing_balance",
        "total_credits",
        "total_debits",
        "credit_count",
        "debit_count",
    )
    row_totals = Transaction.objects.filter(
        statement=statement,
        is_pending=False,
    ).aggregate(
        total_credits=Sum("amount", filter=Q(amount__gt=0)),
        total_debits=Sum("amount", filter=Q(amount__lt=0)),
        credit_count=Count("id", filter=Q(amount__gt=0)),
        debit_count=Count("id", filter=Q(amount__lt=0)),
    ) if statement else {
        "total_credits": None,
        "total_debits": None,
        "credit_count": 0,
        "debit_count": 0,
    }
    row_totals["total_credits"] = row_totals["total_credits"] or Decimal("0.00")
    row_totals["total_debits"] = abs(row_totals["total_debits"] or Decimal("0.00"))
    comparison = {}
    for field in fields[2:]:
        reported = summary.get(field) if field not in unavailable else None
        row_value = row_totals[field]
        if field in ("total_credits", "total_debits"):
            row_value = row_value.quantize(Decimal("0.01"))
            row_value = str(row_value)
        else:
            row_value = int(row_value)
        if reported is None:
            comparison[field] = {
                "status": "unavailable",
                "transaction_rows": row_value,
                "difference": None,
            }
            continue
        difference = Decimal(str(row_value)) - Decimal(str(reported))
        if field in ("total_credits", "total_debits"):
            difference = difference.quantize(Decimal("0.01"))
        else:
            difference = int(difference)
        comparison[field] = {
            "status": "match" if difference == 0 else "mismatch",
            "transaction_rows": row_value,
            "difference": str(difference) if field in ("total_credits", "total_debits") else difference,
        }
    return {
        "filename": statement.filename if statement else None,
        "ambiguous_fields": sorted(set(summary.get("ambiguous_fields", []))),
        "incomplete_fields": sorted(set(summary.get("incomplete_fields", []))),
        **{
            field: (
                str(summary[field])
                if field not in unavailable and summary.get(field) is not None
                else None
            )
            for field in fields
        },
        "comparison": comparison,
    }


class DashboardOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        txs = Transaction.objects.filter(user_id=user_id, is_pending=False)
        latest_statement_metrics = latest_statement_report(user_id)
        totals = txs.aggregate(
            credits=Sum("amount", filter=Q(amount__gt=0)),
            debits=Sum("amount", filter=Q(amount__lt=0)),
        )
        transaction_credits = totals["credits"] or Decimal("0.00")
        transaction_debits = totals["debits"] or Decimal("0.00")

        summarized_statement_totals = {"total_credits": {}, "total_debits": {}}
        for statement in Statement.objects.filter(user_id=user_id).only("id", "summary_data"):
            summary = statement.summary_data if isinstance(statement.summary_data, dict) else {}
            unavailable_fields = set(summary.get("ambiguous_fields", []))
            unavailable_fields.update(summary.get("incomplete_fields", []))
            for field in summarized_statement_totals:
                if field in unavailable_fields or summary.get(field) is None:
                    continue
                summarized_statement_totals[field][statement.id] = abs(
                    Decimal(str(summary[field]))
                )

        summarized_statement_ids = set().union(
            *(set(values) for values in summarized_statement_totals.values())
        )
        statement_row_totals = {
            row["statement_id"]: {
                "total_credits": row["credits"] or Decimal("0.00"),
                "total_debits": abs(row["debits"] or Decimal("0.00")),
            }
            for row in txs.filter(statement_id__in=summarized_statement_ids)
            .values("statement_id")
            .annotate(
                credits=Sum("amount", filter=Q(amount__gt=0)),
                debits=Sum("amount", filter=Q(amount__lt=0)),
            )
        }

        statement_totals_comparison = {}
        mismatch_fields = []
        for field, comparison_name in (
            ("total_credits", "credits"),
            ("total_debits", "debits"),
        ):
            reported_by_statement = summarized_statement_totals[field]
            reported_total = sum(reported_by_statement.values(), Decimal("0.00"))
            transaction_total = sum(
                (
                    statement_row_totals.get(statement_id, {}).get(field, Decimal("0.00"))
                    for statement_id in reported_by_statement
                ),
                Decimal("0.00"),
            )
            has_mismatch = any(
                statement_row_totals.get(statement_id, {}).get(field, Decimal("0.00"))
                != reported
                for statement_id, reported in reported_by_statement.items()
            )
            if has_mismatch:
                mismatch_fields.append(comparison_name)
            statement_totals_comparison[comparison_name] = {
                "reported": float(reported_total) if reported_by_statement else None,
                "transaction_rows": float(transaction_total) if reported_by_statement else None,
                "statement_count": len(reported_by_statement),
                "mismatch": has_mismatch,
            }

        net_cash_flow = transaction_credits + transaction_debits
        savings_rate = Decimal("0")
        if transaction_credits > 0:
            savings_rate = net_cash_flow / transaction_credits * Decimal("100")

        largest_spend_day = (
            confirmed_non_transfer_transactions(user_id).filter(amount__lt=0)
            .values("date")
            .annotate(total=Sum("amount"))
            .order_by("total", "date")
            .first()
        )
        highest_daily_spend = Decimal("0.00")
        highest_daily_spend_date = None
        if largest_spend_day:
            highest_daily_spend = abs(largest_spend_day["total"])
            highest_daily_spend_date = largest_spend_day["date"].strftime("%b %d, %Y")

        top_payee_row = (
            confirmed_non_transfer_transactions(user_id).filter(amount__lt=0)
            .values("merchant_name")
            .annotate(total=Sum("amount"))
            .order_by("total", "merchant_name")
            .first()
        )
        top_payee = None
        top_payee_amount = Decimal("0.00")
        if top_payee_row:
            top_payee = top_payee_row["merchant_name"] or "Unknown"
            top_payee_amount = abs(top_payee_row["total"])

        today = timezone.localdate()
        period = request.query_params.get("period", "month")
        if period == "90days":
            period_start = today - timedelta(days=89)
            previous_end = period_start - timedelta(days=1)
            previous_start = previous_end - timedelta(days=89)
        elif period == "year":
            period_start = today.replace(month=1, day=1)
            previous_end = period_start - timedelta(days=1)
            previous_start = previous_end.replace(month=1, day=1)
        else:
            period = "month"
            period_start = today.replace(day=1)
            previous_end = period_start - timedelta(days=1)
            previous_start = previous_end.replace(day=1)
        period_transactions = confirmed_non_transfer_transactions(user_id)
        currency_expression = Case(
            When(currency="", then=Value("NGN")),
            default=F("currency"),
            output_field=CharField(),
        )

        def period_totals(start, end):
            totals = period_transactions.filter(date__range=(start, end)).annotate(
                currency_code=currency_expression,
            ).values("currency_code").annotate(
                income=Sum("amount", filter=Q(amount__gt=0)),
                spending=Sum("amount", filter=Q(amount__lt=0)),
            )
            return {
                row["currency_code"]: {
                    "income": row["income"] or Decimal("0.00"),
                    "spending": abs(row["spending"] or Decimal("0.00")),
                }
                for row in totals
            }

        current_totals = period_totals(period_start, today)
        previous_totals = period_totals(previous_start, previous_end)
        period_currencies = sorted(set(current_totals) | set(previous_totals))
        cash_flow = []
        for currency in period_currencies:
            current = current_totals.get(currency, {"income": Decimal("0.00"), "spending": Decimal("0.00")})
            previous = previous_totals.get(currency, {"income": Decimal("0.00"), "spending": Decimal("0.00")})
            current_net = current["income"] - current["spending"]
            previous_net = previous["income"] - previous["spending"]
            change_percent = (
                (current_net - previous_net) / abs(previous_net) * Decimal("100")
                if previous_net
                else None
            )
            cash_flow.append({
                "currency": currency,
                "income": str(current["income"].quantize(Decimal("0.01"))),
                "spending": str(current["spending"].quantize(Decimal("0.01"))),
                "net": str(current_net.quantize(Decimal("0.01"))),
                "previous_income": str(previous["income"].quantize(Decimal("0.01"))),
                "previous_spending": str(previous["spending"].quantize(Decimal("0.01"))),
                "previous_net": str(previous_net.quantize(Decimal("0.01"))),
                "net_change_percent": (
                    str(change_percent.quantize(Decimal("0.01")))
                    if change_percent is not None
                    else None
                ),
            })

        seen_account_currencies = set()
        current_balance_totals = {}
        for statement in Statement.objects.filter(user_id=user_id).order_by("-upload_date", "-id"):
            summary = statement.summary_data if isinstance(statement.summary_data, dict) else {}
            closing_balance = summary.get("closing_balance")
            account_rows = txs.filter(statement=statement, account__isnull=False).values(
                "account_id", "account__name", "currency"
            ).distinct()
            for account_row in account_rows:
                currency = (
                    summary.get("currency")
                    or account_row["currency"]
                    or "NGN"
                )
                account_key = (account_row["account_id"], currency)
                if account_key in seen_account_currencies:
                    continue
                seen_account_currencies.add(account_key)
                if closing_balance is None:
                    continue
                bucket = current_balance_totals.setdefault(
                    currency,
                    {"amount": Decimal("0.00"), "account_count": 0, "accounts": []},
                )
                bucket["amount"] += Decimal(str(closing_balance))
                bucket["account_count"] += 1
                bucket["accounts"].append(account_row["account__name"])

        current_balances = [
            {
                "currency": currency,
                "amount": str(bucket["amount"].quantize(Decimal("0.01"))),
                "account_count": bucket["account_count"],
                "accounts": bucket["accounts"],
            }
            for currency, bucket in sorted(current_balance_totals.items())
        ]
        stale_before = timezone.now() - timedelta(days=30)
        stale_account_count = sum(
            1
            for account in Account.objects.filter(user_id=user_id).annotate(
                latest_activity=Max("transactions__statement__upload_date"),
            )
            if account.latest_activity is None or account.latest_activity < stale_before
        )
        failed_import_count = StatementImportAttempt.objects.filter(
            user_id=user_id,
            status="failed",
            created_at__gte=timezone.now() - timedelta(days=30),
        ).count()
        uncategorized_count = txs.filter(category__isnull=True).count()

        return Response({
            "net_cash_flow": float(net_cash_flow),
            "credits_all_time": float(transaction_credits),
            "expenses_all_time": float(abs(transaction_debits)),
            "transaction_credits_all_time": float(transaction_credits),
            "transaction_expenses_all_time": float(abs(transaction_debits)),
            "statement_totals_comparison": statement_totals_comparison,
            "statement_totals_mismatch": bool(mismatch_fields),
            "statement_totals_mismatch_fields": mismatch_fields,
            "latest_statement_metrics": latest_statement_metrics,
            "savings_rate": float(savings_rate),
            "highest_daily_spend": float(highest_daily_spend),
            "highest_daily_spend_date": highest_daily_spend_date,
            "top_payee": top_payee,
            "top_payee_amount": float(top_payee_amount),
            "period": {
                "key": period,
                "start": period_start.isoformat(),
                "end": today.isoformat(),
                "previous_start": previous_start.isoformat(),
                "previous_end": previous_end.isoformat(),
            },
            "cash_flow_by_currency": cash_flow,
            "current_balances": current_balances,
            "attention_needed": {
                "failed_imports": failed_import_count,
                "stale_accounts": stale_account_count,
                "uncategorized_transactions": uncategorized_count,
            },
        })


class CategoryBreakdownView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        breakdown = confirmed_non_transfer_transactions(request.user.id).filter(
            amount__lt=0,
        ).values(
            name=F('category__name'),
            color=F('category__color')
        ).annotate(
            value=Sum('amount')
        ).order_by('value')

        data = [
            {
                "name": item['name'] or 'Uncategorized',
                "value": float(abs(item['value'])),
                "color": item['color'] or '#9ca3af'
            }
            for item in breakdown
        ]

        return Response(sorted(data, key=lambda x: x['value'], reverse=True))

class CashFlowView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        transactions = Transaction.objects.filter(
            user_id=request.user.id,
            is_pending=False,
        ).order_by("date")
        daily_stats = {}
        for tx in transactions.only("date", "amount").iterator():
            date_key = tx.date.isoformat()
            if date_key not in daily_stats:
                daily_stats[date_key] = {
                    "income": Decimal("0.00"),
                    "expenses": Decimal("0.00"),
                }

            if tx.amount > 0:
                daily_stats[date_key]["income"] += tx.amount
            else:
                daily_stats[date_key]["expenses"] += abs(tx.amount)

        data = []
        for day, stats in sorted(daily_stats.items()):
            data.append({
                "date": day,
                "income": float(stats["income"]),
                "expenses": float(stats["expenses"]),
            })

        return Response(data)


class TopMerchantsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        merchants = confirmed_non_transfer_transactions(request.user.id).filter(
            amount__lt=0,
        ).values(
            'merchant_name', 
            cat_name=F('category__name')
        ).annotate(
            amount=Sum('amount'),
            count=Count('id')
        ).order_by('amount')[:5]

        data = [
            {
                "name": m['merchant_name'],
                "amount": float(abs(m['amount'])),
                "count": m['count'],
                "category": m['cat_name'] or 'Uncategorized',
                "logo": m['merchant_name'][:2].upper() if m['merchant_name'] else '??'
            }
            for m in merchants
        ]

        return Response(data)
