#!/bin/bash
# Test script for the Distance-Vector Router
# Run this from the host machine after docker-compose up

echo "============================================"
echo " DV Router Test Suite"
echo "============================================"
echo ""

echo "[1] Waiting for topology to converge (20 seconds)..."
sleep 20

echo ""
echo "============================================"
echo " TEST: Routing Tables After Convergence"
echo "============================================"

echo ""
echo "--- Router A Routing Table ---"
docker exec router_a python3 -c "
import socket, json
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(2)
print('Kernel routes:')
" 2>/dev/null
docker exec router_a ip route
echo ""

echo "--- Router B Routing Table ---"
docker exec router_b ip route
echo ""

echo "--- Router C Routing Table ---"
docker exec router_c ip route
echo ""

echo "============================================"
echo " TEST: Connectivity (Ping Tests)"
echo "============================================"

echo ""
echo "[2] Router A -> Router B (10.0.1.2) ..."
docker exec router_a ping -c 2 -W 2 10.0.1.2
echo ""

echo "[3] Router A -> Router C (10.0.3.2) ..."
docker exec router_a ping -c 2 -W 2 10.0.3.2
echo ""

echo "[4] Router B -> Router C (10.0.2.2) ..."
docker exec router_b ping -c 2 -W 2 10.0.2.2
echo ""

echo "[5] Router A -> Net_BC subnet (10.0.2.1 via learned route) ..."
docker exec router_a ping -c 2 -W 2 10.0.2.1
echo ""

echo "============================================"
echo " TEST: Failover (Stop Router C)"
echo "============================================"

echo "[6] Stopping Router C..."
docker stop router_c
echo "    Router C stopped. Waiting 25 seconds for route expiry and reconvergence..."
sleep 25

echo ""
echo "--- Router A Routing Table (after C stopped) ---"
docker exec router_a ip route
echo ""

echo "--- Router B Routing Table (after C stopped) ---"
docker exec router_b ip route
echo ""

echo "============================================"
echo " TEST: Restore Router C"
echo "============================================"

echo "[7] Starting Router C again..."
docker start router_c
echo "    Router C started. Waiting 20 seconds for reconvergence..."
sleep 20

echo ""
echo "--- Router A Routing Table (after C restored) ---"
docker exec router_a ip route
echo ""

echo "--- Router B Routing Table (after C restored) ---"
docker exec router_b ip route
echo ""

echo "--- Router C Routing Table (after C restored) ---"
docker exec router_c ip route
echo ""

echo "[8] Router A -> Router C (10.0.3.2) after restore ..."
docker exec router_a ping -c 2 -W 2 10.0.3.2
echo ""

echo "============================================"
echo " TEST SUITE COMPLETE"
echo "============================================"
