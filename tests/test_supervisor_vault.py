import subprocess
import sys
import time

from survival.supervisor import AIProcesses
from survival.vault import Vault


def _stat(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def test_pause_freezes_resume_unfreezes_death_kills():
    ai = AIProcesses()
    child = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.1)"]
    brain = ai.spawn("brain", child)
    script = ai.spawn("script:dip-watch.py", child)
    time.sleep(0.3)
    ai.pause()
    time.sleep(0.2)
    assert _stat(brain.pid).startswith("T") and _stat(script.pid).startswith("T")
    ai.resume()
    time.sleep(0.2)
    assert not _stat(brain.pid).startswith("T")
    ai.pause()
    ai.stop_all(grace=2)  # death while paused still kills
    assert brain.poll() is not None and script.poll() is not None
    try:
        ai.spawn("brain", child)
        raise AssertionError("spawn after death must fail")
    except RuntimeError:
        pass


def test_vault_roundtrip_redacts_and_never_reprs_values(tmp_path):
    v = Vault(tmp_path / "secrets")
    v.put("xai_api_key", "xai-SECRETSECRET123")
    v.put_card("Kestrel", "4111 1111 1111 4417", 12, 2030, "123")
    assert v.get("xai_api_key") == "xai-SECRETSECRET123"
    assert v.card_last4() == "4417"
    assert "SECRET" not in repr(v)
    text = "key xai-SECRETSECRET123 card 4111 1111 1111 4417 and 4111111111114417"
    out = v.redact(text)
    assert "SECRET" not in out and "4417" not in out
    assert b"SECRET" not in (tmp_path / "secrets" / "vault.enc").read_bytes()
    assert oct((tmp_path / "secrets" / "vault.key").stat().st_mode)[-3:] == "600"
    assert Vault(tmp_path / "secrets").get("xai_api_key") == "xai-SECRETSECRET123"
