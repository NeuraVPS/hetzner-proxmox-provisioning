# Prompt para el agente que actualiza las plantillas de Windows

> ## 🛑 CORRECCIÓN 2026-08-03 — LEE ESTO ANTES QUE NADA
>
> **La Tarea 2 de abajo está MAL en su mitad de v144. NO cablees
> `StrategyQuantX.exe`.** Ese hook **fork-bombea**, y no en teoría:
>
> * En la caja de un cliente (vm 998): **90 `wscript.exe` en 33 segundos y SQX
>   sin arrancar ni una vez.**
> * Reproducido en condiciones controladas (vm 1350, 2026-08-03): el QEMU del
>   invitado a **764 % de CPU** y el agente sin responder, partiendo de reposo.
>
> Se ha retirado de las 122 máquinas de la flota. **En las plantillas solo se
> cablea el hook de v143 (`StrategyQuantX_nocheck.exe`) y los cuatro de
> MetaTrader.** Si `sqx144_hook_launcher.vbs` acaba en una plantilla, se lo
> mandamos a cada cliente nuevo.
>
> La **Tarea 1 (perfil de energía) sigue siendo correcta y es la prioritaria.**
>
> Lo demás de la Tarea 2 se mantiene tal cual: la detección por CONTENIDO (no
> por nombre de carpeta) sigue siendo la buena, sirve para saber qué versión
> lleva la imagen, y el aviso de no hacer `New-Item -Force` sobre una clave
> IFEO existente sigue vigente.


> Copia todo lo que hay debajo de la línea y pásalo como prompt. Es autocontenido.

---

Trabajas en el repositorio `hetzner-proxmox-provisioning` de NeuraVPS. Tu tarea
es actualizar **las plantillas de Windows** (las imágenes que se clonan para
cada VPS nuevo) para que salgan de fábrica con dos cosas que hoy faltan y que
están costando rendimiento y averías a los clientes.

Lee primero, sin excepción:

- `windows_vm/README.md`
- `windows_vm/POWER_PLAN.md`
- `windows_vm/hooks/README.md` — **especialmente los cuatro "hard gates"**
- `windows_vm/prepare.md` (dónde encaja el paso pre-sysprep)

Los dos cambios ya están **documentados y validados en la flota viva**. Tu
trabajo NO es rediseñarlos: es llevarlos a la plantilla para que los clientes
nuevos no nazcan con el defecto.

## Tarea 1 — Perfil de energía «Alto rendimiento»

Windows Server arranca en **Equilibrado**, que aparca núcleos en un invitado
multi-vCPU. En la carga de StrategyQuantX (ráfagas cortas usando todos los
núcleos a la vez) eso es casi el peor caso posible.

Medido en producción: un VPS E de un cliente daba **69.500 / 93.500**
estrategias/hora frente a las 100.000 publicadas, con **18 de sus 20 vCPU
aparcadas**. Tras pasar a Alto rendimiento, la misma máquina dio **102.175 y
102.031**. El cliente había pedido reembolso y cancelación, y los retiró.

No es un caso aislado: un VPS E creado desde la imagen actual el 2026-08-02 se
comprobó recién arrancado y venía en Equilibrado con **16 de 22 núcleos
aparcados**. Afecta a **VPS A–E** (las cajas `mt` de 2 vCPU no aparcan).

**Qué hacer:** aplicar los comandos de `windows_vm/POWER_PLAN.md` dentro de la
plantilla, antes del pre-sysprep. Sysprep conserva el esquema activo, así que
no hace falta tocar el primer arranque.

**Criterio de aceptación** (ejecútalo en una VM clonada de la plantilla nueva,
no en la plantilla): los tres checks de la sección «Verify» de `POWER_PLAN.md`
deben pasar. El que importa es el segundo — **0 núcleos aparcados** leído del
contador vivo, no del GUID del esquema: un esquema puede llamarse «Alto
rendimiento» y seguir aparcando por un valor rancio.

## Tarea 2 — Hooks de lanzamiento en AMBOS ejecutables de SQX

El nombre del ejecutable principal de StrategyQuantX **cambió de versión**:

| versión | ejecutable principal | VBS que le corresponde |
|---|---|---|
| SQX **<= 143** | `StrategyQuantX_nocheck.exe` | `sqx_hook_launcher.vbs` |
| SQX **>= 144** | `StrategyQuantX.exe` | `sqx144_hook_launcher.vbs` |

Hoy la plantilla solo cablea el de v143. **Y de momento así se queda:** el hook
de v144 se retiró el 2026-08-03 por fork-bomb (ver la corrección al principio),
así que los clientes de v144 se quedan sin esa protección a sabiendas — es el
mal menor frente a que la aplicación no abra. **Cablea solo
`StrategyQuantX_nocheck.exe`.**

**Los dos VBS ya existen** en `windows_vm/hooks/`. Son idénticos salvo la
clave IFEO de su guarda anti-recursión. **No los fusiones ni los reescribas**:
esa duplicación es deliberada.

### Tres cosas que te van a morder si no las respetas

1. **No cablees `StrategyQuantX.exe` en absoluto** (ver la corrección del principio). Y si algún día se rehabilita: **nunca lo apuntes a `sqx_hook_launcher.vbs`.** Ese VBS
   lleva la clave `StrategyQuantX_nocheck.exe` codificada como guarda: borraría
   la clave equivocada antes de relanzar, el IFEO se volvería a disparar sobre
   sí mismo y tendrías el **fork-bomb de wscript/reg del 2026-07-16**. Cada
   ejecutable con SU VBS.

2. **Detecta v144+ por CONTENIDO, jamás por el nombre de la carpeta.** El gate
   viejo usaba el regex `SQX.*144|StrategyQuant.*144` sobre el nombre. En un
   barrido de 826 máquinas eso enganchó `StrategyQuantX.exe` en **15 cajas que
   corrían v143** y solo tenían una carpeta *vacía* llamada `StrategyQuantX144`
   de una descarga abandonada → **SQX moría en silencio al doble clic**, que es
   justo lo que el gate existía para evitar. El test correcto es: una carpeta
   que contenga `StrategyQuantX.exe` y **no** contenga
   `StrategyQuantX_nocheck.exe`, buscando en `C:\` **y** en
   `C:\Users\*\Downloads\*` y `C:\Users\*\Desktop\*` (hay instalaciones reales
   ahí). Está resuelto en el snippet del paso 3 de `hooks/README.md`.

   En una plantilla recién hecha solo habrá la versión que instales tú, así que
   el gate es trivial — pero el mismo snippet se reutiliza para remediar cajas
   existentes, así que déjalo correcto.

3. **Nunca `New-Item -Path <clave IFEO> -Force` sobre una clave que ya existe.**
   En PowerShell eso *recrea* la clave y borra sus subclaves, incluida
   `PerfOptions` con `CpuPriorityClass=6` (prioridad alta de SQX). Protege cada
   creación con `if (-not (Test-Path $k))`.

También hay que cablear los **cuatro ejecutables de MetaTrader** con
`mt_hook_launcher.vbs` — con el gate 2 de `hooks/README.md` (solo en cajas cuyos
datos sean portables; en una plantilla nueva siempre lo son).

**Criterio de aceptación:** en una VM clonada de la plantilla nueva,

```powershell
$k='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
Get-ChildItem $k | Where-Object { $_.PSChildName -match 'Strategy|terminal|metaeditor' } |
  ForEach-Object { $_.PSChildName + ' -> ' + (Get-ItemProperty $_.PSPath -Name Debugger -EA SilentlyContinue).Debugger }
```

debe listar **`StrategyQuantX_nocheck.exe` → `sqx_hook_launcher.vbs`** y los
cuatro de MetaTrader a `mt_hook_launcher.vbs`. **`StrategyQuantX.exe` NO debe
aparecer**; si aparece, la imagen lleva el fork-bomb.

Y **la prueba que de verdad vale**: abre SQX en la VM de prueba y comprueba en
su propio log (`<install>\user\log\*.log`) la línea

```
Runtime args: -Djava.awt.headless=true
```

Si esa línea no aparece, el hook no está haciendo nada aunque el registro se vea
bien. Comprueba además que no quedan procesos `wscript`/`reg` acumulándose (eso
sería el fork-bomb) y que la clave `Debugger` sigue puesta después del
lanzamiento (el VBS la quita y la repone; si se quedó quitada, algo falló).

## Reglas de trabajo

- Rama → PR → self-merge. No toques `master` directamente.
- No modifiques los `.vbs` existentes salvo que encuentres un fallo real; si lo
  haces, explica por qué en el PR.
- Si algo de lo documentado no cuadra con lo que ves en la plantilla, **para y
  pregunta** en vez de improvisar: estos dos cambios ya causaron una regresión
  con clientes cada uno.
- Prueba en una VM clonada de la plantilla. No des por bueno el registro sin
  abrir la aplicación.

## Contexto que quizá necesites

- La flota **ya está remediada** para ambos problemas (826 máquinas barridas,
  las 802 accesibles verificadas). Esto es solo para que las **plantillas** no
  vuelvan a producir el defecto en clientes nuevos.
- El fallo del perfil de energía casi cuesta un cliente (pidió reembolso el día
  siguiente de contratar). El del hook dejó a 15 clientes sin poder abrir SQX.
