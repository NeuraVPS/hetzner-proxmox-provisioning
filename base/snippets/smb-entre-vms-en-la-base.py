#!/usr/bin/env python3
"""Historical helper, withdrawn: its broad source-port rule allowed bypasses.

Use base/docs/egress-pools-base-bootstrap.md (global isolation corrections)
and base/docs/smb-between-accounts-policy.md (account-bound policy). This
stub deliberately performs no network or filesystem mutations.
"""
import sys

if __name__ == "__main__":
    sys.exit("Retired unsafe SMB helper. Follow base/docs/smb-between-accounts-policy.md and the BASE bootstrap runbook.")
