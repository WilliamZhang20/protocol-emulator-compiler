#!/usr/bin/env python3
"""Compare explicit abstract SRAM request traces with/without four-byte prefetch.

Illustrative counterfactual: two independent objects, not an RTL replay.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frontend', required=True)
    args = p.parse_args()
    source = Path(__file__).resolve().parents[2] / 'examples/ethernet_tx_coded.spt'
    source_model = json.loads(subprocess.run([args.frontend, 'model-ir', str(source)], text=True, capture_output=True, check=True).stdout)
    constants = {x['name']: x['value'] for x in source_model['items'] if x['kind']=='constant' and 'name' in x}
    frame_bytes = len(constants['FRAME']['values'])
    wire_bytes = len(constants['WIRE_FRAME']['values'])
    # UART 8N1 at 64 cycles/bit consumes one byte per 640 cycles.
    # Host-background requests are an explicitly assumed independent stream,
    # 26 cycles apart, phase chosen to coincide with the first actor deadline.
    byte_cycles, host_cycles, count = 640, 26, 16
    stop = count * byte_cycles
    candidates = [dict(name='one_1k_bank', capacity_bytes=1024, banks=1, stripe_bytes=1024,
                       ports_per_bank=1, latency=1, initiation_interval=1),
                  dict(name='two_1k_banks', capacity_bytes=2048, banks=2, stripe_bytes=1024,
                       ports_per_bank=1, latency=1, initiation_interval=1)]
    all_results = []
    for phase in range(host_cycles):
        results = []
        for prefetch in (False, True):
            reads = []
            for i in range(count):
                due = (i+1)*byte_cycles
                # At most four bytes are requested before the first consumption;
                # each subsequent request is released after a slot is consumed.
                release = 0 if prefetch and i < 4 else ((i-3)*byte_cycles if prefetch else due)
                reads.append(dict(id=f'z_actor_{i:02}', requester='uart_source', address=i,
                                  bytes=1, release=release, deadline=due+1))
            for candidate in candidates:
                host = [dict(id=f'a_host_{i:04}', requester='background_host',
                             address=(512 if candidate['banks']==1 else 1024)+i, bytes=1, release=t)
                        for i,t in enumerate(range(phase, stop+1, host_cycles))]
                request = {'requests': host+reads, 'candidates': [candidate]}
                with tempfile.TemporaryDirectory(prefix='spatial-memory-') as tmp:
                    path = Path(tmp)/'experiment.json'; path.write_text(json.dumps(request))
                    output = subprocess.run([args.frontend, 'model-memory', str(path)], text=True, capture_output=True, check=True)
                for result in json.loads(output.stdout)['candidates']:
                    access = {x['id']: x for x in result['accesses']}
                    reads_done = [access[r['id']] for r in reads]
                    # Bound occupancy of the assumed 4-byte queue at each completion,
                    # counting consumptions at their declared times; no value simulation.
                    max_occupancy = None
                    if prefetch:
                        events = [(x['completion'], 1) for x in reads_done] + [((i+1)*byte_cycles, -1) for i in range(count)]
                        occupancy = 0; max_occupancy = 0
                        for _, delta in sorted(events, key=lambda x:(x[0],x[1])):
                            occupancy += delta; max_occupancy=max(max_occupancy,occupancy)
                            if occupancy < 0:
                                raise RuntimeError('queue underflow in declared prefetch schedule')
                        if max_occupancy > 4:
                            raise RuntimeError(f'prefetch schedule exceeds four bytes: {max_occupancy}')
                    results.append({'host_phase':phase, 'candidate':result['name'], 'prefetch_depth':4 if prefetch else 0,
                        'actor_deadline_misses':sum(x['deadline_missed'] for x in reads_done),
                        'actor_wait_cycles':sum(x['start']-x['ready'] for x in reads_done),
                        'all_request_wait_cycles':result['total_queue_cycles'], 'max_prefetch_occupancy':max_occupancy})
        all_results.extend(results)
    results = [r for r in all_results if r['host_phase']==16]
    sweep = []
    for candidate in candidates:
        for depth in (0,4):
            subset=[r for r in all_results if r['candidate']==candidate['name'] and r['prefetch_depth']==depth]
            sweep.append({'candidate':candidate['name'], 'prefetch_depth':depth, 'phases':len(subset),
                'phases_with_misses':sum(r['actor_deadline_misses']>0 for r in subset),
                'worst_actor_deadline_misses':max(r['actor_deadline_misses'] for r in subset)})
    print(json.dumps({'model':'explicit-memory-request-counterfactual-v1',
        'assumptions':{'actor_object_bytes':count, 'actor_byte_period_cycles':byte_cycles,
          'actor_first_consumption_cycle':byte_cycles, 'actor_deadline_offset_cycles':1,
          'background_host_period_cycles':host_cycles, 'background_host_first_cycle':16,
          'background_object_is_independent':True, 'object_placement':'host base 512 in shared bank, base 1024 in two-bank candidate',
          'deterministic_priority':'lowest release cycle, then lexicographic ID; host wins simultaneous requests',
          'prefetch_initial_contents':'empty', 'memory_latency_and_initiation_cycles':1},
        'limitations':['Synthetic releases, no data-value validation or host protocol replay.',
          'Queue arrivals and consumption are modeled as deadlines, not a ready/valid RTL queue.',
          'Host and actor access disjoint objects; neither data sharing nor coherence is modeled.',
          'No pin constraints, dynamic arbitration fairness, area, physical timing or PPA.',
          'One bank provides 1 KiB total and two banks provide 2 KiB; this trace fits in either but larger workloads may not.'],
        'frame_capacity_probe':{'source':str(source), 'stored_frame_bytes':frame_bytes,
          'source_wire_frame_bytes_without_crc_tail':wire_bytes, 'sram_capacity_bytes':1024,
          'frame_fits_one_sram_bank':frame_bytes<=1024, 'local_queue_bytes':4,
          'frame_fits_entire_local_queue':frame_bytes<=4, 'minimum_queue_fills_if_streamed':math.ceil(frame_bytes/4),
          'limitation':'Capacity arithmetic only; source WIRE_FRAME excludes CRC tail, and queue refill timing, control, host pins and larger frames are not modeled.'},
        'results':results, 'phase_sweep':sweep},indent=2))


if __name__ == '__main__':
    main()
