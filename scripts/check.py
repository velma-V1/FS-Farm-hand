"""Run every cloud check. Invoke from any working directory."""
from pathlib import Path
import subprocess
import sys

from lupa.lua54 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


if __name__ == "__main__":
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    run(sys.executable, "-m", "compileall", "-q", "companion", "protocol",
        "adapters", "simulator", "tests", "scripts")
    lua = LuaRuntime()
    compile_lua = lua.eval("function(source, name) assert(load(source, name)) end")
    for path in sorted((ROOT / "mod").rglob("*.lua")):
        compile_lua(path.read_text(), str(path.relative_to(ROOT)))
    run("git", "diff", "--check", "HEAD")
    print("All cloud checks passed (Python, schema, Lua, whitespace).", flush=True)
