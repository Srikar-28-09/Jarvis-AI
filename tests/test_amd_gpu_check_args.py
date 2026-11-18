import subprocess
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS_DIR / "docker" / "check-docker-amd-gpu.sh"
LEGACY_SCRIPT = SCRIPTS_DIR / "check-docker-amd-gpu.sh"


def test_amd_gpu_check_rejects_unknown_extra_arg_before_diagnostics():
    for script in (SCRIPT, LEGACY_SCRIPT):
        proc = subprocess.run(
            ["bash", str(script), "--bad-option"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 1
        assert "Unknown option: --bad-option" in proc.stderr


def test_amd_gpu_check_shell_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    subprocess.run(["bash", "-n", str(LEGACY_SCRIPT)], check=True)
