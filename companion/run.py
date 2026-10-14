"""Run AC-001 once against the live game on this PC (Windows, next to FS25).

    python -m companion.run --config farmhand.json here
    python -m companion.run --config farmhand.json marker "Home Yard"
    python -m companion.run --config farmhand.json marker-id 7
    python -m companion.run --config farmhand.json field 12

farmhand.json (trusted local configuration):
    {"tractor_store_categories": ["<categoryName from fhHitchList>", ...],
     "field_entrances": {"<map_id>": {"12": "<AutoDrive marker name>"}}}

The request is a structured AC-001 intent; no language model is involved and no
vehicle is named: the executor selects the exact pair. Exit status 0 only for
COMPLETE.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time
import uuid

from adapters.world import StepUnverified
from companion.bridge import FS25World, start_local_link
from companion.executor import Executor

READY_TIMEOUT_S = 120.0
log = logging.getLogger("farmhand")


def load_config(path: Path) -> tuple[dict[str, str], dict]:
    config = json.loads(path.read_text(encoding="utf-8"))
    names = config.get("tractor_store_categories", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ValueError("tractor_store_categories must be a list of names")
    entrances = {}
    for map_id, fields in (config.get("field_entrances") or {}).items():
        if not isinstance(fields, dict):
            raise ValueError("field_entrances must map map_id -> {field: marker name}")
        entrances[map_id] = {int(field): name for field, name in fields.items()}
    return {name: "tractor" for name in names}, entrances


def target(args) -> object:
    if args.kind == "here":
        return "player_at_acceptance"
    if args.kind == "marker":
        return {"type": "autodrive_marker", "marker_name": args.value}
    if args.kind == "marker-id":
        return {"type": "autodrive_marker", "marker_id": args.value}
    return {"type": "field_entrance", "field": int(args.value)}


def wait_until_linked(world: FS25World) -> str:
    """The farm ID once a snapshot names this companion's session."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            frame = world.link.fresh_snapshot()
        except StepUnverified:
            log.info("waiting for FS25 (load a save with the Farmhand mod enabled)")
            continue
        if frame["session"] == world.link.session:
            log.info("linked: session %s, farm %s", world.link.session, frame["farm_id"])
            return str(frame["farm_id"])
    raise SystemExit("FS25 did not link to this companion session")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", type=Path, help="FarmingSimulator2025 folder (default: Documents)")
    parser.add_argument("kind", choices=("here", "marker", "marker-id", "field"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    if (args.kind == "here") != (args.value is None):
        parser.error("'here' takes no value; marker, marker-id and field need one")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    categories, entrances = load_config(args.config)
    world = FS25World(start_local_link(args.profile), categories)
    try:
        farm_id = wait_until_linked(world)
        intent = {"version": 1, "request_id": "ac001-" + uuid.uuid4().hex[:12],
                  "task": "deliver_tractor_and_forage_wagon", "farm_id": farm_id, "target": target(args)}
        log.info("request %s", json.dumps(intent))
        result = Executor(world, field_entrances=entrances).execute(intent)
    finally:
        world.link.close()
    print(json.dumps({"status": result.status, "reason": result.reason,
                      "equipment": list(result.equipment_ids), "states": list(result.states)}))
    return 0 if result.status == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
