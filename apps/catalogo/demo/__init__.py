"""Bloque 5.6 — preparación automática de la demostración de Órbita (`manage.py preparar_demo_orbita`).

Crea, verifica, publica, genera ejemplos y retira los cuatro casos de `docs/demo/ORBITA_DEMO.md` con las MISMAS
operaciones de dominio que usa el Studio (permisos, validaciones y auditoría incluidos): nunca inserta
configuración directamente. Cada caso queda identificado por `catalogo.RecursoDemo`; nada ajeno a la demo se
lee como propio ni se modifica.

- `contrato`: los cuatro casos como DATOS (columnas, campos, entregables, gaceta, fases, actividades, rutas).
  Lo usan tanto la preparación como la verificación.
- `participantes`: personas, equipos y áreas EXISTENTES que se asignan por parámetro (no se crean usuarios).
- `preparacion`: construye un caso en una transacción (todo o nada). No publica.
- `fases`: verificar (sin cambios), publicar, ejemplos y retirar (sin borrados físicos).
"""
