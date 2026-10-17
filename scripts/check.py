"""Run every pre-game CI check. Invoke from any working directory."""
from pathlib import Path
import subprocess
import sys
import tempfile

from lupa import lua51, lua54

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


if __name__ == "__main__":
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    run(sys.executable, "-m", "compileall", "-q", "companion", "protocol",
        "adapters", "simulator", "tests", "scripts")
    for runtime in (lua51, lua54):  # FS25's embedded Lua version is unverified.
        lua = runtime.LuaRuntime()
        compile_lua = lua.eval("function(source, name) assert((loadstring or load)(source, name)) end")
        for path in sorted((ROOT / "mod").rglob("*.lua")):
            compile_lua(path.read_text(), str(path.relative_to(ROOT)))
    run(sys.executable, "scripts/package_probe.py", "--check-source")
    with tempfile.TemporaryDirectory() as folder:   # the packaged app imports on its own
        app = Path(folder) / "app"
        run(sys.executable, "scripts/package_probe.py", "--build-app", str(app))
        subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); "
                        "import companion.service, companion.setup, companion.run"], cwd=app, check=True)
    run("git", "diff", "--check", "HEAD")
    print("All CI checks passed (Python, schema, Lua, package layouts, whitespace).", flush=True)
