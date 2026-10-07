# Protección headless para SQX SIN interceptar procesos.
#
# Añade `option -Djava.awt.headless=true` al .config de cada instalación de
# SQX. Ese fichero es donde el lanzador nativo de SQX lee sus argumentos de
# JVM — es el mismo sitio donde ya vive el `-Xmx`.
#
# Sustituye a los hooks IFEO para el crash de AWT al reconectar por RDP
# (modo A). Por qué es mejor:
#   - No intercepta nada => es IMPOSIBLE que fork-bombee. Los dos incidentes
#     de agosto de 2026 fueron el hook disparándose sobre su propia
#     descendencia; aquí no hay descendencia que interceptar.
#   - No hay .vbs que derive de versión, se quede a 0 bytes o pierda las
#     comillas al reponer el `Debugger`.
#   - Vale para v142, v143, v144 y lo que venga: no depende del nombre del exe.
#   - Aplica en el SIGUIENTE ARRANQUE DE SQX. No hace falta reiniciar Windows
#     ni cerrar la sesión del cliente.
#
# ⚠️ POR QUÉ NO SE USA UNA VARIABLE DE ENTORNO DE MÁQUINA. Se probó y funciona
# (`JAVA_TOOL_OPTIONS`, verificado en vm1096: la JVM responde `Picked up
# JAVA_TOOL_OPTIONS`), pero afecta a TODO Java de la caja — y
# **QuantAnalyzer4 es Java con interfaz propia** (no lleva Electron, como sí
# lleva SQX), así que headless global se la puede dejar sin abrir. El .config
# es por aplicación y no tiene ese efecto colateral.
#
# ⚠️ EL SALTO DE LÍNEA IMPORTA. Hay configs en la flota que NO terminan en
# newline. Un `Add-Content` a secas pega la línea al final de la anterior y
# produce `option -Xmx16goption -Djava.awt.headless=true`, que rompe el -Xmx.
# Pasó de verdad en la vm1309 (2026-08-15). Por eso aquí se lee, se filtra y
# se reescribe entero con `Set-Content`, que siempre separa bien.
#
# DÓNDE BUSCA (07/10/2026). Antes solo miraba `C:\SQX_<n>`; hay clientes que
# instalan SQX en `C:\SQX144`, `C:\Apps\SQX`… (VM623: crash de modo A en una
# copia que nadie veía). Ahora usa la misma regla que `SQX_INSTALLS_PS` de
# `functions/sqx_installs.py` (repo NeuraVPS): toda carpeta de primer o segundo
# nivel de C:\ que contenga `StrategyQuantX.exe` o `StrategyQuantX_nocheck.exe`.
# NUNCA atraviesa un reparse point (atributo 1024): `C:\My Servers\VM<n> - …`
# son enlaces SMB al c$ de OTRAS VMs del cliente; seguirlos tocaría la config
# de otra máquina y, si esa VM está parada, cada Test-Path se cuelga ~21 s.
# Tampoco rutas UNC.
#
# QUÉ .config TOCA: los dos que lee el lanzador según versión, si existen —
# `StrategyQuantX.config` (v144) y `StrategyQuantX_nocheck.config` (v142/v143).
# Poner la línea en el que esa versión no lee es inocuo. Si falta el que le
# corresponde (hay `StrategyQuantX_nocheck.exe` ⇒ v142/v143 ⇒ `_nocheck.config`;
# si no, `StrategyQuantX.config`), se informa `=FALTA` y no se crea nada.
#
# IDEMPOTENTE: si ya está `option -Djava.awt.headless=true` no se toca (`=ya`).
# Si hay otra línea headless (`=false`, pegada a otro `option`), tampoco:
# `=DISTINTO`, para revisarla a mano.
#
# SIMULACIÓN: `-DryRun` no escribe nada (ni .bak); informa `=PONDRIA`.
#   Por stdin con QGA: `& ([scriptblock]::Create($s)) -DryRun`.
#
# Salida: una línea `ruta\fichero.config=ESTADO; …` o `SINSQX`.
# ESTADO: ya | PUESTO | REVERTIDO | PONDRIA | FALTA | DISTINTO.
#
# Verificación real: SQX registra sus argumentos en
# `user\log\StrategyQuant\log_<fecha>.log` como
# `SQApp - Runtime args: -Djava.awt.headless=true`.

param([switch]$DryRun)

$ErrorActionPreference = 'SilentlyContinue'
$linea = 'option -Djava.awt.headless=true'
$skip = 'Windows', '$Recycle.Bin', 'System Volume Information', 'Recovery', 'PerfLogs'
$rp = 1024   # [IO.FileAttributes]::ReparsePoint
$res = @()

$top = Get-ChildItem -LiteralPath 'C:\' -Directory -EA 0 |
       Where-Object { ($skip -notcontains $_.Name) -and -not ($_.Attributes -band $rp) }
$cand = foreach ($t in $top) {
    $t.FullName
    Get-ChildItem -LiteralPath $t.FullName -Directory -EA 0 |
        Where-Object { -not ($_.Attributes -band $rp) } | ForEach-Object { $_.FullName }
}
$installs = $cand | Where-Object {
    $_ -and $_ -notmatch '^\\\\' -and
    ((Test-Path -LiteralPath (Join-Path $_ 'StrategyQuantX.exe')) -or
     (Test-Path -LiteralPath (Join-Path $_ 'StrategyQuantX_nocheck.exe')))
} | Sort-Object -Unique

foreach ($dir in $installs) {
    $nocheckExe = Test-Path -LiteralPath (Join-Path $dir 'StrategyQuantX_nocheck.exe')
    $suyo = if ($nocheckExe) { 'StrategyQuantX_nocheck.config' } else { 'StrategyQuantX.config' }
    if (-not (Test-Path -LiteralPath (Join-Path $dir $suyo))) { $res += ((Join-Path $dir $suyo) + '=FALTA') }

    foreach ($n in 'StrategyQuantX.config', 'StrategyQuantX_nocheck.config') {
        $f = Join-Path $dir $n
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { continue }
        $antes = @(Get-Content -LiteralPath $f -EA 0)
        if ($antes | Where-Object { $_ -match '^\s*option\s+-Djava\.awt\.headless=true\s*$' }) {
            $res += ($f + '=ya'); continue
        }
        # Otra línea headless (`=false`, o pegada a otro `option`): no es
        # nuestra, no se toca; se informa para mirarla a mano.
        if ($antes | Where-Object { $_ -match 'java\.awt\.headless' }) {
            $res += ($f + '=DISTINTO'); continue
        }
        if ($DryRun) { $res += ($f + '=PONDRIA'); continue }

        Copy-Item -LiteralPath $f -Destination ($f + '.bak') -Force -EA 0
        $nuevo = @($antes) + $linea
        Set-Content -LiteralPath $f -Value $nuevo -Encoding ascii

        # Aceptación: el fichero releído es exactamente lo que había más la
        # nuestra al final: ningún `option` pegado, el
        # -Xmx intacto y nada convertido a `?` por la codificación. Si no
        # cuadra, se repone el respaldo.
        $fin = @(Get-Content -LiteralPath $f -EA 0)
        if (($fin -join "`n") -cne ($nuevo -join "`n")) {
            Copy-Item -LiteralPath ($f + '.bak') -Destination $f -Force -EA 0
            $res += ($f + '=REVERTIDO')
        } else {
            $res += ($f + '=PUESTO')
        }
    }
}

if ($res.Count -eq 0) { 'SINSQX' } else { $res -join '; ' }
