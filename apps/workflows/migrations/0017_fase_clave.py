import re
import unicodedata

from django.db import migrations, models
from django.db.models import Q

LARGO = 60


def _base(nombre):
    texto = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9]+", "_", texto).strip("_")
    if not texto:
        return "fase"
    if not texto[0].isalpha():
        texto = f"fase_{texto}"
    return texto[:LARGO].rstrip("_") or "fase"


def _unica(base, usadas):
    if base not in usadas:
        return base
    numero = 2
    while True:
        sufijo = f"_{numero}"
        candidata = f"{base[: LARGO - len(sufijo)].rstrip('_')}{sufijo}"
        if candidata not in usadas:
            return candidata
        numero += 1


def rellenar_claves(apps, schema_editor):
    """Asigna la clave estable a las fases existentes recorriendo las versiones de cada plantilla en orden: una
    fase hereda la clave de la fase de la versión ANTERIOR con el mismo nombre (así se clonaron hasta hoy) y, si
    no la hay, recibe una clave nueva única en todo el linaje del Workflow. No borra ni mueve nada."""
    Workflow = apps.get_model("workflows", "Workflow")
    WorkflowVersion = apps.get_model("workflows", "WorkflowVersion")
    FaseWorkflow = apps.get_model("workflows", "FaseWorkflow")
    for workflow in Workflow.objects.filter(modo="PLANTILLA_FASES").order_by("pk"):
        usadas = set()
        anteriores = {}
        for version in WorkflowVersion.objects.filter(workflow=workflow).order_by("numero", "pk"):
            actuales = {}
            asignadas_en_version = set()
            for fase in FaseWorkflow.objects.filter(version=version).order_by("orden", "pk"):
                nombre = (fase.nombre or "").strip().lower()
                clave = anteriores.get(nombre)
                if clave is None or clave in asignadas_en_version:
                    clave = _unica(_base(fase.nombre), usadas)
                usadas.add(clave)
                asignadas_en_version.add(clave)
                actuales.setdefault(nombre, clave)
                FaseWorkflow.objects.filter(pk=fase.pk).update(clave=clave)
            anteriores = actuales


class Migration(migrations.Migration):
    """Fase 4.A — clave estable de `FaseWorkflow` (aditiva; relleno de datos sin borrar nada) para reconciliar la
    configuración de un Servicio cuando su plantilla de fases publica otra versión."""

    dependencies = [
        ("workflows", "0016_motivo_espera_gaceta"),
    ]

    operations = [
        migrations.AddField(
            model_name="faseworkflow",
            name="clave",
            field=models.CharField(blank=True, default="", max_length=60),
        ),
        migrations.RunPython(rellenar_claves, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="faseworkflow",
            constraint=models.UniqueConstraint(
                condition=~Q(clave=""), fields=("version", "clave"), name="uq_faseworkflow_version_clave"
            ),
        ),
    ]
