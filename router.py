import socket
import json
import threading
import time
import os
import subprocess

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = 5000

BROADCAST_INTERVAL = 5
ROUTE_TIMEOUT = 15
METRIC_INFINITY = 16

routing_table_lock = threading.Lock()

# routing_table: { subnet: [distance, next_hop, last_updated] }
routing_table = {}


def get_local_subnets():
    """Discover subnets on this router's own interfaces."""
    subnets = []
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "inet":
                    cidr = parts[i + 1]
                    ip_addr, prefix_len = cidr.split("/")
                    subnet = compute_network(ip_addr, int(prefix_len))
                    subnet_cidr = f"{subnet}/{prefix_len}"
                    if not ip_addr.startswith("127."):
                        subnets.append(subnet_cidr)
    except Exception as e:
        print(f"[{MY_IP}] Error discovering local subnets: {e}", flush=True)
    return subnets


def compute_network(ip_str, prefix_len):
    """Given an IP and prefix length, return the network address."""
    octets = list(map(int, ip_str.split(".")))
    ip_int = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    mask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF
    net_int = ip_int & mask
    return f"{(net_int >> 24) & 0xFF}.{(net_int >> 16) & 0xFF}.{(net_int >> 8) & 0xFF}.{net_int & 0xFF}"


def init_routing_table():
    """Populate routing table with directly connected subnets (distance 0)."""
    local_subnets = get_local_subnets()
    with routing_table_lock:
        for subnet in local_subnets:
            routing_table[subnet] = [0, "0.0.0.0", time.time()]
    print(f"[{MY_IP}] Initialized with local subnets: {local_subnets}", flush=True)


def build_update_packet(destination_ip):
    """Build a DV-JSON packet, applying Split Horizon with Poisoned Reverse."""
    routes = []
    with routing_table_lock:
        for subnet, (distance, next_hop, _) in routing_table.items():
            if next_hop == destination_ip and distance > 0:
                routes.append({"subnet": subnet, "distance": METRIC_INFINITY})
            else:
                routes.append({"subnet": subnet, "distance": distance})

    packet = {
        "router_id": MY_IP,
        "version": 1.0,
        "routes": routes,
    }
    return json.dumps(packet).encode("utf-8")


def broadcast_updates():
    """Periodically send routing table updates to all neighbors."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        time.sleep(BROADCAST_INTERVAL)
        for neighbor in NEIGHBORS:
            try:
                data = build_update_packet(neighbor)
                sock.sendto(data, (neighbor, PORT))
            except Exception as e:
                print(f"[{MY_IP}] Error sending to {neighbor}: {e}", flush=True)
        expire_stale_routes()


def listen_for_updates():
    """Listen on UDP port 5000 for DV-JSON routing updates."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print(f"[{MY_IP}] Listening for routing updates on port {PORT}...", flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            packet = json.loads(data.decode("utf-8"))

            if packet.get("version") != 1.0:
                print(f"[{MY_IP}] Ignoring packet with unknown version from {addr}", flush=True)
                continue

            neighbor_ip = packet["router_id"]
            routes = packet["routes"]
            update_logic(neighbor_ip, routes)
        except json.JSONDecodeError:
            print(f"[{MY_IP}] Received malformed packet from {addr}", flush=True)
        except Exception as e:
            print(f"[{MY_IP}] Error processing packet: {e}", flush=True)


def update_logic(neighbor_ip, routes_from_neighbor):
    """Implement Bellman-Ford: compare received distances + 1 vs current distances."""
    changed = False
    with routing_table_lock:
        for route in routes_from_neighbor:
            subnet = route["subnet"]
            advertised_distance = route["distance"]
            new_distance = min(advertised_distance + 1, METRIC_INFINITY)

            if subnet not in routing_table:
                if new_distance < METRIC_INFINITY:
                    routing_table[subnet] = [new_distance, neighbor_ip, time.time()]
                    apply_route(subnet, neighbor_ip)
                    changed = True
            else:
                current_distance, current_next_hop, _ = routing_table[subnet]

                if current_next_hop == neighbor_ip:
                    # Update from the same neighbor we're currently routing through
                    if new_distance != current_distance:
                        routing_table[subnet] = [new_distance, neighbor_ip, time.time()]
                        if new_distance >= METRIC_INFINITY:
                            remove_route(subnet)
                        else:
                            apply_route(subnet, neighbor_ip)
                        changed = True
                    else:
                        routing_table[subnet][2] = time.time()
                elif new_distance < current_distance:
                    # Found a shorter path through a different neighbor
                    routing_table[subnet] = [new_distance, neighbor_ip, time.time()]
                    apply_route(subnet, neighbor_ip)
                    changed = True

    if changed:
        print_routing_table()


def apply_route(subnet, next_hop):
    """Install or replace a route in the Linux kernel routing table."""
    if next_hop == "0.0.0.0":
        return
    try:
        os.system(f"ip route replace {subnet} via {next_hop}")
        print(f"[{MY_IP}] Route updated: {subnet} via {next_hop}", flush=True)
    except Exception as e:
        print(f"[{MY_IP}] Failed to apply route {subnet} via {next_hop}: {e}", flush=True)


def remove_route(subnet):
    """Remove a route from the Linux kernel routing table."""
    try:
        os.system(f"ip route del {subnet} 2>/dev/null")
        print(f"[{MY_IP}] Route removed: {subnet}", flush=True)
    except Exception as e:
        print(f"[{MY_IP}] Failed to remove route {subnet}: {e}", flush=True)


def expire_stale_routes():
    """Remove routes that haven't been refreshed within the timeout window."""
    now = time.time()
    expired = []
    with routing_table_lock:
        for subnet, (distance, next_hop, last_updated) in list(routing_table.items()):
            if distance == 0:
                continue
            if now - last_updated > ROUTE_TIMEOUT:
                expired.append(subnet)
                routing_table[subnet] = [METRIC_INFINITY, next_hop, last_updated]
                remove_route(subnet)

    if expired:
        print(f"[{MY_IP}] Expired routes: {expired}", flush=True)
        print_routing_table()


def print_routing_table():
    """Print the current routing table for debugging."""
    with routing_table_lock:
        print(f"\n[{MY_IP}] === Routing Table ===", flush=True)
        print(f"  {'Subnet':<20} {'Distance':<10} {'Next Hop':<16}", flush=True)
        print(f"  {'-'*46}", flush=True)
        for subnet, (distance, next_hop, _) in sorted(routing_table.items()):
            dist_str = str(distance) if distance < METRIC_INFINITY else "INF"
            print(f"  {subnet:<20} {dist_str:<10} {next_hop:<16}", flush=True)
        print(flush=True)


if __name__ == "__main__":
    print(f"[{MY_IP}] Starting DV Router Daemon...", flush=True)
    print(f"[{MY_IP}] Neighbors: {NEIGHBORS}", flush=True)

    init_routing_table()
    print_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    listen_for_updates()
