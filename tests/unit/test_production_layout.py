from pathlib import Path


def test_production_layout_contains_only_required_state_services() -> None:
    compose = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres:" in compose
    assert "redis:" in compose
    assert "mqtt:" in compose
    # Object artifacts use the workspace filesystem; no unused object-store
    # service is started merely for appearance.
    assert "minio:" not in compose.casefold()
    env = Path(".env.example").read_text(encoding="utf-8")
    assert "SCHEDULER_POLICY=ortools" in env
    assert "SCHEDULER_ALLOW_FALLBACK=false" in env
    assert "MEMORY_REDIS_URL=" in env
