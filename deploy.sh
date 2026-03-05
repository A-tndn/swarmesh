#!/bin/bash
# SwarMesh Deployment Script
# Portable — works on any VPS with Python 3.10+
#
# Usage:
#   ./deploy.sh              # Deploy and start
#   ./deploy.sh --stop       # Stop the node
#   ./deploy.sh --status     # Check status
#   ./deploy.sh --test       # Run tests only

set -e

SWARMESH_DIR="$(cd "$(dirname "$0")" && pwd)"
SWARMESH_PARENT="$(dirname "$SWARMESH_DIR")"
SWARMESH_PORT="${SWARMESH_PORT:-7770}"
PID_FILE="/tmp/swarmesh.pid"
LOG_FILE="/tmp/swarmesh.log"

export PYTHONPATH="$SWARMESH_PARENT:$PYTHONPATH"

case "${1:-deploy}" in
    deploy|--deploy)
        echo "=== SwarMesh Deployment ==="

        # Check deps
        echo "[1/4] Checking dependencies..."
        python3 -c "import solana, solders, base58, nacl, aiohttp, dotenv" 2>/dev/null || {
            echo "Installing dependencies..."
            pip3 install -q solana solders base58 pynacl aiohttp python-dotenv 2>/dev/null || \
            pip3 install --break-system-packages -q solana solders base58 pynacl aiohttp python-dotenv
        }
        echo "Dependencies OK"

        # Create .env if missing
        if [ ! -f "$SWARMESH_DIR/.env" ]; then
            echo "[2/4] Creating .env from template..."
            cp "$SWARMESH_DIR/.env.example" "$SWARMESH_DIR/.env"
        else
            echo "[2/4] .env exists, skipping"
        fi

        # Run tests
        echo "[3/4] Running tests..."
        python3 "$SWARMESH_DIR/tests/test_core.py"

        # Start node
        echo "[4/4] Starting SwarMesh node on port $SWARMESH_PORT..."
        if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            echo "Node already running (PID $(cat $PID_FILE))"
        else
            SWARMESH_PORT=$SWARMESH_PORT PYTHONPATH="$SWARMESH_PARENT" \
                nohup python3 -m swarmesh > "$LOG_FILE" 2>&1 &
            echo $! > "$PID_FILE"
            sleep 2
            if kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
                echo "SwarMesh node started (PID $(cat $PID_FILE))"
                echo "WebSocket: ws://0.0.0.0:$SWARMESH_PORT/ws"
                echo "Health:    http://0.0.0.0:$SWARMESH_PORT/health"
                echo "Logs:      $LOG_FILE"
            else
                echo "FAILED to start. Check $LOG_FILE"
                cat "$LOG_FILE"
                exit 1
            fi
        fi
        ;;

    --stop|stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            kill "$PID" 2>/dev/null && echo "Stopped (PID $PID)" || echo "Not running"
            rm -f "$PID_FILE"
        else
            echo "Not running"
        fi
        ;;

    --status|status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            echo "Running (PID $(cat $PID_FILE))"
            curl -s "http://localhost:$SWARMESH_PORT/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Health check failed"
        else
            echo "Not running"
        fi
        ;;

    --test|test)
        python3 "$SWARMESH_DIR/tests/test_core.py"
        ;;

    *)
        echo "Usage: $0 [deploy|--stop|--status|--test]"
        ;;
esac
