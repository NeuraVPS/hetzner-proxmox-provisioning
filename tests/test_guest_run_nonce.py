"""qemu-ga reutiliza PID: solo vale la salida que trae NUESTRO nonce (07/10/2026).

`exec-status <pid>` puede devolver primero el resultado viejo de OTRO proceso
con el mismo PID (medido en la vm749 el 04/10/2026). Se prueban las cuatro
copias del arreglo de este repo: el bloque `nv_*` de egresscheck, el barrido de
MT y vmtool (identico en los tres), y el bucle bash de
migrate_to_deterministic_ipv6.sh. La orden del nodo y el bucle bash se ejecutan
de verdad, con `qm` / `pvesh` simulados que registran lo que se les pide."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/conversion-vms"))  # vmtool importa convert_mode

FILES = {
    "egresscheck": ROOT / "run_remotes/neuravps-egresscheck.py",
    "sweep": ROOT / "base/mt_portable_optout_sweep.py",
    "vmtool": ROOT / "scripts/conversion-vms/vmtool.py",
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(f"nvnonce_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MODS = {k: _load(k, p) for k, p in FILES.items()}
NONCE = "0123456789abcdef0123456789abcdef"


def _status(out, exitcode=0, exited=1):
    """Lo que imprime `qm guest exec-status` (JSON con sangria, como PVE)."""
    d = {"exited": exited}
    if exited:
        d.update({"exitcode": exitcode, "out-data": out})
    return json.dumps(d, indent=3)


def _ours(body, nonce=NONCE, end=True):
    return f"NV-RUN-{nonce}\r\n{body}" + (f"NV-END-{nonce}\r\n" if end else "")


class BloqueIdentico(unittest.TestCase):
    def test_the_three_copies_are_the_same_text(self):
        bloques = []
        for path in FILES.values():
            s = path.read_text()
            a = s.index("# --- Solo vale la salida que trae NUESTRO nonce")
            b = s.index("NV_AJENO_MSG = ")
            bloques.append(s[a:b])
        self.assertEqual(len(set(bloques)), 1)


class Marca(unittest.TestCase):
    def test_shape_matches_functions_guest_run_nonce(self):
        for m in MODS.values():
            self.assertEqual(
                m.nv_marca("Get-Date # comentario", "n1"),
                "'NV-RUN-n1';Get-Date # comentario\n;$nvok=$?;'NV-END-n1';if(-not $nvok){exit 1}")

    def test_ours_with_and_without_end_marker(self):
        for m in MODS.values():
            self.assertEqual(m.nv_lee(_ours("a\r\nb\r\n"), NONCE), (True, "a\r\nb\r\n"))
            # `exit N` explicito: no hay marca final, la salida sigue siendo nuestra
            self.assertEqual(m.nv_lee(_ours("a\r\n", end=False), NONCE), (True, "a\r\n"))
            # BOM y lineas vacias delante
            self.assertEqual(m.nv_lee("﻿\r\n" + _ours("x"), NONCE)[0], True)
            # la marca final solo se quita si es lo ultimo
            self.assertEqual(m.nv_lee(f"NV-RUN-{NONCE}\nNV-END-{NONCE}\ncola\n", NONCE),
                             (True, f"NV-END-{NONCE}\ncola\n"))

    def test_foreign_results_are_rejected(self):
        for m in MODS.values():
            for ajena in ("Symlink & credential deployment completed successfully.\r\n",
                          _ours("x", nonce="f" * 32), "", None,
                          f"basura NV-RUN-{NONCE}\n"):   # en otra linea no cuenta
                self.assertFalse(m.nv_lee(ajena, NONCE)[0], ajena)


class Resultado(unittest.TestCase):
    def test_foreign_then_ours(self):
        for m in MODS.values():
            out = (_status("stale\r\n") + "\n" + m.NV_SEP + "\n"
                   + _status(_ours("v4g=1 v6g=2\r\n"), exitcode=0) + "\n")
            estado, d = m.nv_resultado(out, NONCE)
            self.assertEqual(estado, "propio")
            self.assertEqual(d["out-data"], "v4g=1 v6g=2\r\n")
            self.assertEqual(d["exitcode"], 0)

    def test_only_foreign(self):
        for m in MODS.values():
            out = "\n@@NV-AJENO@@\n".join(_status("stale\r\n") for _ in range(3))
            self.assertEqual(m.nv_resultado(out, NONCE), ("ajeno", None))

    def test_no_result(self):
        for m in MODS.values():
            for out in ('{\n   "pid" : 1412\n}\n', "QEMU guest agent is not running\n", "", None):
                self.assertEqual(m.nv_resultado(out, NONCE), ("sin_resultado", None))


FAKE_QM = r'''#!/usr/bin/env python3
"""qm simulado: `guest exec --synchronous 0` da un PID; `guest exec-status`
recorre el guion de STATE/script.json (OURS = resultado con el nonce real)."""
import base64, json, os, re, sys
st = os.environ["STATE"]
a = sys.argv[1:]
open(os.path.join(st, "qm.log"), "a").write(" ".join(a[:4]) + "\n")
if a[:2] == ["guest", "exec"]:
    if os.environ.get("NOAGENT"):
        print("QEMU guest agent is not running"); sys.exit(255)
    assert "--synchronous" in a and a[a.index("--synchronous") + 1] == "0", a
    script = base64.b64decode(a[-1]).decode("utf-16-le")
    nonce = re.match(r"'NV-RUN-([0-9a-f]+)'", script).group(1)
    open(os.path.join(st, "nonce"), "w").write(nonce)
    print('{\n   "pid" : 1412\n}'); sys.exit(0)
if a[:2] == ["guest", "exec-status"]:
    assert a[3] == "1412", a
    steps = json.load(open(os.path.join(st, "script.json")))
    i = int(open(os.path.join(st, "i")).read()) if os.path.exists(os.path.join(st, "i")) else 0
    open(os.path.join(st, "i"), "w").write(str(i + 1))
    step = steps[min(i, len(steps) - 1)]
    if step == "GONE":
        print("Agent error: PID lld does not exist"); sys.exit(29)
    if step == "RUNNING":
        print(json.dumps({"exited": 0}, indent=3)); sys.exit(0)
    nonce = open(os.path.join(st, "nonce")).read()
    out = f"NV-RUN-{nonce}\r\nresultado\r\nNV-END-{nonce}\r\n" if step == "OURS" else step
    print(json.dumps({"exitcode": 0, "exited": 1, "out-data": out}, indent=3)); sys.exit(0)
sys.exit(2)
'''


class OrdenNodo(unittest.TestCase):
    """La orden que corre en el nodo, ejecutada con bash y un qm simulado."""

    def _run(self, steps, timeout=20, env_extra=None):
        m = MODS["vmtool"]
        with tempfile.TemporaryDirectory() as st:
            Path(st, "qm").write_text(FAKE_QM)
            os.chmod(Path(st, "qm"), 0o755)
            Path(st, "script.json").write_text(json.dumps(steps))
            # el nonce real lo elige la orden; el falso lo lee del script codificado
            nonce = NONCE
            enc = base64.b64encode(m.nv_marca("'x'", nonce).encode("utf-16-le")).decode()
            orden = m.nv_orden_nodo(749, timeout, nonce,
                                    f"powershell.exe -NoProfile -EncodedCommand {enc}")
            env = dict(os.environ, PATH=f"{st}:{os.environ['PATH']}", STATE=st, **(env_extra or {}))
            r = subprocess.run(["bash", "-c", orden], capture_output=True, text=True,
                               env=env, timeout=60)
            log = Path(st, "qm.log").read_text().splitlines()
        return r.stdout, log, m

    def test_foreign_is_discarded_and_the_same_pid_polled_again(self):
        out, log, m = self._run(["stale\r\n", "RUNNING", "OURS"])
        self.assertEqual(m.nv_resultado(out, NONCE), ("propio", mock.ANY))
        self.assertEqual(m.nv_resultado(out, NONCE)[1]["out-data"], "resultado\r\n")
        self.assertEqual(out.count(m.NV_SEP), 1)
        # una sola ejecucion en el invitado: nunca se relanza el comando
        self.assertEqual(sum(1 for l in log if l.startswith("guest exec ")), 1)
        self.assertEqual(sum(1 for l in log if l.startswith("guest exec-status")), 3)

    def test_gives_up_after_three_foreign_results(self):
        out, log, m = self._run(["a\r\n", "b\r\n", "c\r\n", "OURS"])
        self.assertEqual(m.nv_resultado(out, NONCE), ("ajeno", None))
        self.assertEqual(sum(1 for l in log if l.startswith("guest exec-status")), 3)

    def test_no_agent_passes_qm_message_through(self):
        out, _log, m = self._run(["OURS"], env_extra={"NOAGENT": "1"})
        self.assertEqual(m.nv_resultado(out, NONCE), ("sin_resultado", None))
        self.assertIn("guest agent is not running", out)

    def test_pid_vanishing_stops_the_loop(self):
        out, _log, m = self._run(["GONE"])
        self.assertEqual(m.nv_resultado(out, NONCE), ("sin_resultado", None))
        self.assertIn("does not exist", out)

    def test_timeout_prints_pid_like_synchronous_qm(self):
        out, _log, m = self._run(["RUNNING"], timeout=1)
        self.assertEqual(json.loads(out), {"pid": 1412})
        self.assertEqual(m.nv_resultado(out, NONCE), ("sin_resultado", None))


def _fake_run(stdout, stderr=""):
    return mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, stderr))


class Llamadores(unittest.TestCase):
    def _with_nonce(self, mod):
        return mock.patch.object(mod.secrets, "token_hex", return_value=NONCE)

    def test_egresscheck_and_vmtool(self):
        for name, call in (("egresscheck", lambda m: m.por_agente("n", 749)),
                           ("vmtool", lambda m: m.por_agente("n", 749, "'x'"))):
            m = MODS[name]
            ok_out = _status("stale") + "\n" + m.NV_SEP + "\n" + _status(_ours("medida\r\n"))
            with self._with_nonce(m), mock.patch.object(m.subprocess, "run", _fake_run(ok_out)) as run:
                self.assertEqual(call(m), (True, "medida"))
                cmd = run.call_args[0][0][-1]
                self.assertIn("--synchronous 0", cmd)
                script = base64.b64decode(re.search(r"-EncodedCommand (\S+)", cmd).group(1)).decode("utf-16-le")
                self.assertTrue(script.startswith(f"'NV-RUN-{NONCE}';"))
            ajeno = "\n@@NV-AJENO@@\n".join(_status("stale") for _ in range(3))
            with self._with_nonce(m), mock.patch.object(m.subprocess, "run", _fake_run(ajeno)):
                self.assertEqual(call(m), (False, m.NV_AJENO_MSG))
            # sin agente: el mensaje de siempre
            with self._with_nonce(m), mock.patch.object(m.subprocess, "run",
                                                        _fake_run("", "QEMU guest agent is not running")):
                ok, msg = call(m)
                self.assertFalse(ok)
                self.assertIn("guest agent is not running", msg)

    def test_egresscheck_err_data_fallback_is_kept(self):
        m = MODS["egresscheck"]
        out = json.dumps({"exited": 1, "exitcode": 1, "out-data": _ours(""), "err-data": "boom"})
        with self._with_nonce(m), mock.patch.object(m.subprocess, "run", _fake_run(out)):
            self.assertEqual(m.por_agente("n", 749), (True, "boom"))

    def test_sweep(self):
        m = MODS["sweep"]
        payload = '{"hook":true,"complete":true}'
        with self._with_nonce(m), mock.patch.object(m.subprocess, "run",
                                                    _fake_run(_status(_ours(payload + "\r\n")))):
            self.assertEqual(json.loads(m.guest_exec("n", "749", "x")["out-data"]),
                             {"hook": True, "complete": True})
        ajeno = "\n@@NV-AJENO@@\n".join(_status("stale") for _ in range(3))
        with self._with_nonce(m), mock.patch.object(m.subprocess, "run", _fake_run(ajeno)):
            with self.assertRaises(m.ResultadoAjeno):
                m.guest_exec("n", "749", "x")
            # el unhook no lo da por limpio: main() lo cuenta como "needing a look"
            self.assertTrue(m.unhook("n", "749").startswith("UNREADABLE"))
        # timeout: como antes, `{"pid": N}` se lee y acaba como sonda incompleta
        with self._with_nonce(m), mock.patch.object(m.subprocess, "run",
                                                    _fake_run('{\n   "pid" : 1412\n}\n')):
            self.assertEqual(m.guest_exec("n", "749", "x"), {"pid": 1412})


# --- migrate_to_deterministic_ipv6.sh -----------------------------------------
IPV6_SH = (ROOT / "run_remotes/migrate_to_deterministic_ipv6.sh").read_text()
RUN_PS = re.search(r"(?ms)^  _PS_LAST_ERROR=\"\"\n  _run_ps_via_agent\(\) \{.*?^  \}\n", IPV6_SH).group()

FAKE_PVESH = r'''#!/usr/bin/env python3
import base64, json, os, re, sys
st = os.environ["STATE"]
a = sys.argv[1:]
open(os.path.join(st, "pvesh.log"), "a").write(a[0] + " " + a[1].rsplit("/", 1)[-1] + "\n")
if a[0] == "create":
    script = base64.b64decode(a[-1]).decode("utf-16-le")
    open(os.path.join(st, "script.ps1"), "w").write(script)
    open(os.path.join(st, "nonce"), "w").write(re.match(r"'NV-RUN-([0-9a-f]+)'", script).group(1))
    print(json.dumps({"pid": 77})); sys.exit(0)
steps = json.load(open(os.path.join(st, "steps.json")))
i = int(open(os.path.join(st, "i")).read()) if os.path.exists(os.path.join(st, "i")) else 0
open(os.path.join(st, "i"), "w").write(str(i + 1))
step = steps[min(i, len(steps) - 1)]
nonce = open(os.path.join(st, "nonce")).read()
if step == "BROKEN":
    print('{"exited": 1, "exitcode": 0, "out-data": '); sys.exit(0)
kind, ec = step.split(":")
out = (f"NV-RUN-{nonce}\r\nhola\r\n" if kind == "OURS" else "Symlink & credential deployment completed successfully.\r\n")
print(json.dumps({"exited": 1, "exitcode": int(ec), "out-data": out, "err-data": "e" if ec != "0" else ""}))
'''


class Ipv6Sh(unittest.TestCase):
    def _run(self, steps):
        with tempfile.TemporaryDirectory() as st:
            Path(st, "pvesh").write_text(FAKE_PVESH)
            os.chmod(Path(st, "pvesh"), 0o755)
            Path(st, "steps.json").write_text(json.dumps(steps))
            # igual que en los nodos: la funcion viaja con `declare -f`
            sh = textwrap.dedent(f'''\
                set +e
                NODE_NAME=pve-test
                outer() {{
                {RUN_PS}
                _run_ps_via_agent 749 "Write-Output 'hola'" 5
                echo "rc=$? err=$_PS_LAST_ERROR"
                }}
                eval "$(declare -f outer)"
                outer
                ''')
            env = dict(os.environ, PATH=f"{st}:{os.environ['PATH']}", STATE=st)
            r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env, timeout=60)
            log = Path(st, "pvesh.log").read_text().splitlines()
            script = Path(st, "script.ps1").read_text()
        return r.stdout.strip().splitlines()[-1], log, script

    def test_ours_exit_0_succeeds_after_a_foreign_one(self):
        last, log, script = self._run(["FOREIGN:0", "OURS:0"])
        self.assertEqual(last, "rc=0 err=")
        self.assertEqual(log, ["create exec", "get exec-status", "get exec-status"])
        self.assertRegex(script, r"^'NV-RUN-[0-9a-f]{32}';Write-Output 'hola'\n;\$nvok=\$\?;"
                                 r"'NV-END-[0-9a-f]{32}';if\(-not \$nvok\)\{exit 1\}$")

    def test_foreign_exit_0_never_counts_as_configured(self):
        last, log, _ = self._run(["FOREIGN:0"])
        self.assertTrue(last.startswith("rc=1 err=guest agent kept returning another process"), last)
        self.assertEqual(log.count("get exec-status"), 3)
        self.assertEqual(log.count("create exec"), 1)

    def test_already_configured_exit_10_still_reaches_the_caller(self):
        last, _log, _ = self._run(["OURS:10"])
        self.assertIn("rc=1 err=PS exitcode=10 ", last)
        self.assertIn("stdout='hola'", last)

    def test_unreadable_status_fails_instead_of_passing(self):
        last, _log, _ = self._run(["BROKEN"])
        self.assertTrue(last.startswith("rc=1 err=unreadable exec-status"), last)


if __name__ == "__main__":
    unittest.main()
