# Stable gateway MAC on isolated VM bridges

Without an explicit MAC, Linux selects a bridge address from its ports. A VM
starting or leaving can therefore change the gateway MAC for other guests.
Their neighbor caches then point to the old address until they recover.

New installations set a stable, locally administered MAC in the generated
`vmbr0` configuration. Existing nodes should keep the address already in use:

```sh
python3 neuravps-pin-bridge-mac.py          # inspect only
python3 neuravps-pin-bridge-mac.py --apply  # pin the same address and persist it
```

The helper validates the effective ifupdown configuration, rejects conflicting
MAC settings, verifies that addresses and interface flags stay unchanged, and
backs up the original file as `/etc/network/interfaces.before-neuravps-bridge-mac`.
It does not reload networking, restart a service, or operate on a guest.

This fixes collateral neighbor-cache disruption on the host. It does not make
the final VM migration pause disappear or guarantee identical gateway MACs
between different destination hosts.
