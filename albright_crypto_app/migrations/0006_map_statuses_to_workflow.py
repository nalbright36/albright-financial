"""The old statuses mapped onto the Inbox/Pipeline workflow:

  new, reviewing -> inbox        doing -> working       submitted, applied -> applied
  paid -> paid                   rejected -> closed (lost)   expired -> closed (expired)
  skipped -> skipped

Applied items get an applied date: when a draft was marked sent, if one
was - otherwise now, so the 7-day follow-up counts from the migration
rather than firing at once for old applications. Reversible (closed items
go back to rejected/expired by their close reason; saved/talking -> reviewing/applied).
"""
from django.db import migrations
from django.utils import timezone

FORWARD = {"new": "inbox", "reviewing": "inbox", "doing": "working", "submitted": "applied", "applied": "applied",
           "paid": "paid", "rejected": "closed", "expired": "closed", "skipped": "skipped"}
BACKWARD = {"inbox": "new", "saved": "reviewing", "applied": "applied", "talking": "applied", "working": "doing",
            "paid": "paid", "skipped": "skipped"}


def forward(apps, schema_editor):
    Opportunity = apps.get_model("albright_crypto_app", "Opportunity")
    ApplicationDraft = apps.get_model("albright_crypto_app", "ApplicationDraft")
    now = timezone.now()
    for old, new in FORWARD.items():
        fields = {"status": new}
        if old == "rejected":
            fields["close_reason"] = "lost"
        elif old == "expired":
            fields["close_reason"] = "expired"
        Opportunity.objects.filter(status=old).update(**fields)
    for opp in Opportunity.objects.filter(status="applied", applied_at__isnull=True):
        sent = (ApplicationDraft.objects.filter(opportunity=opp, sent_at__isnull=False)
                .order_by("sent_at").values_list("sent_at", flat=True).first())
        opp.applied_at = opp.stage_changed_at = sent or now
        opp.save(update_fields=["applied_at", "stage_changed_at"])


def backward(apps, schema_editor):
    Opportunity = apps.get_model("albright_crypto_app", "Opportunity")
    for new, old in BACKWARD.items():
        Opportunity.objects.filter(status=new).update(status=old)
    Opportunity.objects.filter(status="closed", close_reason="expired").update(status="expired")
    Opportunity.objects.filter(status="closed").update(status="rejected")


class Migration(migrations.Migration):
    dependencies = [("albright_crypto_app", "0005_inbox_pipeline_relevance")]
    operations = [migrations.RunPython(forward, backward)]
