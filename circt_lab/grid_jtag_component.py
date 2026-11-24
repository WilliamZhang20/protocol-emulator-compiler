#!/usr/bin/env python3
"""Emit a restricted, source-checked eight-bit JTAG grid component image.

Fixed placement and routes adapt the grid SPI baseline. This does not lower
JTAG TAP control, the full source functions or dynamic scan lengths.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def expr(obj):
    if obj.get('kind') == 'name':
        return obj['name']
    if obj.get('kind') == 'field':
        return expr(obj['object']) + '.' + obj['field']
    raise ValueError('unsupported JTAG expression')


def frame_width(statement):
    if statement['op'] != 'set' or len(statement['fields']) != 1:
        raise ValueError('unsupported scan frame')
    field = statement['fields'][0]
    values = field['value'].get('values', [])
    if field['name'] != 'frame' or len(values) != 1:
        raise ValueError('unsupported scan frame')
    call = values[0]
    args = call.get('args', [])
    if call.get('kind') != 'call' or expr(call['callee']) != 'data' or len(args) != 1:
        raise ValueError('unsupported scan frame')
    value = args[0]['value']
    if args[0]['name'] is not None or value.get('kind') != 'integer':
        raise ValueError('unsupported scan width')
    return expr(statement['target']), int(value['value'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', required=True)
    parser.add_argument('--mapper', required=True)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('program', type=Path)
    args = parser.parse_args()
    cmd = [sys.executable, str(HERE/'investigate_workloads.py'), '--frontend', args.frontend,
           '--mapper', args.mapper, str(args.program)]
    try:
        p = json.loads(subprocess.run(cmd, text=True, capture_output=True, check=True).stdout)['programs'][0]
        if p['source_resource_counts'] != {'timer': 1, 'shifter': 2}:
            raise ValueError('requires one timer and two shifters')
        pins = {(x['name'], x['mode']) for x in p['pins']}
        if pins != {('tck','out'), ('tdi','out'), ('tms','out'), ('tdo','in')} or any(x['pads'] is not None for x in p['pins']):
            raise ValueError('unsupported pin declarations')
        expected = {
            ('tck','pin.drive'):'clock.out', ('tms','pin.drive'):'mode.out', ('tdi','pin.drive'):'data.out',
            ('clock','timer.start'):'data.busy', ('clock','timer.stop'):'data.done',
            ('data','shifter.start'):'go', ('data','shifter.shift'):'clock.fall',
            ('data','shifter.sample_data'):'tdo', ('data','shifter.sample_at'):'clock.rise',
            ('mode','shifter.start'):'go', ('mode','shifter.shift'):'clock.fall'}
        actual = {}
        for c in p['connections']:
            if c['context'] is not None or c['check']['status'] != 'valid':
                raise ValueError('unsupported connection or context')
            key = c['consumer'], c['port']
            if key in actual:
                raise ValueError('duplicate connection')
            actual[key] = expr(c['expression'])
        if actual != expected:
            raise ValueError('unsupported JTAG component connections')
        timer = next(x for x in p['resources'] if x['resource_kind']=='timer')
        shifters = {x['name']: x for x in p['resources'] if x['resource_kind']=='shifter'}
        if set(shifters) != {'data', 'mode'} or {f['name'] for f in shifters['data']['fields']} != {'start', 'shift', 'sample'} or {f['name'] for f in shifters['mode']['fields']} != {'start', 'shift'}:
            raise ValueError('unsupported shifter configuration')
        fields = {f['name']: f['value'] for f in timer['fields']}
        if set(fields) != {'every', 'idle', 'run'} or fields['idle'].get('value') != '0':
            raise ValueError('unsupported timer mode')
        timing = dict(timer['timings']).get('every', {})
        if timing.get('status') != 'known' or timing.get('denominator') != '1':
            raise ValueError('timer period must be integral')
        half = int(timing['numerator'])
        if not 4 <= half <= 65535:
            raise ValueError('timer period unsupported')
        scan = next(f for f in p['functions'] if f['name']=='scan8')['body']
        if len(scan) != 8 or [x['op'] for x in scan] != ['set','set','evaluate','evaluate','fire','wait','wait','reply']:
            raise ValueError('unsupported scan8 control body')
        if {frame_width(scan[0]), frame_width(scan[1])} != {('data',8),('mode',8)}:
            raise ValueError('scan8 requires two eight-bit frames')
        for item, target, arg in ((scan[2],'mode.load','tms'), (scan[3],'data.load','tdi')):
            call=item['expression']; call_args=call.get('args', [])
            if call.get('kind')!='call' or expr(call['callee'])!=target or len(call_args)!=1 or expr(call_args[0]['value'])!=arg:
                raise ValueError('unsupported scan8 payload loading')
        if scan[4]['event']!='go' or any(scan[i]['next'] for i in (5,6)) or [expr(scan[i]['signal']) for i in (5,6)]!=['data.done','mode.done'] or expr(scan[7]['value'])!='data.received':
            raise ValueError('unsupported scan8 completion')
        image = [int(x, 16) for x in args.base.read_text().splitlines()]
        if len(image)!=66 or image[65]!=0 or image[63]!=0x000a:
            raise ValueError('requires a matching grid SPI base image')
        def track(address, lane, source):
            image[address] = (image[address] & ~(7 << (3*lane))) | (source << (3*lane))
        image[0]=image[1]=half;image[2]=image[55]=8
        track(14,3,3);track(26,3,5);track(39,3,6)
        track(33,2,3);track(21,2,5)
        image[50]=12;image[59]=14;image[60]=21;image[63]=0x2005
        args.output.write_text(''.join(f'{word:04x}\n' for word in image))
        print(json.dumps({'target':'grid-3x3-jtag-scan8-component', 'source_validated':True,
            'general_mapping':False, 'full_source_program_compiled':False, 'full_tap_verified':False,
            'half_period':half, 'source_bytes':['TDI','TMS'], 'destination':'TDO',
            'limitations':'Fixed routes and placement; scan8 component only. Host loads two bytes; step, TAP control and exact source-cycle equivalence are not compiled.'}, indent=2))
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError, StopIteration) as error:
        print(f'grid JTAG component: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
