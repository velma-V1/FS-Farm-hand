"""Validate and build the exact install folders for the user's Windows PC.

    --check-source              validate mod/ as CI does
    --build dist/FS25_Farmhand  the FS25 mod folder (copied into the game's mods/)
    --build-companion dist/FarmhandCompanion
                                the companion: runtime sources, pinned requirements
                                and the one-time installer (scripts/install_windows.cmd)
"""

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
    "FarmhandBridge.lua",
    "FarmhandProbe.lua",
    "translations/translation_en.xml",
)
SOURCE_FILES = RUNTIME_FILES[1:5]
# The on-foot keys (mod/FarmhandBridge.lua Bridge.KEYS).
REQUEST_ACTIONS = ["FARMHAND_FETCH_HERE", "FARMHAND_TALK", "FARMHAND_STOP", "FARMHAND_PUSH_TO_TALK",
                   "FARMHAND_SETTINGS"]


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
        fail("the AC-001 mod must remain single-player only")

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

    lua_files = tuple(sorted(p.relative_to(path).as_posix() for p in path.rglob("*.lua")))
    if lua_files != tuple(sorted(SOURCE_FILES)):
        fail(f"unexpected/unreferenced Lua files: {lua_files!r}")

    # The in-game keys: each declared, bound and named exactly once (AutoDrive's layout).
    actions = [a.attrib.get("name") for a in root.findall("actions/action")]
    bindings = [b.attrib.get("action") for b in root.findall("inputBinding/actionBinding")]
    l10n = root.find("l10n")
    if actions != REQUEST_ACTIONS or bindings != REQUEST_ACTIONS:
        fail(f"request actions not declared and bound exactly once: {actions!r} {bindings!r}")
    if l10n is None or l10n.attrib.get("filenamePrefix") != "translations/translation":
        fail("modDesc l10n must point at translations/translation")
    try:
        texts = ElementTree.parse(path / "translations" / "translation_en.xml").getroot()
    except (ElementTree.ParseError, OSError) as exc:
        fail(f"invalid translation_en.xml: {exc}")
    named = [t.attrib.get("name") for t in texts.findall("texts/text")]
    if any(f"input_{action}" not in named for action in REQUEST_ACTIONS):
        fail("translation_en.xml does not name every request action")

    if exact_files:
        actual = tuple(sorted(p.relative_to(path).as_posix() for p in path.rglob("*")
                              if p.is_file() or p.is_symlink()))
        expected = tuple(sorted(RUNTIME_FILES))
        if actual != expected:
            fail(f"artifact contains unexpected files: {actual!r}")


def build(destination: Path) -> None:
    validate_mod(SOURCE, exact_files=False)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for name in RUNTIME_FILES:
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, destination / name)

    validate_mod(destination, exact_files=True)

    manifest = destination.parent / "SHA256SUMS.txt"
    rows = []
    for name in RUNTIME_FILES:
        data = (destination / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        rows.append(f"{digest}  {destination.name}/{name}")
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"Qualified FS25 mod built at {destination}")
    print(manifest.read_text(encoding="utf-8"), end="")


# The companion's runtime: never tests, the simulator or development tooling.
COMPANION_FILES = (
    "README.md",
    "requirements.txt",
    "farmhand.example.json",
    "scripts/install_windows.cmd",
    "adapters/fs25.py",
    "adapters/world.py",
    "companion/ai.py",
    "companion/bridge.py",
    "companion/coordinator.py",
    "companion/executor.py",
    "companion/hitch.py",
    "companion/language.py",
    "companion/model.py",
    "companion/voice.py",
    "requirements-voice.txt",
    "companion/run.py",
    "companion/service.py",
    "protocol/command.schema.json",
    "protocol/frame.schema.json",
    "protocol/intent.schema.json",
    "protocol/ipc.py",
    "protocol/model.py",
)
# The live validation steps travel with the companion.
COMPANION_DOCS = {"mod/README.md": "LIVE_VALIDATION.md"}


def build_companion(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    rows = []
    for source, target in [(name, name) for name in COMPANION_FILES] + list(COMPANION_DOCS.items()):
        path = ROOT / source
        if not path.is_file() or path.is_symlink():
            fail(f"missing regular companion file: {source}")
        out = destination / target
        out.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if source.endswith(".cmd"):          # cmd.exe needs CRLF whatever the checkout did
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        out.write_bytes(data)
        rows.append(f"{hashlib.sha256(data).hexdigest()}  {destination.name}/{target}")
    if any("simulator" in p.parts or "tests" in p.parts for p in destination.rglob("*")):
        fail("the companion bundle must not contain tests or the simulator")
    manifest = destination.parent / "COMPANION_SHA256SUMS.txt"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"Companion bundle built at {destination}")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-source", action="store_true")
    group.add_argument("--build", type=Path)
    group.add_argument("--build-companion", type=Path)
    args = parser.parse_args()

    if args.check_source:
        validate_mod(SOURCE, exact_files=False)
        print("FS25 mod source layout valid.")
    elif args.build_companion:
        build_companion(args.build_companion.resolve())
    else:
        build(args.build.resolve())


if __name__ == "__main__":
    main()
