"""Run the dashboard on a fake, offline run — no API keys, no money, no network.

    uv run python scripts/demo.py [survival|dead|genesis|funding|setup] [--port 8080]

Everything lives in a throwaway temp folder: a seeded ledger, agents, trades,
memory and a vault holding a test card. Open the printed URL and log in with
the printed demo password.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_PASSWORD = "survival-demo"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", nargs="?", default="survival",
                    choices=["survival", "dead", "genesis", "funding", "setup"])
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="council-demo-"))
    data, secrets = tmp / "data", tmp / "secrets"
    secrets.mkdir(parents=True)
    os.environ["COUNCIL_SECRETS_DIR"] = str(secrets)  # never touch the real vault

    from survival.config import load_config
    from survival.dashboard.server import Dashboard, set_password
    from survival.vault import Vault
    from tests.demo_seed import seed

    seed(data, secrets, args.phase)
    pw_file = secrets / "dashboard.json"
    set_password(pw_file, DEMO_PASSWORD.ljust(12, "!"))

    dash = Dashboard(load_config(), data, Vault(secrets), pw_file, secure_cookie=False)
    srv = dash.serve("127.0.0.1", args.port)
    print(f"Demo dashboard ({args.phase}) on http://127.0.0.1:{args.port}")
    print(f"Password: {DEMO_PASSWORD.ljust(12, '!')}")
    print(f"Fake data in {tmp}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
