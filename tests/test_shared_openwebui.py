import pytest

from openwebui_bridge.config import load_settings


def test_shared_openwebui_defaults_overrides_and_opt_out(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://localhost:3000"
api_key_env = "SHARED_WEBUI_KEY"
knowledge_ids = ["shared"]
memory_write = true
[profiles.claude]
cwd = "."
[profiles.codex]
backend = "codex"
cwd = "."
allowed_tools = []
[profiles.codex.openwebui]
api_key_env = "CODEX_WEBUI_KEY"
knowledge_ids = []
memory_write = false
[profiles.off]
cwd = "."
openwebui = false
""")
    profiles = load_settings(path).profiles
    shared = profiles["claude"].openwebui
    override = profiles["codex"].openwebui
    assert shared.url == override.url == "http://localhost:3000"
    assert shared.api_key_env == "SHARED_WEBUI_KEY"
    assert shared.knowledge_ids == ["shared"]
    assert shared.memory_write is True
    assert override.api_key_env == "CODEX_WEBUI_KEY"
    assert override.knowledge_ids == []
    assert override.memory_write is False
    assert shared.knowledge and override.memory
    assert profiles["off"].openwebui is None


def test_profile_opt_out_without_shared_defaults(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text('[profiles.off]\ncwd = "."\nopenwebui = false\n')
    assert load_settings(path).profiles["off"].openwebui is None


def test_shared_openwebui_rejects_unknown_settings(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://localhost:3000"
typo = true
[profiles.test]
cwd = "."
""")
    with pytest.raises(ValueError, match="typo"):
        load_settings(path)
