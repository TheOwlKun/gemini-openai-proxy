#!/usr/bin/env python3
"""
Cross-Platform Launcher for Gemini OpenAI Proxy Server

Supports:
    - Windows (foreground, background via pythonw, Task Scheduler service)
    - Linux/macOS (foreground, background via nohup, systemd service)
    - Docker

Usage:
    python launch.py
"""

import os
import sys
import platform
import subprocess
import shutil
import textwrap
from pathlib import Path

# ANSI Colors (works on modern Windows 10+ and Linux/macOS)
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

SCRIPT_DIR = Path(__file__).parent.resolve()
SERVER_SCRIPT = SCRIPT_DIR / "openai_server.py"
ENV_FILE = SCRIPT_DIR / ".env"
LOG_FILE = SCRIPT_DIR / "server.log"
PID_FILE = SCRIPT_DIR / "server.pid"

SYSTEM = platform.system()  # "Windows", "Linux", "Darwin"


def banner():
    print(f"""
{CYAN}{BOLD}+============================================================+
|          Gemini -> OpenAI Proxy Server Launcher            |
|                                                            |
|   OS: {SYSTEM:<20}  Python: {platform.python_version():<16}  |
+============================================================+{RESET}
""")


def check_env():
    """Check if .env file exists and has required cookies."""
    if not ENV_FILE.exists():
        print(f"{RED}[FAIL] No .env file found!{RESET}")
        print(f"  Create one at: {ENV_FILE}")
        print(f"  Required fields: SECURE_1PSID, SECURE_1PSIDTS")

        example = SCRIPT_DIR / ".env.example"
        if example.exists():
            print(f"  Copy from: {example}")

        return False

    with open(ENV_FILE) as f:
        content = f.read()

    has_psid = "SECURE_1PSID=" in content and "your_cookie" not in content
    has_psidts = "SECURE_1PSIDTS=" in content and "your_cookie" not in content

    if not has_psid or not has_psidts:
        print(f"{YELLOW}[!] .env file found but cookies may not be configured.{RESET}")
        print(f"  Edit: {ENV_FILE}")
        return True  # Still allow starting

    print(f"{GREEN}[OK] .env file configured{RESET}")
    return True


def check_dependencies():
    """Check if required Python packages are installed."""
    missing = []
    for pkg in ["fastapi", "uvicorn", "httpx", "dotenv", "PIL", "numpy", "curl_cffi", "pydantic"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"{YELLOW}[!] Missing packages: {', '.join(missing)}{RESET}")
        answer = input(f"  Install from requirements.txt? [Y/n]: ").strip().lower()
        if answer != "n":
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(SCRIPT_DIR / "requirements.txt")],
                cwd=str(SCRIPT_DIR),
            )
            print(f"{GREEN}[OK] Dependencies installed{RESET}")
        else:
            return False
    else:
        print(f"{GREEN}[OK] Dependencies OK{RESET}")
    return True


def is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_existing(port: int):
    """Kill any existing process on the port."""
    if not is_port_in_use(port):
        return

    print(f"{YELLOW}[!] Port {port} is already in use.{RESET}")
    answer = input(f"  Kill existing process? [Y/n]: ").strip().lower()
    if answer == "n":
        return

    if SYSTEM == "Windows":
        # Find and kill process using netstat
        try:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    print(f"{GREEN}[OK] Killed process {pid}{RESET}")
        except Exception as e:
            print(f"{RED}Failed to kill process: {e}{RESET}")
    else:
        # Linux/macOS: use fuser or lsof
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
            print(f"{GREEN}[OK] Killed process on port {port}{RESET}")
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True
                )
                if result.stdout.strip():
                    subprocess.run(["kill", "-9", result.stdout.strip()])
                    print(f"{GREEN}[OK] Killed process on port {port}{RESET}")
            except Exception:
                print(f"{RED}Could not kill existing process. Stop it manually.{RESET}")


def get_port() -> int:
    """Read port from .env or default."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("PORT="):
                    try:
                        return int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
    return 3897


# ===========================================================================
#  Run Modes
# ===========================================================================

def run_foreground():
    """Run server in the foreground (Ctrl+C to stop)."""
    port = get_port()
    kill_existing(port)
    print(f"\n{GREEN}Starting server on port {port}...{RESET}")
    print(f"{DIM}Press Ctrl+C to stop{RESET}\n")
    subprocess.run(
        [sys.executable, str(SERVER_SCRIPT)],
        cwd=str(SCRIPT_DIR),
    )


def run_background():
    """Run server in the background."""
    port = get_port()
    kill_existing(port)

    if SYSTEM == "Windows":
        # Use pythonw or START /B on Windows
        pythonw = shutil.which("pythonw")
        if pythonw:
            proc = subprocess.Popen(
                [pythonw, str(SERVER_SCRIPT)],
                cwd=str(SCRIPT_DIR),
                stdout=open(str(LOG_FILE), "w"),
                stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc = subprocess.Popen(
                [sys.executable, str(SERVER_SCRIPT)],
                cwd=str(SCRIPT_DIR),
                stdout=open(str(LOG_FILE), "w"),
                stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
    else:
        # Linux/macOS: nohup
        with open(str(LOG_FILE), "w") as log:
            proc = subprocess.Popen(
                [sys.executable, str(SERVER_SCRIPT)],
                cwd=str(SCRIPT_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    # Save PID
    PID_FILE.write_text(str(proc.pid))

    print(f"\n{GREEN}[OK] Server started in background!{RESET}")
    print(f"  PID:  {proc.pid}")
    print(f"  Port: {port}")
    print(f"  Logs: {LOG_FILE}")
    print(f"  PID file: {PID_FILE}")
    print(f"\n  To stop: {CYAN}python launch.py stop{RESET}")


def stop_background():
    """Stop a background server."""
    if not PID_FILE.exists():
        print(f"{YELLOW}No PID file found. Server may not be running.{RESET}")
        port = get_port()
        if is_port_in_use(port):
            kill_existing(port)
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        if SYSTEM == "Windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
        print(f"{GREEN}[OK] Stopped server (PID {pid}){RESET}")
    except (ProcessLookupError, OSError):
        print(f"{YELLOW}Process {pid} not found (may have already stopped){RESET}")

    PID_FILE.unlink(missing_ok=True)


def install_systemd_service():
    """Install as a systemd service (Linux only)."""
    if SYSTEM != "Linux":
        print(f"{RED}systemd is only available on Linux.{RESET}")
        return

    python_path = shutil.which("python3") or sys.executable
    service_name = "gemini-openai-proxy"
    user = os.getenv("USER", "root")

    service_content = textwrap.dedent(f"""\
        [Unit]
        Description=Gemini OpenAI Proxy Server
        After=network.target

        [Service]
        Type=simple
        User={user}
        WorkingDirectory={SCRIPT_DIR}
        ExecStart={python_path} {SERVER_SCRIPT}
        Restart=on-failure
        RestartSec=10
        StandardOutput=journal
        StandardError=journal
        EnvironmentFile={ENV_FILE}

        [Install]
        WantedBy=multi-user.target
    """)

    service_path = Path(f"/etc/systemd/system/{service_name}.service")

    print(f"\n{CYAN}This will create: {service_path}{RESET}")
    print(f"{DIM}{service_content}{RESET}")

    answer = input(f"Install systemd service? (requires sudo) [y/N]: ").strip().lower()
    if answer != "y":
        # Write to local file instead
        local_path = SCRIPT_DIR / f"{service_name}.service"
        local_path.write_text(service_content)
        print(f"\n{GREEN}[OK] Service file saved to: {local_path}{RESET}")
        print(f"  Install manually with:")
        print(f"    sudo cp {local_path} /etc/systemd/system/")
        print(f"    sudo systemctl daemon-reload")
        print(f"    sudo systemctl enable --now {service_name}")
        return

    try:
        # Write service file
        tmp_path = SCRIPT_DIR / f"{service_name}.service"
        tmp_path.write_text(service_content)

        subprocess.run(
            ["sudo", "cp", str(tmp_path), str(service_path)], check=True
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        subprocess.run(
            ["sudo", "systemctl", "enable", "--now", service_name], check=True
        )

        tmp_path.unlink(missing_ok=True)
        print(f"\n{GREEN}[OK] Service installed and started!{RESET}")
        print(f"  Status:  sudo systemctl status {service_name}")
        print(f"  Logs:    sudo journalctl -u {service_name} -f")
        print(f"  Stop:    sudo systemctl stop {service_name}")
        print(f"  Remove:  sudo systemctl disable {service_name} && sudo rm {service_path}")

    except subprocess.CalledProcessError as e:
        print(f"{RED}Failed to install service: {e}{RESET}")


def install_windows_task():
    """Install as a Windows Task Scheduler task (runs at logon)."""
    if SYSTEM != "Windows":
        print(f"{RED}Task Scheduler is only available on Windows.{RESET}")
        return

    task_name = "GeminiOpenAIProxy"
    python_path = sys.executable

    print(f"\n{CYAN}This will create a Task Scheduler task that runs at logon.{RESET}")
    answer = input(f"Install Windows scheduled task '{task_name}'? [y/N]: ").strip().lower()
    if answer != "y":
        return

    # Create a VBS wrapper to run without a visible window
    vbs_path = SCRIPT_DIR / "start_hidden.vbs"
    vbs_content = (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.CurrentDirectory = "{SCRIPT_DIR}"\n'
        f'WshShell.Run """"{python_path}"" ""{SERVER_SCRIPT}"""", 0, False\n'
    )
    vbs_path.write_text(vbs_content)

    try:
        result = subprocess.run(
            [
                "schtasks", "/Create",
                "/TN", task_name,
                "/TR", f'wscript.exe "{vbs_path}"',
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/F",
            ],
            capture_output=True, text=True,
        )

        if result.returncode == 0:
            print(f"\n{GREEN}[OK] Task '{task_name}' installed!{RESET}")
            print(f"  It will auto-start at logon.")
            print(f"  Start now:  schtasks /Run /TN {task_name}")
            print(f"  Stop:       schtasks /End /TN {task_name}")
            print(f"  Remove:     schtasks /Delete /TN {task_name} /F")
        else:
            print(f"{RED}Failed: {result.stderr}{RESET}")
            print(f"{YELLOW}Try running this script as Administrator.{RESET}")

    except Exception as e:
        print(f"{RED}Failed to create task: {e}{RESET}")


def show_status():
    """Show server status."""
    port = get_port()
    running = is_port_in_use(port)

    print(f"\n  Port {port}: {'[OK] ' + GREEN + 'RUNNING' if running else '[--] ' + RED + 'STOPPED'}{RESET}")

    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        print(f"  PID: {pid}")

    if LOG_FILE.exists():
        size = LOG_FILE.stat().st_size
        print(f"  Log: {LOG_FILE} ({size} bytes)")

    # Check systemd on Linux
    if SYSTEM == "Linux":
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "gemini-openai-proxy"],
                capture_output=True, text=True,
            )
            status = result.stdout.strip()
            if status == "active":
                print(f"  systemd: {GREEN}active{RESET}")
            elif status == "inactive":
                print(f"  systemd: {DIM}inactive{RESET}")
        except FileNotFoundError:
            pass

    print()


# ===========================================================================
#  Menu
# ===========================================================================

def main():
    banner()

    # Handle CLI arguments
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "stop":
            stop_background()
            return
        elif cmd == "status":
            show_status()
            return
        elif cmd == "foreground":
            check_dependencies()
            run_foreground()
            return
        elif cmd == "background":
            check_dependencies()
            run_background()
            return

    # Pre-flight checks
    check_env()
    check_dependencies()

    print(f"\n{BOLD}How would you like to run the server?{RESET}\n")
    print(f"  {CYAN}1{RESET}) Foreground       - Run here, Ctrl+C to stop")
    print(f"  {CYAN}2{RESET}) Background       - Run in background (survives terminal close)")

    if SYSTEM == "Linux":
        print(f"  {CYAN}3{RESET}) systemd Service  - Install as a system service (auto-start on boot)")
    elif SYSTEM == "Windows":
        print(f"  {CYAN}3{RESET}) Task Scheduler   - Install as a scheduled task (auto-start at logon)")

    print(f"  {CYAN}4{RESET}) Status           - Check server status")
    print(f"  {CYAN}5{RESET}) Stop             - Stop background server")
    print(f"  {CYAN}0{RESET}) Exit")
    print()

    try:
        choice = input(f"  {BOLD}Choose [{CYAN}1-5{RESET}{BOLD}]: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if choice == "1":
        run_foreground()
    elif choice == "2":
        run_background()
    elif choice == "3":
        if SYSTEM == "Linux":
            install_systemd_service()
        elif SYSTEM == "Windows":
            install_windows_task()
        else:
            print(f"{YELLOW}Service installation not supported on {SYSTEM}.{RESET}")
            print(f"Use Docker instead: docker-compose up -d")
    elif choice == "4":
        show_status()
    elif choice == "5":
        stop_background()
    elif choice == "0":
        pass
    else:
        print(f"{RED}Invalid choice.{RESET}")


if __name__ == "__main__":
    main()
