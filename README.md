Execute this on a new server to prepare it for Proxmox:

```bash
screen -d -m bash -c "curl -fsSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/install.sh | bash -s -- HOSTNAME; exec bash"
screen -r
```

This will automatically generate:

- Hostname: `HOSTNAME`
