"""One durable checkpoint shared by web requests and the scheduled command."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

from .models import Adjustment, Allocation, BillingCheckpoint, Charge, Payment, RecurringRule, Tenancy


def invalidate_billing(sender, using, **kwargs):
    if not kwargs.get("raw"):
        BillingCheckpoint.objects.using(using).filter(pk=1, through_date__isnull=False).update(through_date=None)


def connect_signals():
    for model in (Tenancy, RecurringRule, Charge, Payment, Allocation, Adjustment):
        for signal in (post_save, post_delete):
            signal.connect(invalidate_billing, sender=model, dispatch_uid=f"billing-checkpoint-{model._meta.label}-{id(signal)}")


def ensure_billing(through_date, *, force=False):
    from .services import generate_due_charges
    today = timezone.localdate()
    checkpoint = BillingCheckpoint.objects.filter(pk=1).first()
    if not force and checkpoint and checkpoint.completed_on == today and checkpoint.through_date and checkpoint.through_date >= through_date:
        return []
    with transaction.atomic():
        checkpoint, _ = BillingCheckpoint.objects.select_for_update().get_or_create(pk=1)
        if not force and checkpoint.completed_on == today and checkpoint.through_date and checkpoint.through_date >= through_date:
            return []
        result = generate_due_charges(through_date, refresh_rooms=False)
        # Set only after successful completion; a failure rolls back the checkpoint too.
        checkpoint.completed_on, checkpoint.through_date = today, through_date
        checkpoint.save(update_fields=["completed_on", "through_date"])
        return result
