"""Personas, equipos y áreas EXISTENTES que la demostración asigna (Bloque 5.6). Nunca se crean usuarios.

Referencias aceptadas en la línea de comandos:

- persona: su `username` (activa);
- equipo: `equipo:<nombre>` o `equipo:<id>` (activo; un nombre repetido es ambiguo y se rechaza);
- área: su `codigo`, su nombre o `area:<id>` (activa).

`Participantes` guarda los objetos resueltos; `a_parametros` / `desde_parametros` los convierten en ids para
`RecursoDemo.parametros`, así publicar y generar ejemplos usan exactamente lo que se eligió al preparar.

Un caso con personas y equipos FIJOS en su contrato (demo Imperandina) no usa la línea de comandos:
`Participantes.del_caso` resuelve sus usernames y asegura sus equipos (`asegurar_equipo`: lo crea solo si no existe
uno con ese nombre y agrega solo las membresías que falten, auditado; nunca duplica ni retira a nadie).
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.auditoria import auditar_guardado
from apps.core.models import Area, Equipo, MiembroEquipo, Usuario

PERIODOS = ("MES_ACTUAL", "MES_SIGUIENTE")


def resolver_usuario(referencia, etiqueta):
    referencia = (referencia or "").strip()
    usuario = Usuario.objects.filter(username=referencia).first()
    if usuario is None:
        raise ValidationError(f"{etiqueta}: no existe el usuario «{referencia}».")
    if not usuario.is_active:
        raise ValidationError(f"{etiqueta}: el usuario «{referencia}» no está activo.")
    return usuario


def resolver_equipo(referencia, etiqueta):
    referencia = (referencia or "").strip()
    if referencia.lower().startswith("equipo:"):
        referencia = referencia.split(":", 1)[1].strip()
    if referencia.isdigit():
        equipos = list(Equipo.objects.filter(pk=int(referencia)))
    else:
        equipos = list(Equipo.objects.filter(nombre__iexact=referencia))
    if not equipos:
        raise ValidationError(f"{etiqueta}: no existe el equipo «{referencia}».")
    if len(equipos) > 1:
        raise ValidationError(f"{etiqueta}: hay varios equipos llamados «{referencia}»; usa equipo:<id>.")
    if not equipos[0].activo:
        raise ValidationError(f"{etiqueta}: el equipo «{equipos[0].nombre}» no está activo.")
    return equipos[0]


def resolver_actor(referencia, etiqueta):
    """`(tipo, objeto)`: `equipo:…` es un equipo; cualquier otra cosa, un usuario."""
    if (referencia or "").strip().lower().startswith("equipo:"):
        return "EQUIPO", resolver_equipo(referencia, etiqueta)
    return "USUARIO", resolver_usuario(referencia, etiqueta)


def resolver_area(referencia, etiqueta):
    referencia = (referencia or "").strip()
    if referencia.lower().startswith("area:"):
        identificador = referencia.split(":", 1)[1].strip()
        areas = list(Area.objects.filter(pk=int(identificador))) if identificador.isdigit() else []
    else:
        areas = list(Area.objects.filter(codigo=referencia)) or list(Area.objects.filter(nombre__iexact=referencia))
    if len(areas) != 1:
        raise ValidationError(f"{etiqueta}: no se encontró una única área «{referencia}» (usa su código o area:<id>).")
    if not areas[0].activo:
        raise ValidationError(f"{etiqueta}: el área «{areas[0].nombre}» no está activa.")
    return areas[0]


def asegurar_equipo(spec, actor):
    """El equipo `spec.nombre` (activo) con sus `spec.miembros` como integrantes activos. Lo crea si no existe y
    agrega las membresías que falten; no toca las que ya hay ni retira a nadie. Auditado."""
    miembros = [resolver_usuario(username, f"Integrante de «{spec.nombre}»") for username in spec.miembros]
    equipos = list(Equipo.objects.filter(nombre__iexact=spec.nombre))
    if len(equipos) > 1:
        raise ValidationError(f"Hay varios equipos llamados «{spec.nombre}»: deja uno solo antes de preparar.")
    if equipos and not equipos[0].activo:
        raise ValidationError(f"El equipo «{equipos[0].nombre}» existe pero está inactivo: actívalo antes de preparar.")
    if equipos:
        equipo = equipos[0]
    else:
        equipo = Equipo.objects.create(nombre=spec.nombre, descripcion=spec.descripcion)
        auditar_guardado(equipo, actor)
    activos = set(MiembroEquipo.objects.filter(equipo=equipo, activo=True).values_list("usuario_id", flat=True))
    for usuario in miembros:
        if usuario.pk not in activos:
            membresia = MiembroEquipo.objects.create(equipo=equipo, usuario=usuario, fecha_inicio=timezone.localdate())
            auditar_guardado(membresia, actor)
    return equipo


def _lista(texto):
    return [parte.strip() for parte in (texto or "").split(",") if parte.strip()]


@dataclass
class Participantes:
    # Parrilla
    planificador: object = None
    productor: object = None
    aprobador: object = None
    responsable_publicacion: object = None
    revisores: list = field(default_factory=list)  # [(tipo, objeto)]
    aprobadores_gaceta: list = field(default_factory=list)  # [(tipo, objeto)]
    dia_creacion: int = 20
    periodo: str = "MES_SIGUIENTE"
    # Webinar
    coordinador: object = None
    aprobador_webinar: object = None
    equipo_audiovisual: object = None
    # Servicios
    equipo_diseno: object = None
    responsable_presentaciones: object = None  # (tipo, objeto)
    # Imperandina (equipos fijos del contrato)
    equipo_operativo: object = None
    equipo_revisor: object = None
    # Comunes
    area_visibilidad: object = None

    @classmethod
    def desde_opciones(cls, opciones):
        """Resuelve las opciones del comando (`{nombre: texto}`); lo que no se indicó queda vacío."""
        p = cls()

        def usuario(nombre, etiqueta):
            return resolver_usuario(opciones[nombre], etiqueta) if opciones.get(nombre) else None

        p.planificador = usuario("planificador", "--planificador")
        p.productor = usuario("productor", "--productor")
        p.aprobador = usuario("aprobador", "--aprobador")
        p.responsable_publicacion = usuario("responsable_publicacion", "--responsable-publicacion") or p.planificador
        p.revisores = [resolver_actor(r, "--revisores") for r in _lista(opciones.get("revisores"))]
        p.aprobadores_gaceta = [resolver_actor(a, "--aprobadores-gaceta") for a in _lista(opciones.get("aprobadores_gaceta"))]
        if not p.aprobadores_gaceta and p.aprobador is not None:
            p.aprobadores_gaceta = [("USUARIO", p.aprobador)]
        if opciones.get("dia_creacion") is not None:
            p.dia_creacion = int(opciones["dia_creacion"])
        if opciones.get("periodo"):
            p.periodo = opciones["periodo"]
        p.coordinador = usuario("coordinador", "--coordinador")
        p.aprobador_webinar = usuario("aprobador_webinar", "--aprobador-webinar") or p.aprobador
        if opciones.get("equipo_audiovisual"):
            p.equipo_audiovisual = resolver_equipo(opciones["equipo_audiovisual"], "--equipo-audiovisual")
        if opciones.get("equipo_diseno"):
            p.equipo_diseno = resolver_equipo(opciones["equipo_diseno"], "--equipo-diseno")
        if opciones.get("responsable_presentaciones"):
            p.responsable_presentaciones = resolver_actor(
                opciones["responsable_presentaciones"], "--responsable-presentaciones"
            )
        if opciones.get("area_visibilidad"):
            p.area_visibilidad = resolver_area(opciones["area_visibilidad"], "--area-visibilidad")
        return p

    @classmethod
    def del_caso(cls, caso, actor):
        """Participantes de un caso que los fija en su contrato (`personas`, `equipos`, `calendario`): resuelve las
        personas existentes y asegura los equipos. Escribe (equipos y membresías): se llama dentro de la
        transacción de `preparar_caso`."""
        p = cls()
        for atributo, username in caso.personas:
            setattr(p, atributo, resolver_usuario(username, f"Usuario «{username}» de «{caso.nombre}»"))
        for spec in caso.equipos:
            setattr(p, spec.atributo, asegurar_equipo(spec, actor))
        if caso.calendario:
            p.dia_creacion, p.periodo = caso.calendario
        return p

    def faltantes(self, caso):
        """Parámetros obligatorios para PREPARAR `caso` que no se indicaron."""
        from apps.catalogo.demo import contrato

        requeridos = {
            contrato.IMPERANDINA: (
                ("planificador", "lau"), ("productor", "emm"), ("aprobador", "amasso"),
                ("equipo_operativo", "equipo Mercadeo BPO"), ("equipo_revisor", "equipo Mercadeo Imperandina"),
            ),
            contrato.PARRILLA: (("planificador", "--planificador"), ("productor", "--productor"), ("aprobador", "--aprobador")),
            contrato.WEBINAR: (("coordinador", "--coordinador"), ("aprobador_webinar", "--aprobador-webinar o --aprobador")),
            contrato.PIEZA_GRAFICA: (("equipo_diseno", "--equipo-diseno"),),
            contrato.PRESENTACIONES: (("responsable_presentaciones", "--responsable-presentaciones"),),
        }[caso]
        faltan = [opcion for atributo, opcion in requeridos if getattr(self, atributo) is None]
        if contrato.CONTRATOS[caso].programado:
            if not 1 <= self.dia_creacion <= 28:
                faltan.append("--dia-creacion (de 1 a 28)")
            if self.periodo not in PERIODOS:
                faltan.append("--periodo (MES_ACTUAL o MES_SIGUIENTE)")
        return faltan

    def responsable(self, atributo):
        """`(tipo, objeto)` del responsable que atiende un caso."""
        valor = getattr(self, atributo)
        if isinstance(valor, tuple):
            return valor
        return ("EQUIPO", valor) if isinstance(valor, Equipo) else ("USUARIO", valor)

    def a_parametros(self):
        def pk(objeto):
            return objeto.pk if objeto is not None else None

        def actores(lista):
            return [[tipo, objeto.pk] for tipo, objeto in lista]

        return {
            "planificador_id": pk(self.planificador), "productor_id": pk(self.productor),
            "aprobador_id": pk(self.aprobador), "responsable_publicacion_id": pk(self.responsable_publicacion),
            "revisores": actores(self.revisores), "aprobadores_gaceta": actores(self.aprobadores_gaceta),
            "dia_creacion": self.dia_creacion, "periodo": self.periodo,
            "coordinador_id": pk(self.coordinador), "aprobador_webinar_id": pk(self.aprobador_webinar),
            "equipo_audiovisual_id": pk(self.equipo_audiovisual), "equipo_diseno_id": pk(self.equipo_diseno),
            "responsable_presentaciones": (
                [self.responsable_presentaciones[0], self.responsable_presentaciones[1].pk]
                if self.responsable_presentaciones else None
            ),
            "area_visibilidad_id": pk(self.area_visibilidad),
            "equipo_operativo_id": pk(self.equipo_operativo), "equipo_revisor_id": pk(self.equipo_revisor),
        }

    @classmethod
    def desde_parametros(cls, datos):
        """Los participantes guardados al preparar (los que ya no existan quedan vacíos)."""
        datos = datos or {}

        def usuario(clave):
            return Usuario.objects.filter(pk=datos.get(clave)).first() if datos.get(clave) else None

        def actor(par):
            tipo, pk = par
            modelo = Usuario if tipo == "USUARIO" else Equipo
            objeto = modelo.objects.filter(pk=pk).first()
            return (tipo, objeto) if objeto is not None else None

        p = cls()
        p.planificador, p.productor, p.aprobador = usuario("planificador_id"), usuario("productor_id"), usuario("aprobador_id")
        p.responsable_publicacion = usuario("responsable_publicacion_id")
        p.revisores = [a for a in (actor(x) for x in datos.get("revisores") or []) if a]
        p.aprobadores_gaceta = [a for a in (actor(x) for x in datos.get("aprobadores_gaceta") or []) if a]
        p.dia_creacion = datos.get("dia_creacion") or 20
        p.periodo = datos.get("periodo") or "MES_SIGUIENTE"
        p.coordinador, p.aprobador_webinar = usuario("coordinador_id"), usuario("aprobador_webinar_id")
        p.equipo_audiovisual = Equipo.objects.filter(pk=datos.get("equipo_audiovisual_id")).first() if datos.get("equipo_audiovisual_id") else None
        p.equipo_diseno = Equipo.objects.filter(pk=datos.get("equipo_diseno_id")).first() if datos.get("equipo_diseno_id") else None
        p.responsable_presentaciones = actor(datos["responsable_presentaciones"]) if datos.get("responsable_presentaciones") else None
        p.area_visibilidad = Area.objects.filter(pk=datos.get("area_visibilidad_id")).first() if datos.get("area_visibilidad_id") else None
        p.equipo_operativo = Equipo.objects.filter(pk=datos.get("equipo_operativo_id")).first() if datos.get("equipo_operativo_id") else None
        p.equipo_revisor = Equipo.objects.filter(pk=datos.get("equipo_revisor_id")).first() if datos.get("equipo_revisor_id") else None
        return p
