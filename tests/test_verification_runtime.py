from maajun.config import DeploymentConfig
from maajun.verification_runtime import verification_runtime_mismatch


def test_stale_python_environment_is_explained():
    deployment = DeploymentConfig(
        service_command=(
            "{ path=/srv/app/venv-release/bin/uvicorn ; argv[]=uvicorn app:api }"
        )
    )

    warning = verification_runtime_mismatch(
        "cd backend && /srv/app/venv/bin/python -m pytest -q", deployment
    )

    assert "/srv/app/venv" in warning
    assert "/srv/app/venv-release" in warning
