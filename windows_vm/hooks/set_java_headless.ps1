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
# Verificación real: SQX registra sus argumentos en
# `user\log\StrategyQuant\log_<fecha>.log` como
# `SQApp - Runtime args: -Djava.awt.headless=true`.

$ErrorActionPreference = 'SilentlyContinue'
$linea = 'option -Djava.awt.headless=true'
$res = @()

foreach ($dir in (Get-ChildItem C:\ -Directory -EA 0 |
                  Where-Object { $_.Name -match '^SQX_\d+$' })) {
    foreach ($cfg in (Get-ChildItem $dir.FullName -Filter 'StrategyQuantX*.config' -EA 0)) {
        if (Select-String -Path $cfg.FullName -Pattern 'java\.awt\.headless' -Quiet) {
            $res += ($dir.Name + '=ya'); continue
        }
        $antes = @(Get-Content $cfg.FullName -EA 0)
        Copy-Item $cfg.FullName ($cfg.FullName + '.bak') -Force -EA 0

        $nuevo = @($antes | Where-Object { $_ -notmatch 'java\.awt\.headless' }) + $linea
        Set-Content -Path $cfg.FullName -Value $nuevo -Encoding ascii

        # Aceptación: una línea más, ningún `option` pegado a otro, y el -Xmx
        # tal cual estaba. Si algo no cuadra, se repone el respaldo.
        $fin = @(Get-Content $cfg.FullName -EA 0)
        $pegadas = $fin | Where-Object { ($_ -split 'option').Count -gt 2 }
        $xmxAntes = ($antes | Where-Object { $_ -match '^option -Xmx' }) -join ','
        $xmxFin = ($fin | Where-Object { $_ -match '^option -Xmx' }) -join ','
        if ($pegadas -or $fin.Count -ne ($antes.Count + 1) -or $xmxAntes -ne $xmxFin) {
            Copy-Item ($cfg.FullName + '.bak') $cfg.FullName -Force -EA 0
            $res += ($dir.Name + '=REVERTIDO')
        } else {
            $res += ($dir.Name + '=PUESTO')
        }
    }
}

if ($res.Count -eq 0) { 'SINSQX' } else { $res -join ' ' }
