#!/bin/bash
# Matrixx port-forward watchdog
# Keeps kubectl port-forward alive by restarting it when it dies.
# Usage: nohup bash matrixx_portforward.sh > matrixx_pf.log 2>&1 &

export AWS_ACCESS_KEY_ID="$1"
export AWS_SECRET_ACCESS_KEY="$2"
export AWS_SESSION_TOKEN="$3"

if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] || [ -z "$AWS_SESSION_TOKEN" ]; then
    echo "Usage: bash matrixx_portforward.sh <AWS_KEY_ID> <AWS_SECRET> <AWS_SESSION_TOKEN>"
    exit 1
fi

echo "$(date) — Matrixx port-forward watchdog started"

while true; do
    echo "$(date) — Starting kubectl port-forward..."

    # Kill any existing port-forward on 9090
    pkill -f "kubectl port-forward" 2>/dev/null
    sleep 2

    # Start port-forward
    kubectl port-forward svc/rsgateway-ag1 9090:8080 -n matrixx &
    PF_PID=$!

    echo "$(date) — port-forward started (PID $PF_PID)"

    # Wait for it to die
    wait $PF_PID
    EXIT_CODE=$?

    echo "$(date) — port-forward died (exit $EXIT_CODE), restarting in 5s..."
    sleep 5
done
