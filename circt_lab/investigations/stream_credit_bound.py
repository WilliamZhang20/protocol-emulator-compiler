#!/usr/bin/env python3
"""Compute a conservative stream FIFO bound under explicit service assumptions.

This is a sizing aid, not an RTL timing proof or an unbounded-network deadlock
proof. Latency includes all queueing, request, memory and return stages.
"""
import argparse
import json


def assess(latency, byte_period, startup_burst, bank_clients, service_cycles, boundary_drain=0):
    if min(latency, byte_period, startup_burst, bank_clients, service_cycles) < 1:
        raise ValueError('all parameters must be positive')
    # The extra slot covers one byte consumed at the refill boundary. A burst
    # may demand more slots immediately and must be prefetched before launch.
    if boundary_drain < 0:
        raise ValueError('boundary drain must be nonnegative')
    effective_latency = latency + boundary_drain
    periodic = (effective_latency + byte_period - 1) // byte_period + 1
    depth = max(periodic, startup_burst)
    # Strict round-robin grants one request to each continuously requesting
    # client every N service slots. This is only a lower-bound guarantee;
    # phase, refresh and other masters must be included in latency/clients.
    guaranteed_interval = bank_clients * service_cycles
    return {'worst_refill_cycles': latency, 'boundary_drain_cycles': boundary_drain,
            'effective_refill_cycles': effective_latency, 'byte_period_cycles': byte_period,
            'startup_burst_bytes': startup_burst, 'periodic_depth': periodic,
            'required_depth': depth, 'guaranteed_grant_interval_cycles': guaranteed_interval,
            'sustained_rate_guaranteed': byte_period >= guaranteed_interval,
            'assumptions': ['bounded round-robin service with the declared number of clients',
                'ordered responses and a reserved slot per accepted read',
                'the stream is prefetched before its first non-stallable consumption',
                'after startup, at most one byte is consumed in each declared period',
                'no unmodeled downstream stalls, refresh, other requesters or longer wire latency',
                'the supplied period is a per-stream worst-case consumption interval'],
            'claims_deadlock_freedom': False, 'claims_physical_timing': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--latency', type=int, required=True)
    p.add_argument('--byte-period', type=int, required=True)
    p.add_argument('--startup-burst', type=int, default=1)
    p.add_argument('--bank-clients', type=int, default=3)
    p.add_argument('--service-cycles', type=int, default=1)
    p.add_argument('--boundary-drain', type=int, default=0)
    a = p.parse_args()
    try:
        print(json.dumps(assess(a.latency, a.byte_period, a.startup_burst, a.bank_clients, a.service_cycles, a.boundary_drain), indent=2))
    except ValueError as e:
        p.error(str(e))


if __name__ == '__main__':
    main()
