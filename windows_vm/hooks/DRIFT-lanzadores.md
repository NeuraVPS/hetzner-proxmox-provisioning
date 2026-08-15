# Deriva de versiones de los lanzadores (hooks) — hallazgo y plan

**Estado:** HALLAZGO documentado. **Nada desplegado.** Medido 2026-08-15.
**Origen:** revisión pedida tras encontrar el hook de v144 en la VM 2063 (Claire Sherwin).

## Lo primero: el rollback del 3 de agosto SÍ aguantó

Barrido de las **42 VMs SQX creadas desde el 03-08** (41 respondieron):

- `StrategyQuantX.exe` con IFEO → **1 de 41**, la vm2063, y se le puso **a mano hoy a las 10:18**, no por plantilla.
- `StrategyQuantX_nocheck.exe` con IFEO → **41 de 41**. El hook seguro está bien puesto.
- `wscript` vivos → **0 en todas**. Nada fork-bombeando.

**La plantilla no está re-sembrando el hook retirado.** Esa sospecha era infundada.

## El hallazgo real: los lanzadores desplegados son más viejos que el repo

| Fichero | En la flota | En el repo | Fecha repo |
|---|---|---|---|
| `sqx_hook_launcher.vbs` | **2.394 B** (41/41 + 2063) | **4.215 B** | 29-jul |
| `sqx144_hook_launcher.vbs` | **2.386 B** (41/41 + 2063) | **4.207 B** | 29-jul |
| `mt_hook_launcher.vbs` | **6.847 B** (vm2063, creada 09-08) y **9.121 B** (vm678, vm1018) | **11.223 B** | 09-ago |

Del MT hay **al menos dos versiones distintas conviviendo**, y ninguna es la del repo.

## Y la diferencia NO es cosmética

Traje el `sqx_hook_launcher.vbs` desplegado (2.394 B) de la vm2006 y lo diferencié contra el del repo. Lo que falta en producción es la pareja `CountProcesses` + `WaitForNewProcess`, cuyo propio comentario en el repo dice:

> *"Count first, launch, then wait for the child to actually exist before the caller puts the Debugger value back. `shellObj.Run` is asynchronous, so without this the re-add can land BEFORE the child's CreateProcess reads IFEO and the child gets re-intercepted by our own hook — a launch that spins forever with no window (MetaTrader case 2026-07-22). The cross-process lock does not cover it: the race is inside one invocation."*

Esa carrera es **exactamente el mecanismo del fork-bomb** documentado en [[neuravps-sqx144-hook-forkbomb-withdrawn]]: el `reg add` reponiendo el `Debugger` antes de que el hijo arranque, y el hook disparándose sobre sí mismo.

**Cronología incómoda:** el arreglo está en el repo desde el **29 de julio**. El fork-bomb ocurrió el **2-3 de agosto**, con el lanzador viejo desplegado. **No afirmo que el arreglado lo hubiera evitado** —no está probado— pero describe ese fallo con nombre y apellidos, y nunca salió a producción.

## Por qué NO he desplegado

Aunque el arreglo parezca obviamente bueno, un barrido a ~1.800 VMs aquí tiene mal historial y el riesgo no es simétrico:

1. **Un barrido de hooks es justo lo que causó el incidente del 02-08** (122 cajas, cliente escribiendo dos veces). La lección anotada entonces fue *"una prueba que pasa NO es prueba"* y *"falta lista de exclusiones"*.
2. **Si el fichero nuevo falla, SQX y MetaTrader no abren en toda la flota.** El modo de fallo conocido —lanzador inválido → no arranca nada— ya nos ha mordido ([[neuravps-hook-empty-launcher-silent-nolaunch]]).
3. **La combinación actual funciona.** El hook de v143 con el lanzador viejo lleva en producción desde julio y no está implicado en el fork-bomb. Sustituirlo en masa para cerrar una carrera poco frecuente no es obviamente positivo sin canario.

---

# ACTUALIZACIÓN 2026-08-15 (tarde): canario ejecutado. La causa del fork-bomb NO era la deriva.

Canario en cajas del operador (**vm1096 control / vm1097 tratamiento**, `0000238-AX162-2-LTD`, SQX_143, sin sesiones activas), con autorización expresa para apagar y reiniciar SQX.

## El hallazgo que lo cambia todo: `StrategyQuantX.exe` se relanza a sí mismo

Árbol de procesos medido en vm1097, **sin hook alguno**:

```
StrategyQuantX.exe          pid=5748  padre=powershell.exe
StrategyQuantX.exe          pid=3388  padre=5748  <-- MISMO nombre de imagen
      cmd identico: "C:\SQX_143\StrategyQuantX.exe"

StrategyQuantX_nocheck.exe  pid=6708  padre=powershell.exe
StrategyQuantX_ui.exe       pid=5560  <-- nombre DISTINTO
StrategyQuantX_ui.exe       pid=6512,4696,7044  padre=5560
```

De ahí sale todo:

- **Enganchar `StrategyQuantX_nocheck.exe` (hook v143) es seguro por construcción.** El proceso interceptado engendra hijos con **otro** nombre de imagen (`_ui.exe`), así que el hook **no puede volver a dispararse sobre su propia descendencia**. Se intercepta una vez y se acabó. Por eso lleva bien desde julio.
- **Enganchar `StrategyQuantX.exe` (hook v144) es inseguro por construcción.** El proceso **se re-ejecuta con el mismo nombre**. El lanzador está obligado a reponer el `Debugger` tras arrancar, y el auto-relanzamiento vuelve a chocar con el hook. Según el temporizado sale una cadena infinita (**el fork-bomb**) o un traspaso roto (**SQX muere en silencio**).

## Reproducido: el hook de v144 mata SQX en menos de un segundo

Muestreo cada 400 ms en vm1097 con el IFEO de v144 cableado:

```
t+   0 ms   wscript=1  sqx=0
t+ 400 ms   wscript=0  sqx=1
t+ 800 ms   wscript=1  sqx=2   <-- SEGUNDA wscript: la re-intercepción
después      todo a 0          <-- SQX muerto
```

Sin hook, el mismo binario se queda estable en 2 procesos indefinidamente.

## Y el arreglo del repo NO lo soluciona

| lanzador v144 | resultado |
|---|---|
| desplegado (2.386 B, sin `WaitForNewProcess`) | SQX **no arranca**; `wscript` sale con código 0 |
| **repo (4.207 B, con el arreglo)** | SQX **tampoco arranca**: mismo fallo |

**No es una carrera, es el diseño.** `WaitForNewProcess` cierra la ventana entre *nuestro* re-add y el *primer* hijo. El auto-relanzamiento ocurre **después**, desde dentro de SQX, cuando el `Debugger` ya está legítimamente repuesto. Ninguna versión del lanzador puede cubrir eso.

**Consecuencia: el hook de v144 se retira DEFINITIVAMENTE, no "a la espera de canario".** Corrige el estado que dejó [[neuravps-sqx144-hook-forkbomb-withdrawn]]: no hay canario pendiente, hay un defecto estructural medido.

Descartada de paso la hipótesis del headless: matriz 2×2 (exe × `JAVA_TOOL_OPTIONS`) → `StrategyQuantX.exe` sobrevive con y sin headless (2/2/2/2/2). El flag de Java no es el asesino; el re-enganche sí.

## Explica las dos bombas duales del 02-08

En una caja **dual v143+v144**, el IFEO de v144 está puesto sobre el nombre `StrategyQuantX.exe`, que **existe también dentro de `C:\SQX_143\`** y es el que se auto-relanza. La bomba no necesitaba SQX 144 corriendo: bastaba con que alguien abriera el v143 normal. Encaja con que las dos bombas confirmadas fueran duales y las instalaciones únicas no.

## El lanzador v143 del repo SÍ queda validado

vm1097, hook v143 + lanzador del repo (4.215 B, `sha256 39f5640e…`, hash verificado tras la copia):

```
1) SQX abre .................. SI (5 procesos, estable a 40 s)
2) wscript en un digito ...... SI (pico 0)
3) Debugger repuesto ......... SI
```

Segundo punto de apoyo: la **vm678** (Sergio) lleva ese mismo fichero desde el 14-08 y arrancó SQX a través de él 1 h 37 después, con 21 h de uptime y `wscript`=0.

### Defecto menor detectado de paso: el re-add pierde las comillas

Tras pasar por el lanzador, el valor queda `C:\Windows\System32\wscript.exe C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs` — **sin comillas**. `QuoteArg(debuggerValue)` duplica las comillas internas y `cmd` las come en el `reg add`. Hoy es inocuo porque ninguna ruta tiene espacios, pero **queda a un espacio de romper el hook**. Merece arreglarse en el repo antes de cualquier despliegue masivo.

## Plan revisado

1. **Quitar el IFEO de v144 de la vm2063 (Claire).** Ver abajo.
2. **Arreglar el entrecomillado** del re-add en `sqx_hook_launcher.vbs` y `mt_hook_launcher.vbs`.
3. **Desplegar el lanzador v143 del repo por lotes**, con lista de exclusiones y registrando la versión previa de cada caja (hay al menos tres conviviendo). Criterio de aceptación: los tres puntos conductuales de arriba, no el registro.
4. **Borrar `sqx144_hook_launcher.vbs` de la plantilla y de la flota.** Es un fichero inerte mientras nadie lo cablee, pero su sola presencia invita a recablearlo.

## Decisión sobre la vm2063 (Claire Sherwin) — CORREGIDA

Mi recomendación anterior (*"actualizarle el lanzador"*) **era errónea**: cambiar de lanzador no arregla nada en la ruta de v144, porque el fallo no está en el lanzador.

Su caja es v144 **única** (no dual) y ahora mismo SQX arranca a través del hook, así que no está rota. Pero está a **una instalación de SQX 143 de convertirse en una caja dual**, que es exactamente la configuración que bombeó dos veces.

**Recomendación: quitarle el IFEO de v144** y darle la protección headless por la vía que no tiene re-entrada — `JAVA_TOOL_OPTIONS` como **variable de entorno de máquina**. Consigue lo mismo que ella pidió (SQX arranca headless, no se cae al reconectar por RDP), no depende de interceptar ningún proceso, y no puede fork-bombear. No verificado aún: conviene probarlo en la 1096/1097 antes de tocarle la caja.
