import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from savings.models import Schedule
from savings.paystack import PaystackError
from savings.services import initiate_schedule_payout


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Submit due automatic lock payouts to Paystack."

    def handle(self, *args, **options):
        due_schedules = Schedule.objects.filter(
            auto_payout=True,
            status=Schedule.PENDING,
            scheduled_date__lte=timezone.now(),
        ).values_list("id", flat=True)
        submitted = 0
        for schedule_id in due_schedules.iterator():
            try:
                initiate_schedule_payout(schedule_id)
                submitted += 1
            except PaystackError:
                logger.exception("Automatic payout could not be initiated for schedule %s.", schedule_id)
        self.stdout.write(self.style.SUCCESS(f"Submitted {submitted} due payout(s)."))
