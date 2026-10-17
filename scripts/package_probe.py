"""Validate and build the exact install folders for the user's Windows PC.

    --check-source              validate mod/ as CI does
    --build dist/FS25_Farmhand  the FS25 mod folder alone
    --build-app dist/app        the companion sources alone (no network)
    --build-package dist/FS25Farmhand
                                the qualification package: Setup.cmd, Uninstall.cmd,
                                README.txt, mod/, app/ and runtime/ (the official
                                Windows embeddable Python with the pinned packages);
                                needs network. Never the release: that is built only
                                after the user approves the product for release.
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


# The companion's runtime (the package's app folder): never tests, the simulator
# or development tooling.
APP_FILES = (
    "adapters/fs25.py",
    "adapters/world.py",
    "companion/ai.py",
    "companion/bridge.py",
    "companion/coordinator.py",
    "companion/executor.py",
    "companion/hitch.py",
    "companion/language.py",
    "companion/model.py",
    "companion/run.py",
    "companion/service.py",
    "companion/setup.py",
    "companion/voice.py",
    "protocol/command.schema.json",
    "protocol/frame.schema.json",
    "protocol/intent.schema.json",
    "protocol/ipc.py",
    "protocol/model.py",
)
# The official Windows embeddable Python; sha256 equals the digest python.org signed
# in python-3.12.10-embed-amd64.zip.sigstore (checked 2026-10-17).
EMBEDDED_PYTHON = {
    "url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
    "sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
}
# runtime\python312._pth: the app beside the runtime, the pinned packages inside it.
PTH = "python312.zip\n.\nLib\\site-packages\n..\\app\nimport site\n"
LAUNCHERS = {
    "Setup.cmd": '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" -m companion.setup install\r\n',
    "Uninstall.cmd": '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" -m companion.setup uninstall\r\n',
}
README = """FS25 Farmhand

Install: double-click Setup.cmd. Nothing else is needed: this folder carries its
own Python. Setup puts the mod into Farming Simulator 25, installs the companion
for this Windows user and starts it now and at every logon.

Use, in FS25 on foot: Left Alt + K to tell Farmhand what to do ("bring me a tractor
and forage wagon", "come here", "take this back to the barn", "what are you
doing?", "stop"); Left Alt + L to stop; Left Alt + J to bring the pair here;
Left Alt + N to choose an optional AI model; hold Left Alt + V to speak (needs a
microphone).

Remove: double-click Uninstall.cmd.
"""


def build_app(destination: Path) -> list[str]:
    """The companion sources; returns manifest rows."""
    if destination.exists():
        shutil.rmtree(destination)
    rows = []
    for name in APP_FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            fail(f"missing regular companion file: {name}")
        out = destination / name
        out.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        out.write_bytes(data)
        rows.append(f"{hashlib.sha256(data).hexdigest()}  app/{name}")
    return rows


def build_runtime(destination: Path) -> None:
    """Embedded Python with the pinned runtime and voice wheels for win_amd64."""
    import io
    import subprocess
    import sys
    import urllib.request
    import zipfile
    with urllib.request.urlopen(EMBEDDED_PYTHON["url"], timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != EMBEDDED_PYTHON["sha256"]:
        fail("embedded Python does not match its pinned sha256")
    if destination.exists():
        shutil.rmtree(destination)
    zipfile.ZipFile(io.BytesIO(data)).extractall(destination)
    (destination / "python312._pth").write_text(PTH, encoding="utf-8", newline="\r\n")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--disable-pip-version-check",
                    "--target", str(destination / "Lib" / "site-packages"), "--platform", "win_amd64",
                    "--python-version", "3.12", "--implementation", "cp", "--only-binary=:all:",
                    "-r", str(ROOT / "requirements-voice.txt")], check=True)


def build_package(destination: Path) -> None:
    """The qualification package (a folder; CI zips it as a test artifact only)."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    build(destination / "mod" / "FS25_Farmhand")
    rows = build_app(destination / "app")
    build_runtime(destination / "runtime")
    for name, text in LAUNCHERS.items():
        (destination / name).write_bytes(text.encode("ascii"))
    (destination / "README.txt").write_text(README.replace("\n", "\r\n"), encoding="utf-8", newline="")
    site = destination / "runtime" / "Lib" / "site-packages"
    for junk in [p for p in site.rglob("*") if p.is_dir() and p.name in ("tests", "__pycache__")]:
        shutil.rmtree(junk, ignore_errors=True)            # third-party self-tests: never imported
    unwanted = [p for p in destination.rglob("*") if p.name in (".git", "tests", "simulator", "__pycache__",
                                                               ".venv", "farmhand.json") or p.suffix == ".pyc"]
    if unwanted:
        fail(f"the package contains development files: {unwanted[:5]}")
    (destination / "APP_SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    print(f"Farmhand package built at {destination}")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-source", action="store_true")
    group.add_argument("--build", type=Path)
    group.add_argument("--build-app", type=Path)
    group.add_argument("--build-package", type=Path)
    args = parser.parse_args()

    if args.check_source:
        validate_mod(SOURCE, exact_files=False)
        print("FS25 mod source layout valid.")
    elif args.build_app:
        build_app(args.build_app.resolve())
        print(f"Companion app built at {args.build_app}")
    elif args.build_package:
        build_package(args.build_package.resolve())
    else:
        build(args.build.resolve())


if __name__ == "__main__":
    main()
