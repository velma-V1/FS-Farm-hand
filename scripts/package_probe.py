"""Validate and build the exact development FS25 hitch-probe install folder."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "mod"
RUNTIME_FILES = (
    "modDesc.xml",
    "FarmhandObserve.lua",
    "FarmhandHitch.lua",
    "FarmhandProbe.lua",
)
SOURCE_FILES = RUNTIME_FILES[1:]


def fail(message: str) -> None:
    raise SystemExit(message)


def validate_mod(path: Path, *, exact_files: bool) -> None:
    mod_desc = path / "modDesc.xml"
    if not mod_desc.is_file() or mod_desc.is_symlink():
        fail(f"missing regular modDesc.xml: {mod_desc}")

    try:
        root = ElementTree.parse(mod_desc).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        fail(f"invalid modDesc.xml: {exc}")

    if root.tag != "modDesc":
        fail(f"unexpected XML root: {root.tag!r}")

    try:
        desc_version = int(root.attrib["descVersion"])
    except (KeyError, ValueError):
        fail("modDesc descVersion must be an integer")
    if desc_version <= 0:
        fail("modDesc descVersion must be positive")

    multiplayer = root.find("multiplayer")
    if multiplayer is None or multiplayer.attrib.get("supported") != "false":
        fail("development probe must remain single-player only")

    extra = root.find("extraSourceFiles")
    if extra is None:
        fail("modDesc.xml has no extraSourceFiles")
    listed = tuple(node.attrib.get("filename", "") for node in extra.findall("sourceFile"))
    if listed != SOURCE_FILES:
        fail(f"source load order changed: {listed!r}")

    for name in SOURCE_FILES:
        if Path(name).name != name:
            fail(f"sourceFile must be a root-level filename: {name!r}")
        candidate = path / name
        if not candidate.is_file() or candidate.is_symlink():
            fail(f"missing regular runtime source: {candidate}")

    lua_files = tuple(sorted(p.name for p in path.glob("*.lua")))
    if lua_files != tuple(sorted(SOURCE_FILES)):
        fail(f"unexpected/unreferenced Lua files: {lua_files!r}")

    if exact_files:
        actual = tuple(sorted(p.name for p in path.iterdir() if p.is_file()))
        expected = tuple(sorted(RUNTIME_FILES))
        if actual != expected:
            fail(f"artifact contains unexpected files: {actual!r}")


def build(destination: Path) -> None:
    validate_mod(SOURCE, exact_files=False)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for name in RUNTIME_FILES:
        shutil.copy2(SOURCE / name, destination / name)

    validate_mod(destination, exact_files=True)

    manifest = destination.parent / "SHA256SUMS.txt"
    rows = []
    for name in RUNTIME_FILES:
        data = (destination / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        rows.append(f"{digest}  {destination.name}/{name}")
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"Qualified FS25 probe built at {destination}")
    print(manifest.read_text(encoding="utf-8"), end="")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-source", action="store_true")
    group.add_argument("--build", type=Path)
    args = parser.parse_args()

    if args.check_source:
        validate_mod(SOURCE, exact_files=False)
        print("FS25 probe source layout valid.")
    else:
        build(args.build.resolve())


if __name__ == "__main__":
    main()
