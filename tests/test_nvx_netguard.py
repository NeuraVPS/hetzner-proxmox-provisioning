"""nvx-netguard: fuente unica en install.sh, sintaxis y veredicto del despliegue.

La prueba de comportamiento es real (ifreload en un nodo vacio con VM de
laboratorio): ver base/docs/ifreload-guard.md. Aqui solo lo estatico.
"""
import base64
import importlib.util
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("despliega_netguard", REPO / "scripts" / "despliega-netguard.py")
dn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dn)


def test_heredocs_extraibles_y_con_sintaxis(tmp_path):
    guard = dn.heredoc(dn.GUARD)
    hook = dn.heredoc(dn.HOOK)
    dropin = dn.heredoc(dn.DROPIN)
    (tmp_path / "g.sh").write_text(guard)
    (tmp_path / "h.sh").write_text(hook)
    subprocess.run(["bash", "-n", str(tmp_path / "g.sh")], check=True)
    subprocess.run(["sh", "-n", str(tmp_path / "h.sh")], check=True)
    assert guard.startswith("#!/usr/bin/env bash")
    assert "ExecStartPost=-/usr/local/sbin/nvx-netguard.sh" in dropin
    # La guarda nunca recarga la red ni toca el fichero de interfaces.
    for prohibido in ("ifreload", "ifup ", "ifdown ", "> /etc/network/interfaces", "sed -i"):
        assert prohibido not in "\n".join(l for l in guard.splitlines() if not l.lstrip().startswith("#")), prohibido


def test_adoptar_instala_los_tres_ficheros():
    s = (REPO / "scripts" / "adoptar-nodo-externo.sh").read_text()
    for f in (dn.GUARD, dn.HOOK, dn.DROPIN):
        assert f'put_heredoc "$INSTALL_SH" {f}' in s


def test_hook_no_hace_nada_fuera_de_start(tmp_path):
    hook = tmp_path / "hook"
    hook.write_text(dn.heredoc(dn.HOOK))
    hook.chmod(0o755)
    for mode in ("stop", ""):
        r = subprocess.run([str(hook)], env={**os.environ, "MODE": mode}, capture_output=True)
        assert r.returncode == 0 and not r.stdout


def test_scripts_de_nodo_con_sintaxis(tmp_path):
    for accion in ("instalar", "quitar", "mirar"):
        f = tmp_path / f"{accion}.sh"
        f.write_text(base64.b64decode(dn.node_script(accion)).decode())
        subprocess.run(["bash", "-n", str(f)], check=True)


def _ok():
    d = {k: "si" for k in dn.CHEQUEOS}
    d.update(NVXFIN="1", INSTALADO="si", CT="1", HOOK="si", DROPIN="si")
    return d


def test_veredicto_instalar():
    assert dn.veredicto("instalar", _ok())
    assert dn.veredicto("mirar", _ok())
    for k in dn.CHEQUEOS:
        assert not dn.veredicto("instalar", {**_ok(), k: "no"}), k
    assert not dn.veredicto("instalar", {**_ok(), "CT": "2"})
    assert not dn.veredicto("instalar", {**_ok(), "HOOK": "no"})
    assert not dn.veredicto("instalar", {**_ok(), "PASADA": "fallo"})
    assert not dn.veredicto("instalar", {**_ok(), "SCRIPTS": "desactivados"})
    d = _ok(); del d["NVXFIN"]
    assert not dn.veredicto("instalar", d)


def test_veredicto_quitar():
    assert dn.veredicto("quitar", {"NVXFIN": "1", "INSTALADO": "si", "HOOK": "no", "DROPIN": "no"})
    assert not dn.veredicto("quitar", {"NVXFIN": "1", "HOOK": "si", "DROPIN": "no"})
