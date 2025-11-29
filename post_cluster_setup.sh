#!/usr/bin/env bash

# Always overwrite to keep latest version
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py \
    -o /var/lib/svz/snippets/sync-dnat.py

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-pre-reboot-suspend.sh \
    -o /var/lib/svz/snippets/pve-pre-reboot-suspend.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-post-boot-resume.sh \
    -o /var/lib/svz/snippets/pve-post-boot-resume.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/restore-vm-disk-from-vma.sh \
    -o /var/lib/svz/snippets/restore-vm-disk-from-vma.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/reset-vm-conntrack.py \
    -o /var/lib/svz/snippets/reset-vm-conntrack.py

chmod +x /var/lib/svz/snippets/sync-dnat.py
chmod +x /var/lib/svz/snippets/pve-pre-reboot-suspend.sh
chmod +x /var/lib/svz/snippets/pve-post-boot-resume.sh
chmod +x /var/lib/svz/snippets/restore-vm-disk-from-vma.sh
chmod +x /var/lib/svz/snippets/reset-vm-conntrack.py

sftp -oBatchMode=yes root@[fd00:4000::1] <<EOF
get /etc/firebase-credentials.json /etc/firebase-credentials.json
bye
EOF

sftp -oBatchMode=yes root@[fd00:4000::1] <<EOF
get /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst
bye
EOF