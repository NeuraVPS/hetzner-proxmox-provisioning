"""openssh-fleet-node-task.sh: solo vale la salida que trae NUESTRO nonce (07/10/2026).

Mismo fallo que tests/test_guest_run_nonce.py: qemu-ga puede devolver primero
el resultado de OTRO proceso con el mismo PID. La tarea se ejecuta entera con
bash, un `ssh` simulado que corre la parte remota en local y un `qm` simulado
con un guion por VM."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "run_remotes/openssh-fleet-node-task.sh"

# ssh simulado: el ultimo argumento es la orden remota (`bash -s -- ...`).
FAKE_SSH = '#!/usr/bin/env bash\nexec bash -c "${@: -1}"\n'

FAKE_QM = r'''#!/usr/bin/env python3
"""`qm list` da las VMs de STATE/script.json; `guest exec --synchronous 0` da
PID 1000+vmid; `guest exec-status` recorre el guion de esa VM (OURS = nuestra
salida con el nonce real, OURS_NOEND = sin marca final, como un `exit N`)."""
import base64, json, os, re, sys
st = os.environ["STATE"]
a = sys.argv[1:]
guion = json.load(open(os.path.join(st, "script.json")))
if a[:1] == ["list"]:
    print("      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID")
    for vmid in guion:
        print(f"       {vmid} vm{vmid}             running    4096              60.00 1")
    print("       555 parada               stopped    4096              60.00 0")
    sys.exit(0)
vmid = a[2]
open(os.path.join(st, f"qm-{vmid}.log"), "a").write(" ".join(a[:4]) + "\n")
if a[:2] == ["guest", "exec"]:
    assert a[3:5] == ["--synchronous", "0"], a
    script = base64.b64decode(a[-1]).decode("utf-16-le")
    open(os.path.join(st, f"script-{vmid}.ps1"), "w").write(script)
    nonce = re.match(r"'NV-RUN-([0-9a-f]{32})';", script).group(1)
    open(os.path.join(st, f"nonce-{vmid}"), "w").write(nonce)
    print('{\n   "pid" : %d\n}' % (1000 + int(vmid))); sys.exit(0)
if a[:2] == ["guest", "exec-status"]:
    assert a[3] == str(1000 + int(vmid)), a
    pasos = guion[vmid]
    fi = os.path.join(st, f"i-{vmid}")
    i = int(open(fi).read()) if os.path.exists(fi) else 0
    open(fi, "w").write(str(i + 1))
    paso = pasos[min(i, len(pasos) - 1)]
    if paso == "RUNNING":
        print(json.dumps({"exited": 0}, indent=3)); sys.exit(0)
    if paso == "GONE":
        print("Agent error: PID does not exist"); sys.exit(29)
    nonce = open(os.path.join(st, f"nonce-{vmid}")).read()
    if paso == "OURS_NORESULT":
        out = f"NV-RUN-{nonce}\r\nnada\r\nNV-END-{nonce}\r\n"
    elif paso.startswith("OURS"):
        out = f"NV-RUN-{nonce}\r\nRESULT:OK sshd running\r\n"
        if paso == "OURS":
            out += f"NV-END-{nonce}\r\n"
    else:
        out = paso
    print(json.dumps({"exitcode": 0, "exited": 1, "out-data": out}, indent=3)); sys.exit(0)
sys.exit(2)
'''

PAYLOAD = "# instalador\n$x = 1\n'RESULT:OK sshd running'\n"


class NodeTask(unittest.TestCase):
    def _run(self, guion, gtmo="20"):
        with tempfile.TemporaryDirectory() as st:
            st = Path(st)
            for name, body in (("ssh", FAKE_SSH), ("qm", FAKE_QM)):
                (st / name).write_text(body)
                os.chmod(st / name, 0o755)
            (st / "script.json").write_text(json.dumps(guion))
            work = st / "work"
            work.mkdir()
            (work / "openssh-fleet-node-task.sh").write_text(TASK.read_text())
            (work / "payload.b64").write_text(
                base64.b64encode(PAYLOAD.encode("utf-16-le")).decode())
            env = dict(os.environ, PATH=f"{st}:{os.environ['PATH']}", STATE=str(st),
                       GUEST_TIMEOUT=gtmo, VM_PARALLEL="2")
            r = subprocess.run(["bash", str(work / "openssh-fleet-node-task.sh"), "nodo7", "::1"],
                               capture_output=True, text=True, env=env, timeout=120)
            logs = {p.name: p.read_text().splitlines() for p in st.glob("qm-*.log")}
            scripts = {p.name: p.read_text() for p in st.glob("script-*.ps1")}
        res = {l.split()[2]: " ".join(l.split()[3:]) for l in r.stdout.splitlines()
               if l.startswith("nodo7 VMRES ")}
        return r, res, logs, scripts

    def test_foreign_results_are_discarded_and_never_relaunched(self):
        r, res, logs, scripts = self._run({
            "201": ["RESULT:OK de otro\r\n", "RUNNING", "OURS"],
            "202": ["OURS_NOEND"],
            "203": ["a\r\n", "RESULT:OK ajeno\r\n", "c\r\n", "OURS"],
            "204": ["RESULT:FAIL ajeno\r\n", "OURS"],
        })
        self.assertEqual(res["201"], "OK")
        self.assertEqual(res["202"], "OK")
        self.assertTrue(res["203"].startswith("FAIL foreign-result pid=1203"), res["203"])
        self.assertEqual(res["204"], "OK")
        self.assertIn("nodo7 NODE_DONE 4 vms", r.stdout)
        for vmid, n_status in (("201", 3), ("203", 3), ("204", 2)):
            log = logs[f"qm-{vmid}.log"]
            self.assertEqual(sum(1 for l in log if l.startswith("guest exec ")), 1, log)
            self.assertEqual(sum(1 for l in log if l.startswith("guest exec-status")), n_status, log)
        # el payload va intacto entre la marca de inicio y la cola de siempre
        s = scripts["script-201.ps1"]
        self.assertRegex(s, r"^'NV-RUN-[0-9a-f]{32}';" + PAYLOAD.replace("$", r"\$"))
        self.assertRegex(s, r"\n;\$nvok=\$\?;'NV-END-[0-9a-f]{32}';if\(-not \$nvok\)\{exit 1\}$")

    def test_timeout_and_vanished_pid_fail_explicitly(self):
        _r, res, _logs, _s = self._run({"301": ["RUNNING"], "302": ["GONE"]}, gtmo="1")
        self.assertEqual(res["301"], "FAIL guest-timeout pid=1301")
        self.assertTrue(res["302"].startswith("FAIL exec-status-error pid=1302"), res["302"])

    def test_own_result_without_result_line(self):
        _r, res, _logs, _s = self._run({"401": ["OURS_NORESULT"]})
        self.assertEqual(res["401"], "FAIL no-result exitcode=0")


if __name__ == "__main__":
    unittest.main()
