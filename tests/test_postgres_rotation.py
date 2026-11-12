"""Credential-file replacement must preserve unrelated settings and stay private."""
import importlib.util
from pathlib import Path
import stat


spec = importlib.util.spec_from_file_location(
    "rotate_postgres", Path(__file__).resolve().parents[1] / "scripts/rotate-local-postgres.py"
)
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)


def test_replaces_all_active_assignments_without_touching_other_settings():
    original = "# POSTGRES_PASSWORD=example\nexport POSTGRES_PASSWORD=old\nOTHER=value\nPOSTGRES_PASSWORD=duplicate\n"
    updated = rotation.set_password(original, "synthetic-replacement")
    assert updated == "# POSTGRES_PASSWORD=example\nOTHER=value\nPOSTGRES_PASSWORD=synthetic-replacement\n"


def test_atomic_replacement_restricts_existing_file_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("old contents")
    path.chmod(0o644)
    rotation.write_private(path, "synthetic contents")
    assert path.read_text() == "synthetic contents"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]
