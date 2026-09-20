# Prompt operativo: actualización de plantillas Windows Server 2025

Trabaja únicamente sobre `windows-es` y `windows-en` en el repositorio
`hetzner-proxmox-provisioning`. Lee `README.md`, `POWER_PLAN.md`,
`hooks/README.md` y `prepare.md` antes de actuar. No ejecutes cambios en VMs de
clientes ni publiques streams sin canarios y revisión.

## Preparación invitada

1. Instala las actualizaciones aprobadas y .NET Framework 3.5. Conserva
   Feedback Hub y el stub OS-serviced de DesktopAppInstaller. Si Sysprep señala
   un paquete winget/source instalado para el usuario, retira ese paquete por
   la vía soportada de ese usuario. No fuerces la eliminación de AppX no removibles.
   Contrasta también los parches fuera de banda pertinentes en la documentación
   de Microsoft: pueden no ofrecerse por Windows Update. Sigue sus requisitos,
   verifica los hashes oficiales y mide KB/build después del reinicio; ver README.
2. Configura OpenSSH, NTP, Samba/firewall, política de contraseñas, UI, perfil,
   `C:\NeuraData` y `C:\My Servers` según `README.md`.
3. Aplica High performance y verifica cero núcleos aparcados con
   `POWER_PLAN.md`.
4. No dejes credenciales de autologon en la plantilla; provisioning las escribe
   por VM.

## Instaladores de aplicaciones

Usa solo `windows_vm/installers/install_sqx_from_storagebox.ps1` y
`install_mt_from_storagebox.ps1`. Descargan los VBS únicamente desde el cache
verificado `/pkg/hooks/<revision>/` de `files-hel` o `files-fsn` y comprueban
SHA-256.

- El instalador SQX configura headless en el `.config` de la aplicación
  usando la misma regla que `set_java_headless.ps1`. No uses una variable Java global.
- El instalador puede aplicar IFEO `CpuPriorityClass=6` (AboveNormal) al
  ejecutable previsto. Nunca añadas IFEO `Debugger` a `StrategyQuantX.exe`; el launcher v144 fue
  retirado por el riesgo de fork-bomb en instalaciones duales. No restaures
  `sqx144_hook_launcher.vbs`.
- MetaTrader recibe `/portable` solo después de su gate de datos portables.
  No cambies el modo de datos de una instalación no portable.
- No edites los VBS existentes sin una reproducción comportamental del defecto.

## Cleanup y Sysprep

Ejecuta `presysprep_cleanup.ps1` elevado con los valores seguros por defecto.
No repitas DISM `/ResetBase`, no resetees `DataStore`, `catroot2` o BITS, no
hagas defrag por costumbre, no borres Event Logs sin copiar evidencia y no uses
SDelete salvo medición explícita que justifique `-RunSDelete`. El script
preserva Panther, AppX y Feedback Hub, elimina identidades SSH de plantilla,
prioriza TRIM, y no modifica `unattend.xml` ni ejecuta Sysprep.

Sysprep se inicia en la sesión interactiva elevada de Administrator/Administrador,
directamente o con una tarea `Interactive` / `Highest` que se borre antes de
lanzarlo. QGA puede registrar esa tarea; Sysprep nunca se ejecuta como SYSTEM:

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

No modifiques el XML existente, no añadas `CopyProfile` ni uses un answer file
alternativo. Tras clonar, verifica RDP, Explorer/UI, red, SSH con fingerprint
nueva, SID Administrator `-500`, AppX CBS/Core/XAML, Feedback Hub y el plan de
energía.

## Proxmox y exportación

- La plantilla usa `cpu: x86-64-v4`, nunca `host`. En un reinstall, el código
  conserva RAM, balloon, cores, sockets, NUMA y todos los discos del cliente;
  solo adopta la política virtual de la plantilla y baja a v3 si el nodo no
  soporta AVX-512.
- Lee `qm config --current` y exporta solo VMs positivamente apagadas. El
  exportador escribe una clave staging fechada e inmutable; no uses el flujo
  directo destructivo salvo autorización explícita.
- El config canónico debe contener exactamente un marker:
  `# neuravps-stream-template-key: windows-es-YYYYMMDD` o su equivalente
  `windows-en`. Marker ausente, duplicado, malformado o mutable es fallo de
  publicación. Restore debe usar ese marker para todos los discos y firewall.
- Primero valida streams e instaladores con canarios. Despliega todos los
  consumidores Google de create/reset/colas y el refresher de caché en ambas
  BASE; verifica hooks e instaladores publicados. Solo entonces sustituye
  atómicamente cada `config.conf` canónico por el config staging completo con
  su marker. Guarda el config anterior para rollback y conserva los streams
  canónicos antiguos e inmutables para trabajos ya en marcha.

## Historial retirado

Quedan retiradas la eliminación forzada de AppX de sistema/Feedback Hub y la
ejecución de Sysprep como SYSTEM. También queda retirada la afirmación general
de que winget nunca bloquea Sysprep: depende del paquete instalado al usuario. Si una observación nueva contradice estas reglas, detente y documenta
la evidencia antes de cambiar el procedimiento.
