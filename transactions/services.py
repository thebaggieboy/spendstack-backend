import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction

from .models import Category, Transaction, TransactionRule


_TRANSFER_PATTERN = re.compile(
    r"\bNIP\s*[/\- ]?\s*(?:TRF|TRANSFER)\b|\b(?:TRF|XFER)\s*[/:\-]|"
    r"\b(?:TRANSFER|XFER|TRF)\s+(?:TO|FROM|BETWEEN|INTO|OUT\s+OF)\b|"
    r"\b(?:MOBILE|INTERNAL|OWN\s+ACCOUNT|SELF|BANK|INTERBANK|INSTANT)\s+"
    r"(?:FUND\s+)?TRANSFER\b",
    re.IGNORECASE,
)
_TRANSFER_FEE_PATTERN = re.compile(
    r"\b(?:TRANSFER|TRF|XFER)\b.{0,24}\b(?:FEE|CHARGE|LEVY|VAT|DUTY)\b",
    re.IGNORECASE,
)


def is_transfer_description(description):
    text = str(description or "")
    return bool(_TRANSFER_PATTERN.search(text) and not _TRANSFER_FEE_PATTERN.search(text))


def _rule_matches(rule, description, merchant):
    needle = rule.match_text.casefold().strip()
    if not needle:
        return False
    values = {
        TransactionRule.MATCH_DESCRIPTION: [description],
        TransactionRule.MATCH_MERCHANT: [merchant],
        TransactionRule.MATCH_EITHER: [description, merchant],
    }[rule.match_field]
    return any(needle in str(value or "").casefold() for value in values)


def classify_transaction(description, merchant, category_name, rules):
    original_description = str(description or "")
    merchant_name = str(merchant or "")
    category = str(category_name or "Other")
    is_transfer = is_transfer_description(original_description)

    merchant_overridden = False
    category_overridden = False
    transfer_overridden = False
    for rule in rules:
        if not rule.is_active or not _rule_matches(rule, original_description, merchant_name):
            continue
        if rule.merchant_name and not merchant_overridden:
            merchant_name = rule.merchant_name
            merchant_overridden = True
        if rule.category_name and not category_overridden:
            category = rule.category_name
            category_overridden = True
        if rule.transfer_action == TransactionRule.TRANSFER_YES and not transfer_overridden:
            is_transfer = True
            transfer_overridden = True
        elif rule.transfer_action == TransactionRule.TRANSFER_NO and not transfer_overridden:
            is_transfer = False
            transfer_overridden = True

    if is_transfer:
        category = "Transfers"
    elif category.casefold() == "transfers" and not category_overridden:
        category = "Other"
    return merchant_name, category, is_transfer


def reapply_transaction_rules(user):
    rules = list(
        TransactionRule.objects.filter(user=user, is_active=True).order_by("-priority", "id")
    )
    transactions = Transaction.objects.filter(user=user).select_related("category")
    with db_transaction.atomic():
        for item in transactions.iterator():
            merchant, category_name, is_transfer = classify_transaction(
                item.raw_description,
                item.original_merchant_name or item.merchant_name,
                item.original_category_name or (
                    item.category.name if item.category_id else "Other"
                ),
                rules,
            )
            category, _ = Category.objects.get_or_create(
                name=category_name,
                defaults={"color": "#9ca3af", "is_system": category_name == "Transfers"},
            )
            item.merchant_name = merchant
            item.category = category
            item.is_transfer = is_transfer
            item.save(update_fields=["merchant_name", "category", "is_transfer"])


def detect_recurring_bills(transactions):
    grouped = defaultdict(list)
    for item in transactions:
        if item.amount >= 0 or item.is_transfer:
            continue
        merchant = (item.merchant_name or item.raw_description).casefold()
        key = re.sub(r"\d+", " ", merchant)
        key = re.sub(r"[^a-z]+", " ", key)
        key = " ".join(key.split())
        if key:
            grouped[key].append(item)

    bills = []
    for items in grouped.values():
        items.sort(key=lambda item: item.date)
        if len(items) < 3:
            continue
        intervals = [
            (current.date - previous.date).days
            for previous, current in zip(items, items[1:])
        ]
        average_interval = sum(intervals) / len(intervals)
        if 6 <= average_interval <= 8:
            cadence, cadence_days = "weekly", 7
        elif 13 <= average_interval <= 16:
            cadence, cadence_days = "biweekly", 14
        elif 25 <= average_interval <= 35:
            cadence, cadence_days = "monthly", 30
        else:
            continue

        amounts = [abs(Decimal(item.amount)) for item in items]
        average_amount = sum(amounts) / len(amounts)
        if average_amount and max(amounts) - min(amounts) > average_amount * Decimal("0.35"):
            continue

        latest = items[-1]
        bills.append({
            "merchant": latest.merchant_name or latest.raw_description[:80],
            "category": latest.category.name if latest.category else "Uncategorized",
            "average_amount": round(float(average_amount), 2),
            "last_payment_date": latest.date.isoformat(),
            "next_expected_date": (latest.date + timedelta(days=cadence_days)).isoformat(),
            "cadence": cadence,
            "occurrences": len(items),
        })

    return sorted(bills, key=lambda bill: bill["next_expected_date"])
