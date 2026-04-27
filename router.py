"""
Distance-vector router for Docker eval: multi-interface kernel routes,
per-neighbor router_id, 1s resync/expire/send pump + triggered sends.
"""
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
ROUTE_TIMEOUT = int(os.getenv("ROUTE_TIMEOUT", "12"))
if ROUTE_TIMEOUT >= 30:
    ROUTE_TIMEOUT = 22

routing_table_lock = threading.Lock()
send_lock = threading.Lock()
routing_table = {}
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
    out = []
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.strip().split("\n"):
            parts = line.split()
            for i, part in enumerate(parts):
                if part != "inet":
                    continue
                cidr = parts[i + 1]
                if cidr.startswith("127."):
                    continue
                try:
                    out.append(str(ipaddress.ip_interface(cidr).network))
                except ValueError:
                    continue
    except Exception as e:
        print(f"[{MY_IP}] get_local_subnets: {e}", flush=True)
    return out


def get_iface_for_next_hop(next_hop):
    if not next_hop or next_hop == "0.0.0.0":
        return None
    try:
        nh = ipaddress.ip_address(_normalize_ip(next_hop))
    except ValueError:
        return None
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) < 4 or parts[1] == "lo":
                continue
            for i, part in enumerate(parts):
                if part != "inet":
                    continue
                try:
                    net = ipaddress.ip_interface(parts[i + 1]).network
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
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.strip().split("\n"):
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


def _ip_route(args):
    return subprocess.run(args, capture_output=True, text=True)


def apply_route(subnet, next_hop):
    if not next_hop or next_hop == "0.0.0.0":
        return False
    nh = _normalize_ip(next_hop)
    err = ""
    for _ in range(4):
        iface = get_iface_for_next_hop(nh)
        trials = []
        if iface:
            trials.append(["ip", "route", "replace", subnet, "via", nh, "dev", iface])
            trials.append(
                ["ip", "route", "replace", subnet, "via", nh, "dev", iface, "onlink"]
            )
        trials.append(["ip", "route", "replace", subnet, "via", nh])
        for cmd in trials:
            p = _ip_route(cmd)
            if p.returncode == 0:
                return True
            err = (p.stderr or "").strip()
        time.sleep(0.08)
    print(f"[{MY_IP}] apply fail {subnet} via {nh}: {err}", flush=True)
    return False


def remove_route(subnet, next_hop=None):
    nh = _normalize_ip(next_hop) if next_hop else None
    trials = []
    if nh:
        iface = get_iface_for_next_hop(nh)
        if iface:
            trials.append(["ip", "route", "del", subnet, "via", nh, "dev", iface])
        trials.append(["ip", "route", "del", subnet, "via", nh])
    trials.append(["ip", "route", "del", subnet])
    for cmd in trials:
        if _ip_route(cmd).returncode == 0:
            return


def init_routing_table():
    for _ in range(20):
        loc = get_local_subnets()
        if loc:
            now = time.time()
            with routing_table_lock:
                for s in loc:
                    routing_table[s] = [0, "0.0.0.0", now]
            print(f"[{MY_IP}] locals={loc} neigh={NEIGHBORS}", flush=True)
            return
        time.sleep(0.25)
    print(f"[{MY_IP}] WARNING: no local subnets after wait", flush=True)


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
            for subnet, (dist, _nh, _) in list(routing_table.items()):
                if (
                    dist == 0
                    and subnet in _last_resync_locals
                    and subnet not in locals_now
                ):
                    routing_table[subnet] = [METRIC_INFINITY, "0.0.0.0", now]
                    removals.append((subnet, None))
                    changed = True
                    print(f"[{MY_IP}] withdrew local {subnet}", flush=True)

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


def expire_routes():
    now = time.time()
    expired = []
    with routing_table_lock:
        for subnet, (dist, nh, ts) in list(routing_table.items()):
            if dist == 0 or dist >= METRIC_INFINITY:
                continue
            if now - ts > ROUTE_TIMEOUT:
                nh_n = _normalize_ip(nh)
                routing_table[subnet] = [METRIC_INFINITY, nh_n, now]
                expired.append((subnet, nh_n))
    for subnet, nh in expired:
        remove_route(subnet, nh)


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
            pkt = {
                "router_id": get_local_ip_for_neighbor(neighbor),
                "version": VERSION,
                "routes": build_routes_for_neighbor(neighbor),
            }
            data = json.dumps(pkt).encode("utf-8")
            try:
                sock.sendto(data, (neighbor, PORT))
            except OSError as e:
                print(f"[{MY_IP}] send {neighbor}: {e}", flush=True)
    finally:
        sock.close()


def pump_loop():
    while True:
        resync_local_subnets()
        expire_routes()
        with send_lock:
            send_updates_to_neighbors()
        time.sleep(1)


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
            if subnet is None:
                continue
            subnet = str(subnet).strip()
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


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print(f"[{MY_IP}] listen {PORT}", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(65535)
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        except Exception as e:
            print(f"[{MY_IP}] recv: {e}", flush=True)
            continue

        if not _version_ok(packet.get("version")):
            continue
        routes = packet.get("routes")
        if not isinstance(routes, list):
            continue

        neighbor_ip = _normalize_ip(addr[0])
        if update_logic(neighbor_ip, routes):
            with send_lock:
                send_updates_to_neighbors()


def print_routing_table():
    with routing_table_lock:
        print(f"\n[{MY_IP}] === Routing Table ===", flush=True)
        for subnet in sorted(routing_table):
            d, nh, _ = routing_table[subnet]
            ds = "INF" if d >= METRIC_INFINITY else str(d)
            print(f"  {subnet:<20} {ds:<10} {nh}", flush=True)
        print(flush=True)


if __name__ == "__main__":
    print(f"[{MY_IP}] DV start neigh={NEIGHBORS}", flush=True)
    time.sleep(1.5)
    init_routing_table()
    print_routing_table()
    with send_lock:
        send_updates_to_neighbors()

    threading.Thread(target=pump_loop, daemon=True).start()
    listen_for_updates()
