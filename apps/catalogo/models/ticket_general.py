from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.core.models import Area, Equipo, RegistroBase


class DestinoTicketGeneral(RegistroBase):
    """Una opción seleccionable del Ticket General (4.C2): "quiero pedir algo a…".

    DESTINO ≠ RESPONSABLE. El destino es a quién va dirigida la solicitud (un
    Área, un Equipo o un Usuario REALES: no hay copias de la organización); el
    responsable es quien debe atenderla y es lo que se asigna al Ticket
    (`usuario_responsable`/`equipo_responsable`): exactamente uno de los dos. Un
    destino AREA debe indicar su responsable; en un destino EQUIPO o USUARIO, si no
    se indica, lo es el propio destino.

    Nada se elimina: un destino se desactiva, y los tickets ya radicados conservan
    su propia foto (`tickets.DireccionamientoTicket`). El objeto del destino no
    cambia nunca (para otro, se crea otro destino); sí su responsable y su estado.
    Cuál es el destino de reserva ("No estoy seguro") vive en
    `ConfiguracionTicketGeneral.destino_predeterminado`, no aquí.
    """

    class Tipo(models.TextChoices):
        AREA = "AREA", "Área"
        EQUIPO = "EQUIPO", "Equipo"
        USUARIO = "USUARIO", "Persona"

    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    area = models.ForeignKey(Area, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    responsable_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    responsable_equipo = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "destino del ticket general"
        verbose_name_plural = "destinos del ticket general"
        constraints = [
            # Un solo objeto según el tipo. Postgres acepta un CHECK que evalúa a
            # NULL: cada rama exige explícitamente el NULL/NOT NULL que corresponde.
            models.CheckConstraint(
                condition=(
                    Q(tipo="AREA", area__isnull=False, equipo__isnull=True, usuario__isnull=True)
                    | Q(tipo="EQUIPO", equipo__isnull=False, area__isnull=True, usuario__isnull=True)
                    | Q(tipo="USUARIO", usuario__isnull=False, area__isnull=True, equipo__isnull=True)
                ),
                name="ck_destinotg_tipo_coherente",
            ),
            # Exactamente un responsable: usuario o equipo, nunca ninguno ni ambos.
            models.CheckConstraint(
                condition=(
                    Q(responsable_usuario__isnull=False, responsable_equipo__isnull=True)
                    | Q(responsable_usuario__isnull=True, responsable_equipo__isnull=False)
                ),
                name="ck_destinotg_un_responsable",
            ),
            UniqueConstraint(fields=["area"], condition=Q(tipo="AREA"), name="uq_destinotg_area"),
            UniqueConstraint(fields=["equipo"], condition=Q(tipo="EQUIPO"), name="uq_destinotg_equipo"),
            UniqueConstraint(fields=["usuario"], condition=Q(tipo="USUARIO"), name="uq_destinotg_usuario"),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()}: {self.etiqueta}"

    @property
    def objeto(self):
        return {"AREA": self.area, "EQUIPO": self.equipo, "USUARIO": self.usuario}[self.tipo]

    @property
    def etiqueta(self):
        """Nombre visible del destino (el de su Área/Equipo/Persona), nunca un texto
        copiado: el que se congela al radicar vive en el direccionamiento del ticket."""
        objeto = self.objeto
        if self.tipo == self.Tipo.USUARIO:
            return objeto.get_full_name() or objeto.get_username()
        return objeto.nombre

    @property
    def responsable(self):
        return self.responsable_usuario or self.responsable_equipo

    @property
    def responsable_etiqueta(self):
        if self.responsable_usuario_id is not None:
            usuario = self.responsable_usuario
            return usuario.get_full_name() or usuario.get_username()
        return self.responsable_equipo.nombre


class ConfiguracionTicketGeneral(RegistroBase):
    """Configuración global del Ticket General (4.C1): una sola fila.

    Guarda ÚNICAMENTE lo que pertenece al comportamiento especial del Ticket
    General: si está habilitado y (4.C2) cuál es su destino de reserva. Todo lo demás vive donde ya vive: el formulario,
    el tiempo objetivo, la política de prórroga, la de entrega y la visibilidad
    son atributos del Servicio interno (`Servicio.es_ticket_general`) y se editan
    en Studio como los de cualquier otro Servicio — no se duplican aquí. Cuál es
    ese Servicio lo dice su marca (única por constraint), no una FK paralela:
    así hay una sola fuente de verdad.

    Una sola fila (`PK_UNICA`), mismo patrón que `ConfiguracionSistema`.
    `actual()` nunca escribe: sin fila guardada devuelve una instancia en memoria
    deshabilitada. El Ticket General nace deshabilitado: habilitarlo es una
    decisión explícita y auditada.
    """

    PK_UNICA = 1

    habilitado = models.BooleanField(default=False)
    # 4.C2 — destino de reserva ("No estoy seguro"/sin elegir): UNA sola fuente de
    # verdad, y como esta tabla tiene una sola fila no puede haber dos. Debe estar
    # activo y ser utilizable para ofrecerse; si no, el destino es obligatorio.
    destino_predeterminado = models.ForeignKey(
        DestinoTicketGeneral, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = "configuración del ticket general"
        verbose_name_plural = "configuración del ticket general"

    def __str__(self):
        return "Ticket general (habilitado)" if self.habilitado else "Ticket general (deshabilitado)"

    def save(self, *args, **kwargs):
        self.pk = self.PK_UNICA
        if self.creado_en is None:
            # Una instancia nueva sobre la fila única existente se guarda como UPDATE:
            # sin esto `auto_now_add` no se aplica y `creado_en` se escribiría nulo.
            self.creado_en = (
                type(self).objects.filter(pk=self.PK_UNICA).values_list("creado_en", flat=True).first()
            )
        super().save(*args, **kwargs)

    @classmethod
    def actual(cls):
        return cls.objects.filter(pk=cls.PK_UNICA).first() or cls(pk=cls.PK_UNICA)

    @classmethod
    def bloquear(cls):
        """La fila única bloqueada (se crea si todavía no existe). Serializa las
        operaciones que cambian el Servicio interno o la habilitación; solo dentro
        de una transacción."""
        cls.objects.get_or_create(pk=cls.PK_UNICA)
        return cls.objects.select_for_update().get(pk=cls.PK_UNICA)
