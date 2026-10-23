"""Fase 1 (modelo configurable de procesos) — un Ticket congela una fila por colección de su Proceso.

`ParrillaTicket.ticket` pasa de 1:1 a FK, con la `clave` y el `orden` de la colección congelados. Cada
fila existente toma la clave de la colección por defecto de su Proceso (la única que existía) y
`orden=1`; sin Proceso localizable usa la misma derivación desde su nombre de elemento."""

import django.db.models.deletion
from django.db import migrations, models


def asignar_claves(apps, schema_editor):
    from apps.catalogo.claves import clave_desde_texto

    ParrillaTicket = apps.get_model("tickets", "ParrillaTicket")
    TicketServicio = apps.get_model("tickets", "TicketServicio")
    DefinicionParrilla = apps.get_model("catalogo", "DefinicionParrilla")
    for parrilla in ParrillaTicket.objects.all().iterator():
        servicio_id = (
            TicketServicio.objects.filter(ticket_id=parrilla.ticket_id).values_list("servicio_id", flat=True).first()
        )
        clave = (
            DefinicionParrilla.objects.filter(servicio_id=servicio_id)
            .order_by("orden", "pk")
            .values_list("clave", flat=True)
            .first()
            if servicio_id
            else None
        )
        parrilla.clave = clave or clave_desde_texto(parrilla.nombre_elemento, por_defecto="elementos")
        parrilla.orden = 1
        parrilla.save(update_fields=["clave", "orden"])


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0021_tareaelemento_instancia_etapa"),
        ("catalogo", "0025_colecciones_multiples"),
    ]

    operations = [
        migrations.AlterField(
            model_name="parrillaticket",
            name="ticket",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="parrillas", to="tickets.ticket"
            ),
        ),
        migrations.AddField(
            model_name="parrillaticket",
            name="clave",
            field=models.CharField(default="elementos", max_length=60),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="parrillaticket",
            name="orden",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(asignar_claves, migrations.RunPython.noop),
        migrations.AlterModelOptions(name="parrillaticket", options={"ordering": ["orden", "pk"]}),
        migrations.AddConstraint(
            model_name="parrillaticket",
            constraint=models.UniqueConstraint(fields=("ticket", "clave"), name="uq_parrilla_ticket_clave"),
        ),
    ]
