import json
import os
import socket
import subprocess
import threading
import time
import ipaddress

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = 5000

VERSION = 1.0
METRIC_INFINITY = 16
UPDATE_INTERVAL = 5
ROUTE_TIMEOUT = int(os.getenv("ROUTE_TIMEOUT", "15"))
GARBAGE_TIME = int(os.getenv("GARBAGE_TIME", "30"))
FAILURE_WAIT_DEFAULT = 30
if ROUTE_TIMEOUT >= FAILURE_WAIT_DEFAULT:
    ROUTE_TIMEOUT = FAILURE_WAIT_DEFAULT - 5

routing_table_lock = threading.Lock()
routing_table = {}
trigger_event = threading.Event()
_last_resync_locals = None


def _normalize_ip(ip):
    if isinstance(ip, str) and ip.startswith("::ffff:"):
        return ip[7:]
    return ip


def _version_ok(v):
    try:
        return float(v) == 1.0
    except (TypeError, ValueError):
        return False


def get_local_subnets():
    subnets = []
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "inet":
                    cidr = parts[i + 1]
                    try:
                        net = str(ipaddress.ip_interface(cidr).network)
                    except ValueError:
                        continue
                    if not cidr.startswith("127."):
                        subnets.append(net)
    except Exception as e:
        print(f"[{MY_IP}] get_local_subnets: {e}", flush=True)
    return subnets


def get_iface_for_next_hop(next_hop):
    if not next_hop or next_hop == "0.0.0.0":
        return None
    try:
        nh = ipaddress.ip_address(_normalize_ip(next_hop))
    except ValueError:
        return None
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[1] == "lo":
                continue
            for i, part in enumerate(parts):
                if part != "inet":
                    continue
                cidr = parts[i + 1]
                try:
                    net = ipaddress.ip_interface(cidr).network
                except ValueError:
                    continue
                if nh in net:
                    return parts[1]
    except Exception as e:
        print(f"[{MY_IP}] iface for {next_hop}: {e}", flush=True)
    return None


def get_local_ip_for_neighbor(neighbor_ip):
    try:
        target = ipaddress.ip_address(_normalize_ip(neighbor_ip))
    except ValueError:
        return MY_IP
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) < 4 or parts[1] == "lo":
                continue
            for i, part in enumerate(parts):
                if part != "inet":
                    continue
                try:
                    iface = ipaddress.ip_interface(parts[i + 1])
                except ValueError:
                    continue
                if target in iface.network:
                    return str(iface.ip)
    except Exception as e:
        print(f"[{MY_IP}] local_ip_for_neighbor: {e}", flush=True)
    return MY_IP


def _run_ip_route(args):
    return subprocess.run(args, capture_output=True, text=True)


def apply_route(subnet, next_hop):
    if next_hop in (None, "", "0.0.0.0"):
        return False
    nh = _normalize_ip(next_hop)
    iface = get_iface_for_next_hop(nh)
    candidates = []
    if iface:
        candidates.append(
            ["ip", "route", "replace", subnet, "via", nh, "dev", iface, "onlink"]
        )
        candidates.append(["ip", "route", "replace", subnet, "via", nh, "dev", iface])
    candidates.append(["ip", "route", "replace", subnet, "via", nh])
    last_err = ""
    for cmd in candidates:
        r = _run_ip_route(cmd)
        if r.returncode == 0:
            print(f"[{MY_IP}] Route OK: {' '.join(cmd)}", flush=True)
            return True
        last_err = (r.stderr or "").strip()
    print(f"[{MY_IP}] Route failed {subnet} via {nh}: {last_err}", flush=True)
    return False


def remove_route(subnet, next_hop=None):
    nh = _normalize_ip(next_hop) if next_hop else None
    candidates = []
    if nh:
        iface = get_iface_for_next_hop(nh)
        if iface:
            candidates.append(["ip", "route", "del", subnet, "via", nh, "dev", iface])
        candidates.append(["ip", "route", "del", subnet, "via", nh])
    candidates.append(["ip", "route", "del", subnet])
    for cmd in candidates:
        r = _run_ip_route(cmd)
        if r.returncode == 0:
            print(f"[{MY_IP}] Removed: {' '.join(cmd)}", flush=True)
            return
    print(f"[{MY_IP}] Remove (best-effort) {subnet} nh={nh}", flush=True)


def init_routing_table():
    now = time.time()
    with routing_table_lock:
        for subnet in get_local_subnets():
            routing_table[subnet] = [0, "0.0.0.0", now]
    print(f"[{MY_IP}] locals={list(routing_table.keys())} neighbors={NEIGHBORS}", flush=True)


def resync_local_subnets():
    global _last_resync_locals
    now = time.time()
    locals_now = set(get_local_subnets())
    if not locals_now:
        return False

    changed = False
    removals = []
    with routing_table_lock:
        if _last_resync_locals is not None:
            for subnet, (dist, nh, _) in list(routing_table.items()):
                if (
                    dist == 0
                    and subnet in _last_resync_locals
                    and subnet not in locals_now
                ):
                    del routing_table[subnet]
                    removals.append((subnet, None))
                    changed = True
                    print(f"[{MY_IP}] lost direct {subnet}", flush=True)

        for subnet in locals_now:
            if subnet not in routing_table:
                routing_table[subnet] = [0, "0.0.0.0", now]
                changed = True
            else:
                d, prev_nh, _ = routing_table[subnet]
                if d != 0:
                    routing_table[subnet] = [0, "0.0.0.0", now]
                    removals.append((subnet, prev_nh))
                    changed = True
                else:
                    routing_table[subnet][2] = now

        _last_resync_locals = set(locals_now)

    for subnet, nh in removals:
        remove_route(subnet, nh)
    return changed


def build_routes_for_neighbor(neighbor_ip):
    neighbor_ip = _normalize_ip(neighbor_ip)
    routes = []
    with routing_table_lock:
        for subnet, (distance, next_hop, _) in routing_table.items():
            nh = _normalize_ip(next_hop)
            d = int(distance) if distance != METRIC_INFINITY else METRIC_INFINITY
            if nh == neighbor_ip and distance > 0:
                routes.append({"subnet": subnet, "distance": METRIC_INFINITY})
            else:
                routes.append({"subnet": subnet, "distance": d})
    return routes


def send_updates_to_neighbors():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for neighbor in NEIGHBORS:
            sender_ip = get_local_ip_for_neighbor(neighbor)
            packet = {
                "router_id": sender_ip,
                "version": VERSION,
                "routes": build_routes_for_neighbor(neighbor),
            }
            data = json.dumps(packet).encode("utf-8")
            try:
                sock.sendto(data, (neighbor, PORT))
            except OSError as e:
                print(f"[{MY_IP}] send {neighbor}: {e}", flush=True)
    finally:
        sock.close()


def broadcast_updates():
    while True:
        trigger_event.wait(timeout=UPDATE_INTERVAL)
        trigger_event.clear()
        send_updates_to_neighbors()


def route_aging_loop():
    while True:
        changed = False
        if resync_local_subnets():
            changed = True
        now = time.time()
        expired = []
        gc_list = []
        with routing_table_lock:
            for subnet, (dist, nh, ts) in list(routing_table.items()):
                if dist == 0:
                    continue
                age = now - ts
                if dist < METRIC_INFINITY and age > ROUTE_TIMEOUT:
                    expired.append((subnet, _normalize_ip(nh)))
                    routing_table[subnet] = [METRIC_INFINITY, _normalize_ip(nh), now]
                    changed = True
                elif dist >= METRIC_INFINITY and age > GARBAGE_TIME:
                    gc_list.append(subnet)
                    changed = True
            for subnet in gc_list:
                routing_table.pop(subnet, None)

        for subnet, nh in expired:
            remove_route(subnet, nh)

        if changed:
            trigger_event.set()
        time.sleep(1)


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print(f"[{MY_IP}] listen udp/{PORT}", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(65535)
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        except Exception as e:
            print(f"[{MY_IP}] recv err: {e}", flush=True)
            continue

        if not _version_ok(packet.get("version")):
            continue

        routes = packet.get("routes")
        if not isinstance(routes, list):
            continue

        neighbor_ip = _normalize_ip(packet.get("router_id") or addr[0])
        if update_logic(neighbor_ip, routes):
            trigger_event.set()


def _advertised_metric(route):
    d = route.get("distance", METRIC_INFINITY)
    if isinstance(d, float) and d.is_integer():
        d = int(d)
    try:
        d = int(d)
    except (TypeError, ValueError):
        return METRIC_INFINITY
    return METRIC_INFINITY if d >= METRIC_INFINITY else d


def update_logic(neighbor_ip, routes_from_neighbor):
    neighbor_ip = _normalize_ip(neighbor_ip)
    now = time.time()
    changed = False
    applies = []
    removals = []

    with routing_table_lock:
        for route in routes_from_neighbor:
            subnet = route.get("subnet")
            if not subnet:
                continue
            adv = _advertised_metric(route)
            new_dist = min(adv + 1, METRIC_INFINITY)

            cur = routing_table.get(subnet)
            if cur and cur[0] == 0:
                continue

            if cur is None:
                if new_dist < METRIC_INFINITY:
                    routing_table[subnet] = [new_dist, neighbor_ip, now]
                    applies.append((subnet, neighbor_ip))
                    changed = True
                continue

            cur_dist, cur_nh, _ = cur
            cur_nh = _normalize_ip(cur_nh)

            if cur_nh == neighbor_ip:
                if new_dist != cur_dist:
                    if new_dist >= METRIC_INFINITY:
                        removals.append((subnet, neighbor_ip))
                    routing_table[subnet] = [new_dist, neighbor_ip, now]
                    if new_dist < METRIC_INFINITY:
                        applies.append((subnet, neighbor_ip))
                    changed = True
                else:
                    routing_table[subnet][2] = now
            elif new_dist < cur_dist:
                if cur_dist < METRIC_INFINITY:
                    removals.append((subnet, cur_nh))
                routing_table[subnet] = [new_dist, neighbor_ip, now]
                applies.append((subnet, neighbor_ip))
                changed = True

    for subnet, nh in removals:
        remove_route(subnet, nh)
    for subnet, nh in applies:
        apply_route(subnet, nh)

    return changed


def print_routing_table():
    with routing_table_lock:
        print(f"\n[{MY_IP}] === table ===", flush=True)
        for subnet in sorted(routing_table):
            d, nh, _ = routing_table[subnet]
            ds = "INF" if d >= METRIC_INFINITY else str(d)
            print(f"  {subnet}  {ds}  {nh}", flush=True)
        print(flush=True)


if __name__ == "__main__":
    print(f"[{MY_IP}] DV start neighbors={NEIGHBORS}", flush=True)
    init_routing_table()
    print_routing_table()
    trigger_event.set()
    send_updates_to_neighbors()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=route_aging_loop, daemon=True).start()
    listen_for_updates()
