from django.db import migrations, models


def asignar_consecutivos(apps, schema_editor):
    """Los tickets ya radicados reciben su código en orden de radicación (el más antiguo es
    el 1) y el contador queda en el último número entregado. Los borradores no tienen código."""
    Ticket = apps.get_model("tickets", "Ticket")
    Consecutivo = apps.get_model("tickets", "ConsecutivoTicket")
    numero = 0
    for ticket in Ticket.objects.filter(radicado__isnull=False).order_by("radicado_en", "pk"):
        numero += 1
        Ticket.objects.filter(pk=ticket.pk).update(consecutivo=numero)
    Consecutivo.objects.update_or_create(pk=1, defaults={"ultimo": numero})


class Migration(migrations.Migration):
    """4.F2 — código público corto del Ticket (`TCK-000123`). Aditiva: agrega el consecutivo
    (NULL en borradores), el contador y rellena los tickets ya radicados en orden."""

    dependencies = [
        ("tickets", "0014_direccionamiento_ticket"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsecutivoTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ultimo", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.AddField(
            model_name="ticket",
            name="consecutivo",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.RunPython(asignar_consecutivos, migrations.RunPython.noop),
    ]
