#!/usr/bin/env python3
"""Ejecutar PowerShell en un invitado por DOS canales. Se ejecuta EN UNA BASE.

Orden (peticion del operador): 1) qm guest exec desde el nodo. 2) SSH a la IPv6
actual del invitado con las credenciales de Firestore. Si ninguno responde, la
VM se marca MANUAL y NO se toca — nunca se reinicia nada.

Uso:  vmtool.py inspect <vmid>
"""
import base64
import json
import secrets
import subprocess
import sys
import convert_mode

CRED = "/etc/firebase-credentials.json"
_cache = {}


def fs():
    if "db" not in _cache:
        from google.cloud import firestore
        from google.oauth2 import service_account
        c = service_account.Credentials.from_service_account_file(CRED)
        _cache["db"] = firestore.Client(credentials=c, project=c.project_id)
    return _cache["db"]


def doc_de(vmid: int):
    from google.cloud import firestore as _f
    for d in fs().collection("servers").where(
            filter=_f.FieldFilter("proxmoxId", "==", vmid)).limit(2).stream():
        return d.id, (d.to_dict() or {})
    return None, None


def nodo_ip(node_id: str) -> str | None:
    return json.load(open("/var/lib/base-nat/pve_nodes.json")).get(node_id)


def _enc(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


# --- Solo vale la salida que trae NUESTRO nonce --------------------------------
# qemu-ga guarda el resultado de cada proceso terminado hasta que alguien lo pide
# por PID, y Windows reutiliza PID a menudo: `exec-status <pid>` puede devolver
# primero la entrada vieja de OTRO proceso (medido en la vm749 el 04/10/2026: una
# sonda `Write-Error` recibio el texto del script SMB con exit 0). `qm guest
# exec` sincrono se queda con esa primera respuesta. Por eso se lanza con
# `--synchronous 0`, el nodo sondea ESE PID hasta ver `NV-RUN-<nonce>` (una
# entrada ajena se descarta y se vuelve a preguntar, sin relanzar nada; a la
# tercera se deja) y aqui solo se acepta la salida que lo trae. Mismo patron que
# functions/guest_run_nonce.py (PR #517). Este bloque es identico en
# neuravps-egresscheck.py, mt_portable_optout_sweep.py y vmtool.py.
NV_RUN, NV_END = "NV-RUN-", "NV-END-"
NV_MAX_AJENOS = 3
NV_SEP = "@@NV-AJENO@@"


def nv_marca(ps: str, nonce: str) -> str:
    """El script con el nonce como primera linea de stdout y otra al final.
    La cola repite lo que powershell.exe hace con la ultima sentencia, asi que
    el codigo de salida no cambia; un `exit N` explicito se salta la cola."""
    return (f"'{NV_RUN}{nonce}';{ps}\n"
            f";$nvok=$?;'{NV_END}{nonce}';if(-not $nvok){{exit 1}}")


def nv_lee(salida: str, nonce: str):
    """(propia, salida sin marcas). Propia = la primera linea es nuestro nonce."""
    cuerpo = (salida or "").lstrip("\ufeff \t\r\n")
    primera, _, resto = cuerpo.partition("\n")
    if primera.strip() != NV_RUN + nonce:
        return False, salida or ""
    fin = resto.rfind(NV_END + nonce)
    if fin >= 0 and not resto[fin + len(NV_END + nonce):].strip():
        resto = resto[:fin]
    return True, resto


def nv_orden_nodo(vmid, timeout: int, nonce: str, argv: str) -> str:
    """Lo que se ejecuta EN EL NODO: lanza `argv` en el invitado y sondea su PID
    hasta el resultado con nuestro nonce. Imprime cada resultado terminado,
    separados por NV_SEP; si no hay PID, lo que dijo `qm`; si vence el plazo,
    `{"pid": N}` como el `qm guest exec` sincrono."""
    return (
        f"o=$(qm guest exec {vmid} --synchronous 0 -- {argv} 2>&1); "
        "p=$(printf '%s' \"$o\" | tr -d ' \\r\\n' | sed -n 's/.*\"pid\":\\([0-9][0-9]*\\).*/\\1/p'); "
        "if [ -z \"$p\" ]; then printf '%s\\n' \"$o\"; exit 0; fi; "
        f"f=0; e=$(( $(date +%s) + {int(timeout)} )); "
        "while [ \"$(date +%s)\" -lt \"$e\" ]; do "
        f"s=$(qm guest exec-status {vmid} \"$p\" 2>&1) || {{ printf '%s\\n' \"$s\"; exit 0; }}; "
        "case \"$(printf '%s' \"$s\" | tr -d ' \\r\\n')\" in *'\"exited\":1'*) "
        "printf '%s\\n' \"$s\"; "
        f"case \"$s\" in *'{NV_RUN}{nonce}'*) exit 0;; esac; "
        f"f=$((f+1)); [ \"$f\" -ge {NV_MAX_AJENOS} ] && exit 0; "
        f"printf '%s\\n' '{NV_SEP}'; continue;; esac; "
        "sleep 1; done; "
        "printf '{\"pid\":%s}\\n' \"$p\""
    )


def nv_resultado(stdout: str, nonce: str):
    """('propio', estado) con `out-data` ya sin marcas, ('ajeno', None) si solo
    llegaron resultados de otros procesos, o ('sin_resultado', None)."""
    ajenos = 0
    for trozo in (stdout or "").split(NV_SEP):
        trozo = trozo.strip()
        if '"exited"' not in trozo:
            continue
        try:
            d = json.loads(trozo)
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("exited"):
            continue
        propia, texto = nv_lee(d.get("out-data") or "", nonce)
        if propia:
            return "propio", dict(d, **{"out-data": texto})
        ajenos += 1
    return ("ajeno" if ajenos else "sin_resultado"), None


NV_AJENO_MSG = "resultado ajeno: qemu-ga devolvio el de otro proceso con el mismo PID"


def por_agente(node_ip: str, vmid: int, ps: str, t=45):
    """(ok, salida). ok=False si el agente no puede ejecutar, o si solo
    devolvio resultados de otro proceso (no se relanza: `convert` cambia la red)."""
    nonce = secrets.token_hex(16)
    orden = nv_orden_nodo(vmid, t, nonce,
                          f"powershell.exe -NoProfile -EncodedCommand {_enc(nv_marca(ps, nonce))}")
    r = subprocess.run(
        ["ssh", "-n", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
         f"root@{node_ip}", orden],
        capture_output=True, text=True, timeout=t + 30)
    estado, d = nv_resultado(r.stdout, nonce)
    if estado == "propio":
        return True, (d.get("out-data") or d.get("err-data") or "").strip()
    if estado == "ajeno":
        return False, NV_AJENO_MSG
    s = r.stdout.strip()
    return False, " ".join((s + " " + r.stderr).split())[:120]


def por_ssh(ipv6: str, user: str, pwd: str, ps: str, t=45):
    if not (ipv6 and user and pwd):
        return False, "sin credenciales"
    r = subprocess.run(
        ["sshpass", "-p", pwd, "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=12",
         "-o", "PreferredAuthentications=password", f"{user}@{ipv6}", ps],
        capture_output=True, text=True, timeout=t + 20)
    if r.returncode != 0 and not r.stdout.strip():
        return False, " ".join(r.stderr.split())[:120]
    return True, r.stdout.strip()


def ejecutar(vmid: int, ps: str):
    """Devuelve (canal, ok, salida). canal in {agente, ssh, MANUAL}."""
    _id, s = doc_de(vmid)
    if not s:
        return "MANUAL", False, "no esta en Firestore"
    nip = nodo_ip(s.get("nodeId") or "")
    if nip:
        ok, out = por_agente(nip, vmid, ps)
        if ok:
            return "agente", True, out
        motivo_a = out
    else:
        motivo_a = "nodo desconocido"
    ok, out = por_ssh(s.get("ipv6"), s.get("serverUser"), s.get("serverPassword"), ps)
    if ok:
        return "ssh", True, out
    return "MANUAL", False, f"agente[{motivo_a}] ssh[{out}]"


PS_ESTADO = (
    '$i=(Get-NetAdapter -Physical | Where-Object Status -eq "Up" | Select-Object -First 1);'
    '$v6=(Get-NetIPAddress -AddressFamily IPv6 -InterfaceIndex $i.ifIndex | '
    'Where-Object {$_.IPAddress -notlike "fe80*"} | ForEach-Object {$_.IPAddress+"/"+$_.PrefixLength}) -join ",";'
    '$v4=(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $i.ifIndex | '
    'ForEach-Object {$_.IPAddress+"/"+$_.PrefixLength}) -join ",";'
    '$g6=(Get-NetRoute -DestinationPrefix "::/0" -InterfaceIndex $i.ifIndex | ForEach-Object {$_.NextHop}) -join ",";'
    '$g4=(Get-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $i.ifIndex | ForEach-Object {$_.NextHop}) -join ",";'
    '$dns=(Get-DnsClientServerAddress -InterfaceIndex $i.ifIndex | ForEach-Object {$_.ServerAddresses}) -join ",";'
    '"alias="+$i.Name+" v6="+$v6+" v4="+$v4+" gw6="+$g6+" gw4="+$g4+" dns="+$dns'
)

PS_PERSIST = (
    '$i=(Get-NetAdapter -Physical | Where-Object Status -eq "Up" | Select-Object -First 1);'
    '$a6=(Get-NetIPAddress -AddressFamily IPv6 -InterfaceIndex $i.ifIndex | '
    'Where-Object {$_.IPAddress -notlike "fe80*"} | ForEach-Object {$_.IPAddress+"[pre="+$_.PrefixOrigin+",suf="+$_.SuffixOrigin+"]"}) -join ",";'
    '$a4=(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $i.ifIndex | '
    'ForEach-Object {$_.IPAddress+"[pre="+$_.PrefixOrigin+",suf="+$_.SuffixOrigin+"]"}) -join ",";'
    '$dh=(Get-NetIPInterface -InterfaceIndex $i.ifIndex | ForEach-Object {$_.AddressFamily.ToString()+"="+$_.Dhcp.ToString()}) -join ",";'
    '$r=(Get-NetRoute -DestinationPrefix "::/0","0.0.0.0/0" -InterfaceIndex $i.ifIndex | '
    'ForEach-Object {$_.DestinationPrefix+"->"+$_.NextHop+"[store="+$_.Store+"]"}) -join ",";'
    '"v6="+$a6+" v4="+$a4+" dhcp="+$dh+" rutas="+$r'
)


PS_NETSH = (
    'netsh interface ipv6 show address "Ethernet" | Out-String; '
    'netsh interface ipv6 show route store=persistent | Out-String; '
    'netsh interface ipv4 show config name="Ethernet" | Out-String'
)


PS_SALIDA = (
    '$s4=(curl.exe -4 -s -m 15 https://api.ipify.org);'
    '$s6=(curl.exe -6 -s -m 15 https://ifconfig.me);'
    '$d=try{(Resolve-DnsName api.ipify.org -Type A -EA Stop|Select-Object -First 1).IPAddress}catch{"FALLO"};'
    '"salida4=$s4 salida6=$s6 dns=$d"'
)


PS_FW = (
    '$c=(Get-NetConnectionProfile | ForEach-Object {$_.Name+"="+$_.NetworkCategory}) -join ",";'
    '$p=(Get-NetFirewallProfile | ForEach-Object {$_.Name+"="+$_.Enabled}) -join ",";'
    '$r=(Get-NetFirewallRule -DisplayGroup "Escritorio remoto" -EA SilentlyContinue | '
    'ForEach-Object {$_.Name+":"+$_.Enabled+":"+$_.Profile}) -join ",";'
    'if(-not $r){$r=(Get-NetFirewallRule -DisplayGroup "Remote Desktop" -EA SilentlyContinue | '
    'ForEach-Object {$_.Name+":"+$_.Enabled+":"+$_.Profile}) -join ","};'
    '$l=(Get-NetTCPConnection -State Listen -LocalPort 3389 -EA SilentlyContinue | '
    'ForEach-Object {$_.LocalAddress}) -join ",";'
    '"perfil="+$c+" fw="+$p+" escucha3389="+$l+" reglasRDP="+$r'
)


if __name__ == "__main__":
    if sys.argv[1] == "fw":
        v = int(sys.argv[2])
        canal, ok, out = ejecutar(v, PS_FW)
        print(f"  vm {v}: [{canal}] {out}")
    elif sys.argv[1] == "salida":
        v = int(sys.argv[2])
        canal, ok, out = ejecutar(v, PS_SALIDA)
        print(f"  vm {v}: [{canal}] {out}")
    elif sys.argv[1] == "convert":
        v = int(sys.argv[2])
        mostrar = "--show" in sys.argv
        _id, doc = doc_de(v)
        if not doc:
            sys.exit(f"  vm {v}: no esta en Firestore")
        nip = nodo_ip(doc.get("nodeId") or "")
        if not nip:
            sys.exit(f"  vm {v}: nodo {doc.get('nodeId')} desconocido -> MANUAL")
        # Estado actual: hace falta el alias y lo VIEJO que hay que retirar.
        ok, est = por_agente(nip, v, PS_ESTADO)
        if not ok:
            # ⚠️ La conversion NO puede ir por SSH: al cambiar la IP se cortaria
            # la propia sesion a mitad del script y no habria forma de medir.
            print(f"  vm {v}: [MANUAL] el agente no ejecuta ({est})")
            sys.exit(3)
        campos = dict(kv.split("=", 1) for kv in est.split() if "=" in kv)
        alias = campos.get("alias") or "Ethernet"
        viejo6 = (campos.get("v6") or "").split("/")[0].split(",")[0]
        viejo_gw6 = (campos.get("gw6") or "").split(",")[0]
        n6, n4 = convert_mode.objetivos(v)
        if viejo6.lower().startswith("2a01:4f9:c01f:e:"):
            print(f"  vm {v}: YA convertida ({viejo6})"); sys.exit(0)
        ps = convert_mode.script(alias, v, viejo6, viejo_gw6)
        print(f"  vm {v}: {viejo6} -> {n6}   |   v4 -> {n4}   (iface {alias})")
        if mostrar:
            print("  --- PowerShell que va a entrar ---")
            print("\n".join("      " + l for l in ps.strip().splitlines()))
            sys.exit(0)
        ok, out = por_agente(nip, v, ps, t=90)
        print(f"  vm {v}: [{'agente' if ok else 'FALLO'}] {out}")
        sys.exit(0 if ok and "BOUND:" in out else 4)
    elif sys.argv[1] == "netsh":
        v = int(sys.argv[2])
        canal, ok, out = ejecutar(v, PS_NETSH)
        print(f"  vm {v}: [{canal}]\n{out}")
    elif sys.argv[1] == "persist":
        v = int(sys.argv[2])
        canal, ok, out = ejecutar(v, PS_PERSIST)
        print(f"  vm {v}: [{canal}] {out}")
    elif sys.argv[1] == "inspect":
        v = int(sys.argv[2])
        canal, ok, out = ejecutar(v, PS_ESTADO)
        print(f"  vm {v}: [{canal}] {out}")

