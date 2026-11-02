"""make pull ID=<id or provider/> REASON=... | make unpull ID=.... The plugin picks it up within about 2 s."""
import os
import sys
import time

import yaml

from _common import store


def known_targets():
    """Deployment ids (both copies) and `<account>/` for every account in config.yaml."""
    cfg = yaml.safe_load(open(os.environ.get("CHARMER_CONFIG", "config.yaml"))) or {}
    ids, accounts = set(), set()
    for d in cfg.get("model_list") or []:
        mid = d["model_info"]["id"]
        ids |= {mid, mid[5:] if mid.startswith("pair-") else f"pair-{mid}"}
        accounts.add(d["model_info"].get("account", "") + "/")
    return ids | accounts


def main(argv):
    if len(argv) < 2 or argv[0] not in ("pull", "unpull"):
        print("usage: pulls.py pull|unpull <id or provider/> [reason]")
        return 2
    if argv[1] not in known_targets():
        print(f"{argv[1]}: not a deployment id or <account>/ in config.yaml; nothing changed")
        return 2
    s = store()
    if argv[0] == "pull":
        s.add_pull(argv[1], argv[2] if len(argv) > 2 and argv[2] else "pulled by the owner", "owner")
    else:
        s.add("unpull", argv[1], time.time())
    s.flush()
    print(f"{argv[0]} {argv[1]}: done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
