"""Генерация Ansible inventory из terraform output -json.

Запуск: terraform -chdir=terraform/yandex output -json | python scripts/gen_inventory.py \\
            --out ansible/inventory/hosts.ini
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEMPLATE = """\
[gpu]
{gpu_ip} ansible_user={user}

[loadgen]
{loadgen_ip} ansible_user={user}

[monitoring]
{monitoring_ip} ansible_user={user}

[all:vars]
ansible_ssh_common_args=-o StrictHostKeyChecking=accept-new
gpu_internal_ip={gpu_internal_ip}
loadgen_internal_ip={loadgen_internal_ip}
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="ansible/inventory/hosts.ini")
    p.add_argument("--user", default="ubuntu")
    args = p.parse_args()
    tf = json.load(sys.stdin)

    def val(name: str) -> str:
        if name not in tf:
            raise SystemExit(f"в terraform output нет {name!r}")
        return tf[name]["value"]

    inventory = TEMPLATE.format(
        gpu_ip=val("gpu_public_ip"),
        loadgen_ip=val("loadgen_public_ip"),
        # мониторинг совмещён с loadgen-ВМ (см. terraform/yandex/main.tf)
        monitoring_ip=val("loadgen_public_ip"),
        gpu_internal_ip=val("gpu_internal_ip"),
        loadgen_internal_ip=val("loadgen_internal_ip"),
        user=args.user,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inventory, encoding="utf-8")
    print(f"inventory: {out}")


if __name__ == "__main__":
    main()
