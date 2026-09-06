"""Run the actual shell helpers against fake routes/probes, without network changes."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent


def prepare(tmp_path, name, home='hel'):
    config = tmp_path / 'config'
    config.write_text(f'NODE_ID=240\nHOME_REGION={home}\nDEFAULT_V4_VIA_TUNNEL=0\n')
    script = (ROOT / name).read_text().replace('/etc/default/neuravps-tunnels', str(config))
    script = script.replace('/run/neuravps-tunnel-probe.state', str(tmp_path / 'state'))
    script = script.replace('/usr/local/sbin/neuravps-tunnel-select.sh', str(tmp_path / 'select'))
    target = tmp_path / name
    target.write_text(script)
    scripts = {
        'ip': '''#!/bin/bash
case "$*" in
 *"route show default table 100"*) echo "default dev ${CURRENT6:-tun-hel}";;
 *"route show default table 101"*) echo "default dev ${CURRENT4:-tun-hel}";;
 *"route show default table 102"*) echo "default dev ${CURRENTHOST:-tun-hel}";;
 *"route replace"*) echo "$*" >> "$LOG";;
esac
''',
        'timeout': '''#!/bin/bash
case "$*" in
 *"::0/443"*) sleep 0.03; exit "${FSN_FAILURE:-0}";;
 *"::2/443"*) exit "${HEL_FAILURE:-0}";;
esac
exit 1
''',
        'select': '#!/bin/bash\necho "select $*" >> "$LOG"\n',
        'logger': '#!/bin/bash\nexit 0\n',
    }
    for filename, content in scripts.items():
        path = tmp_path / filename
        path.write_text(content)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'], 'LOG': str(tmp_path / 'calls')}
    return target, config, env


def run(script, env, *args):
    return subprocess.run(['bash', str(script), *args], env=env, capture_output=True, text=True, check=True)


def test_selector_is_noop_when_policy_routes_match_and_main_has_no_ipv4_default(tmp_path):
    script, _, env = prepare(tmp_path, 'neuravps-tunnel-select.sh')
    run(script, env, 'hel')
    assert not (tmp_path / 'calls').exists()


def test_selector_repairs_inconsistent_policy_route(tmp_path):
    script, _, env = prepare(tmp_path, 'neuravps-tunnel-select.sh')
    run(script, {**env, 'CURRENT4': 'tun-fsn'}, 'hel')
    assert len((tmp_path / 'calls').read_text().splitlines()) == 3


def test_probe_switches_using_ipv6_policy_route_without_main_ipv4_default(tmp_path):
    script, _, env = prepare(tmp_path, 'neuravps-tunnel-probe.sh', home='fsn')
    (tmp_path / 'state').write_text('fsn 1\n')
    run(script, {**env, 'CURRENT6': 'tun-fsn', 'FSN_FAILURE': '1'})
    assert (tmp_path / 'calls').read_text().strip() == 'select hel'


def test_failed_auto_detection_remains_pending_then_returns_to_measured_home(tmp_path):
    script, config, env = prepare(tmp_path, 'neuravps-tunnel-probe.sh', home='auto')
    env = {**env, 'CURRENT6': 'tun-fsn'}
    run(script, {**env, 'FSN_FAILURE': '1', 'HEL_FAILURE': '1'})
    assert 'HOME_REGION=auto' in config.read_text()
    assert not (tmp_path / 'calls').exists()
    for _ in range(4):
        run(script, env)
    assert 'HOME_REGION=hel' in config.read_text()
    assert (tmp_path / 'calls').read_text().strip() == 'select hel'


def test_installer_embeds_current_helpers():
    installer = (ROOT.parents[1] / 'install.sh').read_text()
    for name in ['neuravps-tunnels.sh', 'neuravps-tunnel-select.sh', 'neuravps-tunnel-probe.sh']:
        embedded = installer.split(f"cat > /mnt/usr/local/sbin/{name} <<'NVXEOF'\n", 1)[1].split('\nNVXEOF', 1)[0]
        assert embedded == (ROOT / name).read_text().rstrip()
