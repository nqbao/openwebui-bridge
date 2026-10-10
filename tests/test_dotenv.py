import os

from openwebui_bridge.config import load_settings


def test_dotenv_uses_config_directory_and_preserves_environment(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "profiles.toml"
    path.write_text('[server]\napi_key_env = "BRIDGE_TEST_PROXY_KEY"\n[profiles.test]\ncwd = "."\n')
    (config_dir / ".env").write_text(
        'BRIDGE_TEST_PROXY_KEY="proxy from file"\n'
        "BRIDGE_TEST_WEBUI_KEY='webui from file'\n"
        "BRIDGE_TEST_EXISTING=from-file\n"
    )
    monkeypatch.chdir(tmp_path)
    for name in ("BRIDGE_TEST_PROXY_KEY", "BRIDGE_TEST_WEBUI_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRIDGE_TEST_EXISTING", "from-shell")

    settings = load_settings(path)

    assert settings.api_key == "proxy from file"
    assert os.environ["BRIDGE_TEST_WEBUI_KEY"] == "webui from file"
    assert os.environ["BRIDGE_TEST_EXISTING"] == "from-shell"
    monkeypatch.setenv("BRIDGE_TEST_PROXY_KEY", "shell-proxy")
    assert load_settings(path).api_key == "shell-proxy"


def test_missing_dotenv_does_not_search_parent_directories(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_TEST_PARENT_KEY", raising=False)
    (tmp_path / ".env").write_text("BRIDGE_TEST_PARENT_KEY=parent-secret\n")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "profiles.toml"
    path.write_text(
        '[server]\napi_key_env = "BRIDGE_TEST_PARENT_KEY"\n[profiles.test]\ncwd = "."\n'
    )

    assert load_settings(path).api_key is None
    assert "BRIDGE_TEST_PARENT_KEY" not in os.environ
