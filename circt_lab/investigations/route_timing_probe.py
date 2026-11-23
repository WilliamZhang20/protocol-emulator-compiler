#!/usr/bin/env python3
"""Tiny, explicit pipeline thought experiment; not protocol timing closure."""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent.parent


def period(frontend, file, timer):
    data = json.loads(subprocess.run([frontend, 'model-ir', str(file)], text=True, capture_output=True, check=True).stdout)
    item = next(x for x in data['items'] if x.get('kind')=='resource' and x.get('name')==timer and x.get('resource_kind')=='timer')
    values = {k:v for k,v in item['timings']}
    v = values['every']
    if v['status']!='known' or v['denominator']!='1':
        raise ValueError('probe requires a known integer timer period')
    return int(v['numerator'])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--frontend',required=True);args=p.parse_args()
    fast=period(args.frontend, HERE.parent/'examples/ethernet_tx_coded.spt','half_bit')
    i2c=period(args.frontend, HERE.parent/'examples/i2c.spt','byte_clock')
    # Hypothetical registered pipelines accepting one event/cycle. An input
    # before edge 0 is observed after stage N at edge N-1 and consumed by a
    # downstream synchronous register at edge N.
    pulses = list(range(0,fast*6,fast))
    routes=[]
    for stages in (1,2,3):
        arrivals=[tick+stages for tick in pulses]
        routes.append({'registered_route_stages':stages,'source_edges':pulses,'consumer_edges':arrivals,
                       'minimum_arrival_spacing':min(b-a for a,b in zip(arrivals,arrivals[1:])),
                       'assumed_stage_initiation_interval':1})
    stretch=[]
    for stages in (0,1,2):
        # 2 synchronizer stages plus hypothetical registered route stages;
        # result is measured from the first sampling edge to consumer edge.
        stretch.append({'registered_route_stages':stages,'assumed_synchronizer_stages':2,
                        'consumer_sampling_edge_after_first_sampling_edge':2+stages})
    print(json.dumps({'model':'registered-route-cycle-probe-v1',
       'source_values':{'ethernet_half_bit_period_cycles':fast,'i2c_half_period_cycles':i2c},
       'ethernet':routes,'i2c_stretch':stretch,
       'limitations':['Hypothetical pipelines with initiation interval one, no selector conflict or resource stall.',
          'Input level must remain stable until sampled; asynchronous phase adds up to one cycle.',
          'No output-edge deadline or protocol setup/hold window is established.',
          'Does not model feedback recurrence, controller, I/O cell, routing delay in nanoseconds, or RTL implementation.',
          'Preserved event spacing alone does not prove a two-cycle Ethernet path can be implemented.',
          'I2C SCL electrical behavior and acceptable stretch response are not verified.']},indent=2))


if __name__=='__main__':
    main()
