"""Run the Epic Beta browser experience on this machine: MOCK data only, loopback only, no accounts, no network.

    python scripts/run_beta_local.py            # serve https://beta.invalid/beta/app/ on 127.0.0.1:443
    python scripts/run_beta_local.py --open     # also start a throwaway Chromium/Chrome/Edge profile wired to it

What this is NOT: genuine authentication (the invented identity provider authenticates nobody), Epic access, LIVE data
or anything deployable. See api/beta_local.py. Build the frontend first (once, and after UI changes):

    cd frontend && npx vite build --config vite.beta.config.ts
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CLINICAL_TRACE_SKIP_DOTENV"] = "1"  # a local .env can switch LangSmith tracing on
for _name in [n for n in os.environ if n.startswith(("LANGSMITH", "LANGCHAIN", "EPIC_SANDBOX"))]:
    os.environ.pop(_name)

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_browser() -> str | None:
    return (
        next((path for path in CANDIDATES if Path(path).exists()), None)
        or shutil.which("chrome")
        or shutil.which("chromium")
    )


async def serve(app, certificate) -> None:
    import uvicorn

    from api.beta_local import server_config

    server = uvicorn.Server(server_config(app, certificate))
    await server.serve()


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]  # the instructions must appear before serving
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assets", type=Path, default=ROOT / "frontend" / "dist-beta", help="built Beta frontend")
    parser.add_argument("--data-dir", type=Path, help="keep state here (default: a fresh temporary directory)")
    parser.add_argument("--open", action="store_true", help="start a throwaway browser profile wired to this server")
    args = parser.parse_args()

    if not (args.assets / ".vite" / "manifest.json").is_file():
        print(
            f"No built Beta frontend at {args.assets}.\nBuild it with:  cd frontend && npx vite build --config vite.beta.config.ts"
        )
        return 2

    from api.beta_local import (
        BIND,
        HOST,
        IDP_HOST,
        PORT,
        browser_command,
        build_local_app,
        initialize_runtime_dependencies,
        make_certificate,
    )

    print("Initializing local MOCK runtime dependencies on the main thread...")
    try:
        initialization_s = initialize_runtime_dependencies()
    except Exception as error:
        print(f"Local MOCK runtime initialization failed ({type(error).__name__}); server not started.")
        return 1
    print(f"Local MOCK runtime dependencies ready in {initialization_s:.3f} s.")

    temporary = None if args.data_dir else tempfile.mkdtemp(prefix="beta-local-")
    data = args.data_dir or Path(temporary or "")
    data.mkdir(parents=True, exist_ok=True)
    certificate = make_certificate(data)
    app = build_local_app(data, args.assets)
    profile = Path(tempfile.mkdtemp(prefix="beta-local-profile-"))
    browser = find_browser()
    command = browser_command(browser or "<chrome>", profile, certificate)

    print(f"Epic Beta, local MOCK only: https://{HOST}/beta/app/  (listening on {BIND}:{PORT})")
    print("Synthetic patients, in-process Epic double, invented sign-in that authenticates nobody. Ctrl+C to stop.\n")
    print("Open it with a browser that maps the two invented names to loopback and trusts only this run's key:\n")
    print("  " + subprocess.list2cmdline(command) + "\n")
    print(f"(names mapped: {HOST}, {IDP_HOST}; state: {data}{'' if args.data_dir else ' (deleted on exit)'})")
    child = None
    try:
        if args.open:
            if browser is None:
                print("No Chrome/Chromium/Edge found; use the command above with your browser.")
            else:
                child = subprocess.Popen(command)
        asyncio.run(serve(app, certificate))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(
            f"Could not listen on {BIND}:{PORT} ({error}). Stop whatever uses port {PORT} (the app's origin has no port)."
        )
        return 1
    finally:
        if child is not None:
            child.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
