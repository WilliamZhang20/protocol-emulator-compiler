#!/usr/bin/env python3
"""Lower the checked UART 8N1 component into a typed experimental graph.

This is deliberately a narrow prototype: unsupported source changes fail closed.
The generated graph expresses resource demands, not a configuration image.
"""
import argparse
import json
import subprocess
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k != 'source_span'}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def field(resource, port):
    return {'kind': 'field', 'object': {'kind': 'name', 'name': resource}, 'field': port}


def name(value):
    return {'kind': 'name', 'name': value}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mapper', required=True, help='CIRCT mapper executable')
    p.add_argument('frontend', help='verified frontend MLIR')
    args = p.parse_args()
    try:
        result = subprocess.run([args.mapper, '--frontend-json', args.frontend], text=True, capture_output=True, check=True)
        source = json.loads(result.stdout)
        items = [clean(x) for x in source['items'] if x['kind'] != 'test']
        # Resolve roles from the source bindings, not declaration names.
        declarations = {x['name']: x for x in items if 'name' in x}
        streams = [x for x in items if x['kind'] == 'stream']
        require(len(streams) == 2, 'expected two UART streams')
        txs = [x for x in streams if x['from'] == name('host')]
        rxs = [x for x in streams if x['to'] == name('host')]
        require(len(txs) == len(rxs) == 1, 'expected host TX and RX streams')
        txstream, rxstream = txs[0], rxs[0]
        sender = txstream['to']['name']; receiver = rxstream['from']['name']
        txfields = {x['name']: x['value'] for x in declarations[sender]['fields']}
        rxfields = {x['name']: x['value'] for x in declarations[receiver]['fields']}
        txtimer = txfields['shift']['object']['name']; rxtimer = rxfields['shift']['object']['name']
        rxpin = rxfields['sample']['left']['name']
        pins = [x for x in items if x['kind'] == 'pin' and x['mode'] == 'out' and x['source'] == field(sender, 'out')]
        require(len(pins) == 1, 'expected one sender output pin')
        timerfields = {x['name']: x['value'] for x in declarations[txtimer]['fields']}
        constant = timerfields['every']['name']
        roles = {constant:'BIT', pins[0]['name']:'tx', rxpin:'rx', txtimer:'tx_bit', rxtimer:'rx_bit', sender:'sender', receiver:'receiver', txstream['name']:'send', rxstream['name']:'recv'}
        require(len(roles) == 9, 'UART roles must be distinct')
        def rename(v):
            if isinstance(v, list):
                return [rename(x) for x in v]
            if isinstance(v, dict):
                return {k: roles.get(x, x) if k == 'name' and isinstance(x, str) and v.get('kind') in ('name', 'constant', 'pin', 'resource', 'stream') else rename(x) for k, x in v.items()}
            return v
        items = rename(items)
        named = {x['name']: x for x in items if 'name' in x}
        require(len(named) == len([x for x in items if 'name' in x]), 'duplicate declarations')
        require(set(named) == {'BIT', 'tx', 'rx', 'tx_bit', 'rx_bit', 'sender', 'receiver', 'send', 'recv'}, 'unsupported UART declarations')
        require(sum(x['kind'] == 'clock' for x in items) == 1 and not source['protocols'], 'unsupported UART clock or protocol')
        require(source['clock_hz'].isdigit() and 0 < int(source['clock_hz']) <= 1000000000, 'unsupported clock')
        require(named['tx']['kind'] == 'pin' and named['tx']['mode'] == 'out' and named['tx']['pads'] is None and named['tx']['source'] == field('sender', 'out'), 'unsupported TX pin')
        require(named['rx']['kind'] == 'pin' and named['rx']['mode'] == 'in' and named['rx']['pads'] is None and named['rx']['source'] is None, 'unsupported RX pin')
        for n, start, stop in [('tx_bit', field('sender', 'busy'), field('sender', 'done')), ('rx_bit', field('rx', 'fall'), field('receiver', 'done'))]:
            t = named[n]
            require(t['kind'] == 'resource' and t['resource_kind'] == 'timer', 'expected UART timer')
            fs = {x['name']: x['value'] for x in t['fields']}
            require(len(fs) == len(t['fields']) and set(fs) == {'every', 'first', 'pulse', 'run'} and fs['pulse'] is None, 'unsupported timer fields')
            require(fs['every'] == name('BIT') and fs['run'] == {'kind': 'binary', 'op': 'until', 'left': start, 'right': stop}, 'unsupported timer run semantics')
            timings = {key: value for key, value in t['timings']}
            require(set(timings) == {'every', 'first'} and all(v['status'] == 'known' and v['denominator'] == '1' and v['numerator'].isdigit() for v in timings.values()), 'timer periods must be known integer cycles')
            t['_period'] = int(timings['every']['numerator']); t['_first'] = int(timings['first']['numerator'])
            require(1 <= t['_first'] <= t['_period'] <= 1000000, 'unsupported timer periods')
        require(named['tx_bit']['_period'] == named['rx_bit']['_period'], 'UART periods differ')
        def frame(v):
            return (v.get('kind') == 'list' and len(v.get('values', [])) == 3 and
                    v['values'][0] == {'kind': 'integer', 'value': '0'} and
                    v['values'][2] == {'kind': 'integer', 'value': '1'} and
                    v['values'][1] == {'kind': 'call', 'callee': name('data'), 'args': [{'name': None, 'value': {'kind': 'integer', 'value': '8'}}]})
        for n in ('sender', 'receiver'):
            s = named[n]
            require(s['kind'] == 'resource' and s['resource_kind'] == 'shifter', 'expected UART shifter')
            fs = {x['name']: x['value'] for x in s['fields']}
            require(len(fs) == len(s['fields']) and frame(fs.get('frame', {})), 'only 8N1 default LSB-first frame supported')
            if n == 'sender':
                require(set(fs) == {'frame', 'idle', 'shift'} and fs['idle'] == {'kind': 'integer', 'value': '1'} and fs['shift'] == field('tx_bit', 'tick'), 'unsupported sender')
            else:
                require(set(fs) == {'frame', 'start', 'shift', 'sample'} and fs['start'] == field('rx', 'fall') and fs['shift'] == field('rx_bit', 'tick') and fs['sample'] == {'kind': 'binary', 'op': 'at', 'left': name('rx'), 'right': field('rx_bit', 'tick')}, 'unsupported receiver')
        for n, a, b in [('send', 'host', 'sender'), ('recv', 'receiver', 'host')]:
            s = named[n]
            require(s['kind'] == 'stream' and s['from'] == name(a) and s['to'] == name(b) and s['element_width_bits'] == 8 and not s['packets'] and not s['stages'], 'unsupported stream')
        require(all(x['check']['status'] == 'valid' and x['context'] is None for x in source['connections']['inputs']), 'unsupported or contextual input')
        # Timer first-tick values are kept explicitly. Separate permanent tiles
        # conservatively require simultaneous TX/RX; host interfaces remain abstract.
        nodes = [
            ('host_tx','host','stream_source',[],['i8'],''),
            ('host_rx','host','stream_sink',['i8'],[],''),
            ('rx','input','input',[],['i1'],''),
            ('edge','edge','falling_edge',['i1'],['i1'],''),
            ('tx_timer','timer','uart_timer',['i1','i1'],['i1'],f", period = {named['tx_bit']['_period']} : i64, first = {named['tx_bit']['_first']} : i64"),
            ('rx_timer','timer','uart_timer',['i1','i1'],['i1'],f", period = {named['rx_bit']['_period']} : i64, first = {named['rx_bit']['_first']} : i64"),
            ('sender','shifter','uart_tx_8n1',['i8','i1'],['i1','i1','i1'],''),
            ('receiver','shifter','uart_rx_8n1',['i1','i1','i1'],['i8','i1'],''),
            ('tx','output','output',['i1'],[],''),
        ]
        edges = [('host_tx',0,'sender',0,'data'),('sender',0,'tx',0,'data'),('sender',1,'tx_timer',0,'feedback'),('sender',2,'tx_timer',1,'feedback'),('tx_timer',0,'sender',1,'data'),('rx',0,'edge',0,'data'),('edge',0,'rx_timer',0,'data'),('receiver',1,'rx_timer',1,'feedback'),('rx_timer',0,'receiver',0,'data'),('edge',0,'receiver',1,'data'),('rx',0,'receiver',2,'data'),('receiver',0,'host_rx',0,'data')]
        print('module {\n  "spatial.workload_graph"() {')
        print(f'    provenance = "uart-8n1-source-component-v1", analysis_only = true, clock_hz = {source["clock_hz"]} : i64,')
        print('    nodes = [')
        for i,(ident,resource,opcode,ins,outs,extra) in enumerate(nodes):
            print(f'      {{id = "{ident}", resource = "{resource}", opcode = "{opcode}", inputs = [{", ".join(ins)}], outputs = [{", ".join(outs)}], duration = 1 : i64, exclusive = true{extra}}}' + (',' if i+1<len(nodes) else ''))
        print('    ],\n    edges = [')
        for i,(a,ap,b,bp,mode) in enumerate(edges):
            print(f'      {{from = "{a}", source_port = {ap} : i64, to = "{b}", target_port = {bp} : i64, mode = "{mode}"}}' + (',' if i+1<len(edges) else ''))
        print('    ]\n  } : () -> ()\n}')
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f'UART lowering unsupported: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
