"""Security and behavior tests for the deployment webhook."""

from __future__ import annotations

import importlib.util
import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

WEBHOOK_PATH = Path(__file__).resolve().parents[1] / "deploy" / "webhook.py"


def load_webhook(monkeypatch, *, bind="127.0.0.1"):
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-secret")
    monkeypatch.setenv("WEBHOOK_BIND", bind)
    monkeypatch.setenv("WEBHOOK_PORT", "9090")
    spec = importlib.util.spec_from_file_location("webhook_under_test", WEBHOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def handler(module, path, method="GET", token=""):
    # Bind methods from the real handler but replace its response writer for tests.
    class Concrete(module.WebhookHandler):
        def __init__(self):
            self.path = path
            self.headers = {"Authorization": f"Bearer {token}"} if token else {}
            self.responses = []
            self.wfile = io.BytesIO()

        def _respond(self, code, data):
            self.responses.append((code, data))

    instance = Concrete.__new__(Concrete)
    instance.path = path
    instance.headers = {"Authorization": f"Bearer {token}"} if token else {}
    instance.responses = []
    instance.wfile = io.BytesIO()
    getattr(instance, f"do_{method}")()
    return instance.responses


def test_config_requires_loopback(monkeypatch):
    module = load_webhook(monkeypatch, bind="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        module.validate_config()


def test_config_rejects_invalid_port(monkeypatch):
    module = load_webhook(monkeypatch)
    module.PORT = 70000
    with pytest.raises(ValueError, match="WEBHOOK_PORT"):
        module.validate_config()


def test_health_is_public_and_only_returns_liveness(monkeypatch):
    module = load_webhook(monkeypatch)
    response = handler(module, "/health")
    assert response == [(200, {"status": "ok"})]


def test_get_cannot_trigger_run(monkeypatch):
    module = load_webhook(monkeypatch)
    with patch.object(module.subprocess, "Popen") as popen:
        response = handler(module, "/run", "GET", "test-secret")
    assert response == [(405, {"error": "method not allowed"})]
    popen.assert_not_called()


def test_run_requires_authentication(monkeypatch):
    module = load_webhook(monkeypatch)
    with patch.object(module.subprocess, "Popen") as popen:
        response = handler(module, "/run", "POST")
    assert response == [(401, {"error": "unauthorized"})]
    popen.assert_not_called()


def test_post_run_launches_with_auth_and_returns_accepted(monkeypatch, tmp_path):
    module = load_webhook(monkeypatch)
    module.LOG_FILE = tmp_path / "logs" / "cron.log"
    with patch.object(module, "is_running", return_value=False), patch.object(
        module.subprocess, "Popen"
    ) as popen:
        response = handler(module, "/run", "POST", "test-secret")
    assert response == [(202, {"status": "started"})]
    popen.assert_called_once()
    assert popen.call_args.kwargs["env"]["TRIGGER"] == "webhook"


def test_run_reports_launch_error(monkeypatch, tmp_path):
    module = load_webhook(monkeypatch)
    module.LOG_FILE = tmp_path / "logs" / "cron.log"
    with patch.object(module, "is_running", return_value=False), patch.object(
        module.subprocess, "Popen", side_effect=OSError("missing runner")
    ):
        response = handler(module, "/run", "POST", "test-secret")
    assert response == [(500, {"error": "could not start run"})]


def test_open_port_script_refuses_public_ingress():
    script = WEBHOOK_PATH.parent / "open-webhook-port.sh"
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "Refusing to open TCP/9090" in proc.stderr
    assert "authorize-security-group-ingress" not in script.read_text()


def test_installer_does_not_print_token():
    script = (WEBHOOK_PATH.parent / "install-webhook.sh").read_text()
    assert 'echo "  Token: ${TOKEN}"' not in script
    assert "?token=" not in script


def test_systemd_service_uses_sandboxing():
    service = (WEBHOOK_PATH.parent / "webhook.service").read_text()
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "UMask=0077" in service
