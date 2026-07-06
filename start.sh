#!/bin/bash
# ============================================================
#  Gemini OpenAI Proxy — Linux/macOS Start Script
# ============================================================
#
#  Usage:
#    ./start.sh              # Interactive launcher
#    ./start.sh foreground   # Run in foreground
#    ./start.sh background   # Run in background (nohup)
#    ./start.sh stop         # Stop background server
#    ./start.sh status       # Check server status
#    ./start.sh install      # Install as systemd service
#
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_SCRIPT="$SCRIPT_DIR/openai_server.py"
LOG_FILE="$SCRIPT_DIR/server.log"
PID_FILE="$SCRIPT_DIR/server.pid"
ENV_FILE="$SCRIPT_DIR/.env"

# Load port from .env
PORT=$(grep -oP '^PORT=\K[0-9]+' "$ENV_FILE" 2>/dev/null || echo "3897")

# Colors
GREEN='\033[92m'
RED='\033[91m'
YELLOW='\033[93m'
CYAN='\033[96m'
BOLD='\033[1m'
RESET='\033[0m'

# Find Python
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo -e "${RED}Python not found! Install Python 3.10+${RESET}"
    exit 1
fi

banner() {
    echo -e "
${CYAN}${BOLD}╔══════════════════════════════════════════════════════════╗
║          Gemini → OpenAI Proxy Server                    ║
╚══════════════════════════════════════════════════════════╝${RESET}
"
}

check_port() {
    if command -v ss &>/dev/null; then
        ss -tlnp 2>/dev/null | grep -q ":$PORT " && return 0
    elif command -v lsof &>/dev/null; then
        lsof -i ":$PORT" &>/dev/null && return 0
    elif command -v netstat &>/dev/null; then
        netstat -tlnp 2>/dev/null | grep -q ":$PORT " && return 0
    fi
    return 1
}

kill_port() {
    if check_port; then
        echo -e "${YELLOW}Port $PORT is in use. Killing...${RESET}"
        if command -v fuser &>/dev/null; then
            fuser -k "$PORT/tcp" 2>/dev/null || true
        elif command -v lsof &>/dev/null; then
            kill -9 $(lsof -ti ":$PORT") 2>/dev/null || true
        fi
        sleep 1
    fi
}

start_foreground() {
    kill_port
    echo -e "${GREEN}Starting server on port $PORT...${RESET}"
    echo -e "Press Ctrl+C to stop\n"
    cd "$SCRIPT_DIR"
    exec "$PYTHON" "$SERVER_SCRIPT"
}

start_background() {
    kill_port
    echo -e "${GREEN}Starting server in background...${RESET}"
    cd "$SCRIPT_DIR"
    nohup "$PYTHON" "$SERVER_SCRIPT" > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo -e "${GREEN}✓ Server started!${RESET}"
    echo "  PID:  $(cat "$PID_FILE")"
    echo "  Port: $PORT"
    echo "  Logs: tail -f $LOG_FILE"
    echo "  Stop: $0 stop"
}

stop_server() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo -e "${GREEN}✓ Stopped server (PID $PID)${RESET}"
        else
            echo -e "${YELLOW}Process $PID not running${RESET}"
        fi
        rm -f "$PID_FILE"
    else
        echo -e "${YELLOW}No PID file found${RESET}"
        kill_port
    fi
}

show_status() {
    if check_port; then
        echo -e "  Port $PORT: ${GREEN}🟢 RUNNING${RESET}"
    else
        echo -e "  Port $PORT: ${RED}🔴 STOPPED${RESET}"
    fi
    if [ -f "$PID_FILE" ]; then
        echo "  PID: $(cat "$PID_FILE")"
    fi
    if [ -f "$LOG_FILE" ]; then
        echo "  Log: $LOG_FILE ($(wc -c < "$LOG_FILE") bytes)"
    fi

    # Check systemd
    if systemctl is-active gemini-openai-proxy &>/dev/null; then
        echo -e "  systemd: ${GREEN}active${RESET}"
    fi
}

install_service() {
    SERVICE_NAME="gemini-openai-proxy"
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    USER=$(whoami)

    cat > "/tmp/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Gemini OpenAI Proxy Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SERVER_SCRIPT
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=multi-user.target
EOF

    echo -e "${CYAN}Installing systemd service...${RESET}"
    sudo cp "/tmp/${SERVICE_NAME}.service" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE_NAME"

    echo -e "${GREEN}✓ Service installed and started!${RESET}"
    echo "  Status:  sudo systemctl status $SERVICE_NAME"
    echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "  Remove:  sudo systemctl disable $SERVICE_NAME && sudo rm $SERVICE_FILE"
}

# Handle CLI args
case "${1:-}" in
    foreground) banner; start_foreground ;;
    background) banner; start_background ;;
    stop)       stop_server ;;
    status)     show_status ;;
    install)    install_service ;;
    *)
        banner
        echo -e "${BOLD}How would you like to run the server?${RESET}\n"
        echo -e "  ${CYAN}1${RESET}) Foreground       — Run here, Ctrl+C to stop"
        echo -e "  ${CYAN}2${RESET}) Background       — Run with nohup (survives terminal close)"
        echo -e "  ${CYAN}3${RESET}) systemd Service  — Install as system service (auto-start on boot)"
        echo -e "  ${CYAN}4${RESET}) Status           — Check server status"
        echo -e "  ${CYAN}5${RESET}) Stop             — Stop background server"
        echo ""
        read -rp "  Choose [1-5]: " choice
        case "$choice" in
            1) start_foreground ;;
            2) start_background ;;
            3) install_service ;;
            4) show_status ;;
            5) stop_server ;;
            *) echo -e "${RED}Invalid choice.${RESET}" ;;
        esac
        ;;
esac
