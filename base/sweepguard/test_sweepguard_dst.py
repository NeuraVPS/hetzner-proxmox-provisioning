"""Tests del filtro por destino de sweepguard (2026-09-18).

    python3 -m pytest base/sweepguard/test_sweepguard_dst.py -q
    python3 base/sweepguard/test_sweepguard_dst.py      # sin pytest

Reproduce los dos falsos positivos medidos en produccion y comprueba que un
barrido real hacia nuestras direcciones se sigue viendo.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sweepguard", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

CFG = dict(sg.DEFAULTS)

# lo que devuelve `nft -j list set ip rdpguard nuestras4` en una base
NUESTRAS4 = [{"set": {"family": "ip", "name": "nuestras4", "table": "rdpguard",
                      "type": "ipv4_addr", "flags": ["interval"],
                      "elem": ["77.42.49.79", "94.130.3.118", "95.216.102.179",
                               "116.202.118.221",
                               {"prefix": {"addr": "10.0.0.0", "len": 8}}]}}]
NUESTRAS6 = [{"set": {"family": "ip6", "name": "nuestras6", "table": "rdpguard",
                      "type": "ipv6_addr", "flags": ["interval"],
                      "elem": [{"prefix": {"addr": "2a01:4f9:fff1:5f::", "len": 64}},
                               {"prefix": {"addr": "2a01:4f9:c01f:e::", "len": 64}}]}}]


def ct_line(src, dst, dport, fam="ipv4"):
    return (f"{fam}     2 tcp      6 431999 ESTABLISHED src={src} dst={dst} sport=50000 "
            f"dport={dport} src={dst} dst={src} sport={dport} dport=50000 [ASSURED] mark=0 use=1\n")


def with_conntrack(lines, fn):
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.writelines(lines)
        path = fh.name
    old = sg.CONNTRACK
    sg.CONNTRACK = path
    try:
        return fn()
    finally:
        sg.CONNTRACK = old
        os.unlink(path)


def fake_nft(sets):
    def _nft(args):
        # args = ["list", "set", family, "rdpguard", name]
        return sets.get(args[-1], [])
    return _nft


def test_guarded_destinations_parses_plain_and_prefix():
    sg.nft_json = fake_nft({"nuestras4": NUESTRAS4})
    ours = sg.guarded_destinations("ip")
    assert ours("77.42.49.79") and ours("10.64.2.69") and ours("116.202.118.221")
    assert not ours("143.198.153.78") and not ours("35.71.139.234")


def test_missing_set_means_no_filter():
    sg.nft_json = fake_nft({})
    assert sg.guarded_destinations("ip") is None


def test_empty_set_means_no_filter():
    sg.nft_json = fake_nft({"nuestras4": [{"set": {"name": "nuestras4", "elem": []}}]})
    assert sg.guarded_destinations("ip") is None


def test_hotport_ignores_guest_egress_to_internet():
    # 16/09-17/09: 10.64.4.113 y 10.64.4.36 bloqueados por "14-16 conexiones al
    # puerto 25060" — era un servicio de DigitalOcean, 143.198.153.78.
    lines = [ct_line("10.64.4.113", "143.198.153.78", 25060)] * 16
    lines += [ct_line(f"10.64.3.{i}", "143.198.153.78", 25060) for i in range(10)]
    sg.nft_json = fake_nft({"nuestras4": NUESTRAS4})
    ours = sg.guarded_destinations("ip")
    assert with_conntrack(lines, lambda: sg.hot_port_abusers("ip", CFG, ours)) == {}
    # sin filtro (comportamiento viejo) SI saltaba: la prueba reproduce el fallo
    old = with_conntrack(lines, lambda: sg.hot_port_abusers("ip", CFG, None))
    assert "10.64.4.113" in old


def test_hotport_still_catches_attack_on_our_vip():
    lines = [ct_line("203.0.113.9", "77.42.49.79", 21845)] * 8
    for i in range(5):
        lines += [ct_line(f"198.51.100.{i}", "77.42.49.79", 21845)] * 3
    sg.nft_json = fake_nft({"nuestras4": NUESTRAS4})
    ours = sg.guarded_destinations("ip")
    got = with_conntrack(lines, lambda: sg.hot_port_abusers("ip", CFG, ours))
    assert got["203.0.113.9"] == (8, 21845)


def test_flooder_guest_to_internet_vs_to_us():
    ext = [ct_line("10.64.2.58", "15.197.250.88", 21402)] * 150
    ours_lines = [ct_line("10.64.2.69", "94.130.3.118", 20202)] * 150
    sg.nft_json = fake_nft({"nuestras4": NUESTRAS4})
    ours = sg.guarded_destinations("ip")
    got = with_conntrack(ext + ours_lines, lambda: sg.flooders("ip", CFG, ours))
    assert got == {"10.64.2.69": 150}


def test_ip6_full_form_conntrack_addresses():
    # /proc/net/nf_conntrack escribe la v6 SIN comprimir
    guest = "2a01:04f9:c01f:000e:0000:0000:0000:059f"
    ext = "2606:4700:0000:0000:0000:0000:6810:0001"
    vip = "2a01:04f9:fff1:005f:0000:0000:0000:0002"
    lines = [ct_line(guest, ext, 22000, "ipv6")] * 150 + [ct_line(guest, vip, 20863, "ipv6")] * 120
    sg.nft_json = fake_nft({"nuestras6": NUESTRAS6})
    ours = sg.guarded_destinations("ip6")
    got = with_conntrack(lines, lambda: sg.flooders("ip6", CFG, ours))
    assert got == {"2a01:4f9:c01f:e::59f": 120}   # canonica, como la guarda nft


def test_ip6_already_blocked_is_not_reblocked():
    # conntrack da la v6 sin comprimir; bf_auto (nft -j) la da comprimida
    full = "2600:1900:4020:049c:0000:031a:0000:0000"
    lines = [ct_line(full, "2a01:04f9:002a:2d56:0000:0000:0000:03a0", 31998, "ipv6")] * 150
    sets = {"nuestras6": [{"set": {"name": "nuestras6", "elem": [
                {"prefix": {"addr": "2a01:4f9:2a:2d56::", "len": 64}}]}}],
            "bf_auto": [{"set": {"name": "bf_auto", "elem": [
                {"elem": {"val": "2600:1900:4020:49c:0:31a::", "timeout": 86400}}]}}]}
    sg.nft_json = fake_nft(sets)
    calls = []
    real_run = sg.subprocess.run
    sg.subprocess.run = lambda *a, **k: calls.append(a) or real_run(["true"])
    try:
        n = with_conntrack(lines, lambda: sg.run_family("ip6", CFG))
    finally:
        sg.subprocess.run = real_run
    assert n == 0 and calls == [], (n, calls)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:          # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if fails else 0)
