"""Diagnóstico de configuración: no modifica datos ni genera RegistroAuditoria."""

import json

from django.core.management.base import BaseCommand

from apps.catalogo.operaciones import diagnosticar_activos_incompletos


class Command(BaseCommand):
    help = "Reporta servicios/procesos activos incompletos sin modificar registros."
    requires_system_checks = []

    def handle(self, *args, **options):
        hallazgos = list(diagnosticar_activos_incompletos())
        self.stdout.write(json.dumps({"cantidad": len(hallazgos), "hallazgos": hallazgos}, ensure_ascii=False, indent=2))
