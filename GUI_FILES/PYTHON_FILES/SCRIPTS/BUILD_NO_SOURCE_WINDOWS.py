from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "SERVO_EVDR_APPLICATION"
ENTRYPOINT = "WEBUI_APP.py"
OUTPUT_FOLDER = "USER_APPLICATION"


def _run(command: list[str], cwd: Path) -> None:
    print("\n> " + " ".join(command))
    subprocess.run(command, cwd=str(cwd), check=True)


def _clean(paths: list[Path]) -> None:
    for item in paths:
        if not item.exists():
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink()
            except OSError:
                pass


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    entry_file = project_dir / ENTRYPOINT
    ui_source_dir = None
    ui_candidates = [
        project_dir / "WEBUI",
        project_dir / "UI_FILES",
        project_dir.parent / "WEBUI",
        project_dir.parent / "UI_FILES",
    ]
    for candidate in ui_candidates:
        if candidate.exists():
            ui_source_dir = candidate
            break

    if not entry_file.exists():
        print(f"ERROR: Missing entry script: {entry_file}")
        return 1
    if ui_source_dir is None:
        print(f"ERROR: Missing UI folder under: {project_dir}")
        print("Expected WEBUI/UI_FILES either inside PYTHON_FILES or as sibling under GUI_FILES")
        return 1
    icon_file = ui_source_dir / "ASSETS" / "ICONS" / "APP_ICON.ICO"

    build_dir = project_dir / "build"
    dist_dir = project_dir / "dist"
    spec_file = project_dir / f"{APP_NAME}.spec"
    output_dir = project_dir / OUTPUT_FOLDER
    built_exe = dist_dir / f"{APP_NAME}.exe"
    final_exe = output_dir / f"{APP_NAME}.exe"

    print("Preparing clean build folders...")
    _clean([build_dir, dist_dir, spec_file])

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--add-data",
        f"{ui_source_dir};WEBUI",
    ]
    if icon_file.exists():
        cmd.extend(["--icon", str(icon_file)])
    cmd.append(str(entry_file))

    print("Building Windows executable...")
    try:
        _run(cmd, cwd=project_dir)
    except subprocess.CalledProcessError as exc:
        print("\nBuild failed.")
        print(f"PyInstaller exit code: {exc.returncode}")
        print("Check the build output above for the exact reason.")
        print("If PyInstaller is missing, install it with:")
        print("  pip install pyinstaller")
        return exc.returncode or 1

    if not built_exe.exists():
        print(f"ERROR: Build completed but EXE not found: {built_exe}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    if final_exe.exists():
        final_exe.unlink()
    shutil.copy2(built_exe, final_exe)

    print("\nBuild success.")
    print(f"EXE ready here:\n  {final_exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
