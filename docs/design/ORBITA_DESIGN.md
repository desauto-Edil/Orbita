# Órbita — Sistema visual (referencia para el Studio y los módulos siguientes)

> Fase 4.B. Fuente de referencia visual. Los valores viven en `static/css/tokens.css` (capas: paleta primitiva →
> tokens semánticos → alias legados); este documento explica **qué** usar y **cuándo**. Ningún componente
> escribe colores, radios o sombras directamente: usa tokens semánticos.

## 1. Principios

1. **Producto, no documento.** Las herramientas de configuración (Studio, lienzo de flujos) son aplicaciones: la
   estructura (encabezado, navegación, barra de acciones) es estable y lo que cambia es el área de trabajo.
2. **Densidad funcional con aire.** Tarjetas compactas, bordes discretos y chips (de Passionfroot); superficies
   luminosas, jerarquía tipográfica clara y espacios equilibrados (de Geniestudio). Sin ilustraciones, mascotas,
   titulares gigantes ni gradientes decorativos.
3. **El borde separa, la sombra apenas eleva.** Cada bloque se entiende por su borde gris sutil; la sombra solo
   indica que algo flota (popover, inspector, tarjeta seleccionada).
4. **Un solo acento.** El lima (`--color-primary`) marca la acción principal, la selección y los indicadores.
   Nunca como superficie grande ni como color de texto; siempre con texto oscuro encima.
5. **Divulgación progresiva.** Primero lo habitual; lo avanzado se abre a pedido («Opciones avanzadas»).
6. **Honestidad.** La interfaz muestra el comportamiento real: no dibuja conexiones que el motor no ejecuta, no dice
   «Guardado» si hay cambios pendientes ni «Publicado» si no cambió nada.

## 2. Color

| Rol | Token | Valor | Uso |
|---|---|---|---|
| Fondo general | `--surface-page` | `#f2f3f5` gris neutro muy claro | Lienzo de la aplicación (ya no crema) |
| Superficie | `--surface-card` | `#fdfdfd` blanco suavizado | Paneles, tarjetas, inspector |
| Superficie elevada | `--surface-raised` | `#ffffff` | Popovers, modales, tarjeta en arrastre |
| Panel secundario | `--surface-sunken` | `#f8f9fa` | Navegación lateral, barras, zonas de agrupación |
| Hover | `--surface-hover` | `#eceef1` | Filas y botones fantasma al pasar |
| Borde | `--border-subtle` | `#e2e4e8` | Separación entre componentes (obligatoria) |
| Borde fuerte | `--border-strong` | `#cdd1d6` | Inputs, divisores de columnas del lienzo |
| Texto | `--text-primary` / `--text-secondary` / `--text-muted` | carbón `#17181b` y grises cálidos | Identidad de Órbita, sin cambios |
| Acción principal | `--color-primary` | lima `#d7ff3e` | CTA primario, selección, progreso |
| Estados | `--status-success|warning|danger|info` (+ `-soft`) | | Siempre con texto o icono, nunca solo color |

Los grises cálidos de la paleta original (`--palette-neutral-*`) siguen dando el tono del texto; los fondos y
bordes pasan a la paleta neutra `--palette-gray-*`.

## 3. Tipografía

Inter (o la fuente de interfaz del sistema). Pesos 400/500/600; 700 solo en cifras o marca.

| Rol | Tamaño | Peso | Ejemplo |
|---|---|---|---|
| Título de pantalla | 22–28 px (`--font-size-page-title` fuera del Studio; 20 px en el encabezado compacto del Studio) | 600 | Nombre del servicio |
| Título de sección | 18–20 px (`--font-size-section-title`) | 600 | «Flujo», «Revisión y publicación» |
| Subtítulo | 15–16 px (`--font-size-body-lg`) | 600 | Título de panel o grupo |
| Texto | 13–14 px (`--font-size-body`) | 400 | Contenido, formularios |
| Etiqueta / chip | 12–13 px (`--font-size-caption`, `--font-size-small`) | 500 | Chips, ayudas, estados |

Sin títulos gigantes dentro de las herramientas. Interletrado ligeramente cerrado (-0.01em) en títulos.

## 4. Forma, espacio y elevación

- Radios: controles y tarjetas `--radius-md` (12 px); paneles grandes `--radius-lg` (18 px); chips y botones
  `--radius-pill`. Nada con esquinas rectas.
- Espaciado base 4 px (`--space-*`). Densidad compacta dentro de paneles (8–16 px), aire entre zonas (24 px).
- Sombras: `--shadow-card` (casi nula) en tarjetas; `--shadow-md` en inspector/popover; `--shadow-selected` en la
  tarjeta seleccionada del lienzo. Sin sombras intensas ni glass en tarjetas, tablas o formularios (el glass sigue
  reservado a la navegación global flotante, popovers y modales).

## 5. Componentes

| Componente | Regla |
|---|---|
| Botón primario | Lima con texto carbón, píldora. Uno por zona. |
| Botón secundario | Blanco con borde `--border-strong`. |
| Botón fantasma | Sin fondo; hover `--surface-hover`. |
| Input / selector | Alto 36–40 px, borde `--border-strong`, foco carbón 2 px. |
| Checkbox / radio | Nativos estilizados; los grupos de opciones excluyentes se muestran como **segmentos** (píldoras). |
| Selección múltiple | Componente `data-multiselect`: chips de lo elegido + buscador + lista con casillas; mantiene el `<select multiple>` original sincronizado (sin JS sigue funcionando). |
| Chip / estado | Píldora 12 px; estados con punto de color + texto (Completa, En progreso, Opcional, Requiere atención, Sin configurar). |
| Tarjeta | Superficie + borde + `--shadow-card`, radio 12 px, padding 12–16 px. No todo es una tarjeta: listas compactas y filas para colecciones largas. |
| Panel lateral (inspector) | Derecha, ancho `--studio-inspector-w`, encabezado fijo, contenido con scroll local, acciones al pie. |
| Navegación secundaria | Segmentos horizontales dentro de una sección; solo un grupo visible a la vez. |
| Modal | `<dialog>` con glass; para confirmaciones que cambian el comportamiento (p. ej. mover una actividad de fase). |
| Mensajes de validación | Bajo el campo o en el inspector, con el nombre del campo que falla. |
| Estado vacío | Una frase de qué falta y una acción. |
| Barra de acciones | Pie del Studio: Atrás · estado de guardado + Guardar · Omitir · Continuar. |

## 5.1 Iconografía

- **Familia única:** iconos lineales con la geometría de [Lucide Icons](https://lucide.dev) (licencia ISC) como
  referencia. Los iconos propios de Órbita existentes siguen la misma rejilla y trazo; los nuevos se toman de Lucide
  con **el mismo nombre** (`hand-helping`, `workflow`, `mouse-pointer-click`, `calendar-clock`, …).
- **Integración:** un `<symbol id="i-<nombre>">` por icono en el sprite `templates/layout/icons.html` (una vez por
  página, desde `base.html`) y uso con `{% include "components/icon.html" with name="…" %}`. Se copia solo el icono
  que se usa: sin paquete npm, sin CDN, sin React, sin build. No se mezclan emojis ni otras librerías.
- **Rejilla y trazo:** `viewBox="0 0 24 24"`, sin relleno, `stroke: currentColor`, trazo **1.75** (lo fija `.icon`
  en `components.css`, no cada símbolo), extremos y uniones redondeados.
- **Tamaños:** `sm` 16 px (dentro de botones, chips y listas densas) · normal 20 px (por defecto) · `lg` 24 px
  (tarjetas de opción, encabezados). En una pantalla, un mismo papel usa siempre el mismo tamaño.
- **Color:** heredan el color del texto (`currentColor`): `--text-primary` o `--text-secondary`. Sobre lima
  (`--color-primary`, selección) el icono va en `--color-on-primary` (carbón). Nunca un icono en lima sobre fondo
  claro ni colores sueltos; los estados usan `--status-*` y siempre acompañados de texto.
- **Accesibilidad:** decorativos con `aria-hidden="true"`; un botón solo-icono lleva `aria-label` o tooltip.
- **Excepción de trazo:** la marca de verificación de 14 px usa trazo 2.25 para mantener el peso visual al reducirse.
- **Mosaico de icono** (tarjetas de opción): cuadro `--radius-md` sobre `--surface-sunken`; seleccionado, sobre lima.
- **Vocabulario fijo:** Servicio → `hand-helping` · Proceso → `workflow` · Inicio manual → `mouse-pointer-click` ·
  Inicio programado → `calendar-clock`.

## 5.2 Formularios, contención, bordes y estados

Componentes compartidos en `static/css/components.css` §3.1, **de adopción explícita**: una pantalla los usa poniendo
`ui-form` en su `<form>` (no sustituyen a `.form`, que sigue igual en los módulos existentes). Primera pantalla
adoptante: crear borrador. Al migrar otra pantalla, se cambia `.form` por `ui-form` y se retiran sus estilos locales.

- **Input / Select:** alto `--control-h-form` (44 px), radio `--radius-control` (10 px), borde 1 px
  `--border-control`, fondo `--surface-control`, `appearance: none`; el select lleva su chevron del sistema
  (`--icon-select-chevron`) y recorta con elipsis.
- **Textarea:** alto inicial `--textarea-min-h` (110 px), crece solo en vertical (`resize: vertical`), nunca en ancho.
- **Contención:** todo control es `box-sizing: border-box; width: 100%; max-width: 100%; min-width: 0`; los
  campos (`.field`), filas en rejilla (`minmax(0, 1fr)`) y fieldsets llevan `min-width: 0`. Ningún componente
  fija un ancho en px ni depende de `cols`/`size` nativos.
- **Especificidad:** los controles se declaran con (0,2,1), igual que `.form input[type=…]`; el bloque va después, así
  que dentro de un `ui-form` ganan por orden, también en formularios `.form` anidados o en campos asociados con
  `form=` fuera de su `<form>`. Una pantalla ajusta con una regla propia más específica; nunca `!important`.
- **Rejilla (`.ui-rejilla`):** 1 columna en móvil, 2 desde 640 px, hasta 3 desde 1200 px con `ui-rejilla--tres`.
  `ui-campo--doble` ocupa 2 columnas (fila completa en tablet), `ui-campo--completo` la fila entera y
  `ui-campo--alinear` alinea una casilla con la base de los controles. Agrupar por naturaleza del campo (p. ej.
  Frecuencia | Día | Periodo y debajo Responsable inicial) en vez de apilar.
- **Estado de un apartado** (Básico, `apps/catalogo/estado_basico.py`, única fuente): icono + color en la
  navegación secundaria — verde `check-circle` completo, ámbar `circle`/`alert` en progreso o requiere atención,
  gris `circle` sin configurar, azul grisáceo `circle` opcional — y el nombre del estado solo para lectores de
  pantalla. Bajo el título del apartado, una píldora con el estado y qué hay guardado. Nunca por «visitado».
- **Bordes:** 1 px neutro (`--border-subtle`) en reposo, `--border-strong` al pasar el cursor. Nunca bordes carbón
  de 2–3 px como estado.
- **Foco (teclado):** control → borde `--border-focus` + halo `--ring-focus` (3 px, carbón al 12 %); tarjeta →
  `outline` 2 px `--color-focus-ring` con separación de 2 px. Nunca lima como foco sobre fondo claro.
- **Selección:** borde 1 px `--border-selected` (lima oscuro) + halo `--ring-selected` (lima claro), mosaico del icono
  en lima y **marca de verificación** (`.opcion-tarjeta__marca`): la selección nunca depende solo del color.
- **Error:** borde `--status-danger` + halo `--ring-error`, mensaje debajo en `--status-danger` (lista `errorlist`
  de Django o `aria-invalid="true"`), sin cambiar el tamaño del control.
- **Deshabilitado:** fondo `--surface-sunken`, texto `--text-disabled`, cursor `not-allowed`; un envío en curso
  (`aria-disabled` en el botón) baja la opacidad y bloquea el puntero.
- **Panel (`.ui-panel`):** superficie de contenido sin glass, borde neutro 1 px, `--radius-lg`, `--space-5`.
- **Tarjeta de opción (`.opcion-tarjeta`):** un radio presentado como tarjeta (icono en mosaico, nombre, texto,
  marca). Dos columnas en escritorio, una en móvil.

## 5.3 Navegación global flotante superior

Una sola navegación global (`templates/layout/header.html`, estilos en `layout.css`): un contenedor flotante fijo
arriba, separado del borde (`--nav-offset`), de ancho máximo `--content-portal`, superficie neutra (glass solo con
soporte, si no sólida), borde 1 px `--border-subtle`, radio píldora y sombra ligera.

- **Izquierda:** logo/marca (`layout/marca.html`) + botón de búsqueda (deshabilitado y rotulado «próximamente»
  hasta que exista el backend).
- **Centro:** los destinos de `apps.core.navegacion` (`layout/dock.html`: Inicio · Mis tickets · Trabajo · Más),
  sobre una pista `--surface-sunken`; el activo es una píldora lima con texto carbón (`aria-current="page"`). «Más»
  abre su menú hacia abajo. Los permisos y el cálculo de destinos no cambian.
- **Derecha:** notificaciones + menú de usuario (Mi perfil, Cerrar sesión y lo que ya existía).
- **Reserva de espacio:** el contenido reserva `--header-h` (= `--nav-h` + 2 × `--nav-offset`) arriba
  (`.app-shell--nav`); abajo solo un margen normal (`--dock-clearance`, `--dock-reserve`), sin hueco para un dock.
  Paneles fijos de una pantalla usan `top: calc(var(--header-h) + …)`.
- **Capas:** la barra usa `--z-header`; popovers, toasts y modales van por encima: la navegación nunca tapa menús,
  formularios ni acciones del Studio.
- **Responsive:** escritorio, tres zonas en una fila; tablet (< 1024 px), los destinos muestran solo icono salvo el
  activo y el usuario solo su avatar; móvil (< 640 px), dos filas (identidad y acciones arriba, destinos abajo) y
  `--nav-h` crece para reservar esa altura.
- **Movimiento y accesibilidad:** hover/activo con `--motion-fast` y `--ease-orbita`, sin animaciones de entrada;
  `prefers-reduced-motion` las anula. Enlace «Saltar al contenido», destinos con nombre accesible aunque el texto se
  oculte, foco visible.

## 6. Application Shell del Studio

```
┌──────────────────────────────────────────────────────────┐
│ Navegación global flotante superior de Órbita             │
├──────────────────────────────────────────────────────────┤
│ Encabezado del Studio: nombre · tipo · estado · guardado · Salir │
├──────────┬───────────────────────────────────────────────┤
│ 01 Básico│ Área de trabajo (cambia por sección;           │
│ 02 Form. │ navegación secundaria / constructor / lienzo)  │
│ 03 Salida│                                                │
│ 04 Flujo │                                                │
│ 05 Publ. │                                                │
├──────────┴───────────────────────────────────────────────┤
│ Atrás     ● Sin cambios  [Guardar]        Omitir  Continuar │
└──────────────────────────────────────────────────────────┘
```

- En escritorio (≥1024 px) el Studio ocupa `100vh − --header-h − --dock-reserve`: la página no se desplaza; cada
  zona tiene scroll local. La navegación global está arriba y nunca tapa la barra de acciones.
- En pantallas pequeñas la estructura se apila y la página se desplaza con naturalidad.
- Un único estado por sección en la navegación (punto + etiqueta); el detalle va en el `title`.

## 7. Lienzo de flujos

- Fondo de puntos discretos (`radial-gradient` sobre `--surface-page`), zoom y desplazamiento.
- **Fases como columnas**, de izquierda a derecha; se distinguen por un encabezado y un divisor sutil, no por cajas.
- **Actividades como tarjetas** compactas: icono de intención, nombre, tipo funcional, responsable resumido, número
  de orden de ejecución y estado (punto + texto). Los contenedores muestran sus ramas en miniatura.
- **Conexiones**: curvas discretas con flecha. Secuencia (implícita por el orden) en gris fino; rutas de aprobación
  en verde/ámbar/rojo con etiqueta; condiciones en azul; «en otro caso» discontinua; conexiones de fase
  adicionales que el motor no sigue, en gris punteado y rotuladas como tales.
- **Inspector** a la derecha para fase, actividad o conexión; la selección se conserva tras guardar.
- Movimiento visual (posición) ≠ orden de ejecución: arrastrar dentro de la columna solo cambia la posición;
  cambiar de fase pide confirmación; el orden se cambia con ↑ ↓ explícitos.

## 8. Movimiento — identidad Órbita

Cuatro principios: **fluido** (desacelera al llegar: `--ease-orbita`), **continuo** (lo que aparece nace de lo que se
tocó: entra 8 px desde arriba hacia su sitio), **conectado** (las mismas curvas y duraciones en toda la pantalla) y
**equilibrado** (desplazamientos pequeños, 2–8 px; nada rebota ni gira).

- **Tokens:** `--motion-fast` 150 ms (color, borde, halo) · `--motion-base` 200 ms (elevación, marca) ·
  `--motion-slow` 240 ms (entrada de bloques y cambios de estado de la vista) · `--motion-rise` 8 px ·
  `--motion-lift` −2 px. Los tokens anteriores (`--duration-*`) siguen válidos para lo existente.
- **Qué se anima:** entradas y salidas con `transform` + `opacity` (`.ui-entrar`, keyframes `ui-entrar`); estados
  (hover, foco, selección, error) con color, borde y sombra. Un cambio de tamaño (tarjetas que se compactan) se
  permite solo como transición única de un cambio de estado, nunca en bucle.
- **Microinteracciones:** hover de tarjeta → se eleva 2 px; selección → la marca aparece con escala 0.6→1;
  botón presionado → baja 1 px; formulario que se expande → `ui-entrar`; envío → el botón confirma con «Creando…».
- **Prohibido:** animaciones continuas o decorativas, parallax, retrasos encadenados largos.
- **`prefers-reduced-motion`:** base.css reduce todas las duraciones a ~0 y los componentes anulan además sus
  `transform` y animaciones de entrada: el estado final aparece sin movimiento.

## 9. Referencias y adaptación

- **Geniestudio** — limpieza, superficies luminosas, jerarquía tipográfica, componentes amables. *No* se toma: su
  cielo azul, ilustraciones 3D, titulares de 148 px ni su acento azul.
- **Passionfroot** — densidad, tarjetas compactas de 12 px, chips, bordes finos, formularios ordenados, sombras
  sutiles. *No* se toma: su crema, su violeta, su serif editorial, gradientes de atardecer ni mascota.
- **Órbita conserva** carbón + lima, Inter, iconografía lineal propia (`templates/layout/icons.html`) y una navegación global única.

## 10. Crear borrador (una vista, dos estados)

`templates/catalogo/studio_crear.html` es UNA vista con `data-crear-estado`: **seleccion** («¿Qué quieres crear?» y
dos tarjetas grandes centradas: Servicio «Resolvamos un problema puntual» / Proceso «Elaboremos un plan de trabajo»)
y **configuracion** (las tarjetas se compactan arriba, la elegida resaltada, y aparece el formulario). Sin cambiar
la URL ni recargar; cambiar de tarjeta conserva nombre, categoría y descripción y deshabilita lo que no aplica. En
un Proceso, «¿Cómo inicia este proceso?» con tarjetas Manual / Programado. Una acción principal (Crear borrador) y
Cancelar. Contenedor centrado (máx. 880 px), dos columnas en escritorio y una en móvil, sin espacio artificial al pie.

## 11. Constructor de formularios (Studio › Formulario)

Tres áreas — Estructura · Constructor · Propiedades/Vista previa — repartidas según el ancho REAL del área de
trabajo (consulta de contenedor `trabajo`): ≥ 1040 px, `clamp(240px, 22%, 270px) | 1fr | clamp(300px, 28%, 340px)`;
720–1039 px, Estructura a la izquierda y Constructor sobre Propiedades; < 720 px, una columna. Las tarjetas de la
Estructura van una bajo otra (nunca en dos columnas comprimidas). El estado de versión, la nota de edición y las
acciones (Previsualizar · Editar formulario / Activar versión) están en una barra de herramientas
(`.constructor-barra`); sin campos, un estado vacío con la acción principal.


**Lista de campos y columnas.** Un solo patrón para los campos de los datos de inicio y las columnas de una colección:
lista compacta (`.campo-tabla`) con columnas alineadas — # · Nombre (etiqueta y, debajo, clave y usos en gris) · Tipo
(icono lineal + nombre) · Requisito (Obligatorio / Opcional) · Acciones (subir, bajar, editar). Bordes de 1 px, radio
de control (10 px), fila seleccionada con borde lima, halo y fondo `--color-primary-soft`; transiciones de 150 ms.
En una lista estrecha, Tipo y Requisito pasan bajo el nombre. La información de la fila no se repite en el panel:
vacío, el panel solo invita a seleccionar.

**Edición de un campo o columna.** Las propiedades se agrupan en Identidad (etiqueta, tipo, clave) · Comportamiento
(obligatoriedad, fecha requerida y parámetros SOLO del tipo elegido) · Ayuda y configuración adicional (ayuda, orden)
y, en listas, Valores. «Ampliar» (o el lápiz de la fila) abre una superficie de edición más ancha dentro del mismo
constructor, sin navegar: los grupos van en dos columnas cuando el panel mide ≥ 620 px (contenedor `propiedades`) y
en una en pantallas estrechas; Escape la reduce. Los controles nunca superan el ancho del panel y el textarea solo
crece en vertical; la acción de guardar queda visible al pie.

**Datos de inicio opcionales en un Proceso.** Un Proceso puede usar solo datos de inicio, solo colecciones, ambos o
ninguno. Sin datos de inicio, la pieza se muestra «No los usa (opcional)» y la sección no presenta error; un Servicio
sí necesita su formulario de entrada activo. Los datos de entrada de un Proceso pueden ser el formulario de inicio O una colección: con uno de los dos activo, la sección Formulario está «Completa».

**Flujo.** El lienzo no muestra con quién se comparte la plantilla ni un enlace a ella: ocupan espacio y no se necesitan al configurar.
