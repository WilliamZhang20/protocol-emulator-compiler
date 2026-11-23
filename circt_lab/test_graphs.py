#!/usr/bin/env python3
"""Exercise typed graph verification, placement, routes and port schedules."""
import argparse
import json
import itertools
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def run(*argv, ok=True):
    p = subprocess.run([str(x) for x in argv], text=True, capture_output=True)
    if (p.returncode == 0) != ok:
        raise AssertionError(f'{argv}: return {p.returncode}\n{p.stdout}\n{p.stderr}')
    return p


def graph(nodes, edges=''):
    return f'module {{ "spatial.workload_graph"() {{nodes = [{nodes}], edges = [{edges}]}} : () -> () }}\n'


def target(tiles, links=''):
    return f'module {{ "spatial.target_graph"() {{tiles = [{tiles}], links = [{links}]}} : () -> () }}\n'


def node(name, kind, op, ins, outs, extra=''):
    return (f'{{id = "{name}", resource = "{kind}", opcode = "{op}", '
            f'inputs = [{ins}], outputs = [{outs}], duration = 1 : i64{extra}}}')


def tile(name, kind, operations, width, extra=''):
    return (f'{{id = "{name}", resource = "{kind}", operations = [{operations}], '
            f'width = {width} : i64, slots = 1 : i64, latency = 1 : i64, interval = 1 : i64{extra}}}')


def edge(a, b, mode='data'):
    return f'{{from = "{a}", source_port = 0 : i64, to = "{b}", target_port = 0 : i64, mode = "{mode}"}}'


def link(a, b, width, latency):
    return f'{{from = "{a}", to = "{b}", width = {width} : i64, latency = {latency} : i64}}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mapper', required=True)
    ap.add_argument('--frontend', required=True)
    ap.add_argument('--fixture', type=Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='spatial-graph-') as temp:
        d = Path(temp)
        frontend = d / 'source.mlir'
        frontend.write_text(run(args.frontend, 'frontend-mlir', HERE / 'examples/memory_spi.spt').stdout)
        work = d / 'work.mlir'
        work.write_text(run(args.mapper, '--memory-spi-graph', frontend).stdout)
        g = work.read_text()
        assert 'outputs = [i12, i1]' in g and 'pack_big_endian_low_bits' in g
        assert 'half_period = 8 : i64' in g and g.count('mode = "feedback"') == 2
        image = d / 'source.mem'
        run(args.mapper, '--compile-memory-spi', frontend, '2652', image)
        words = [int(x, 16) for x in image.read_text().splitlines()]
        assert (((0x0a << 8) | 0x5c) & ((1 << 12) - 1)) << 4 == words[18]
        match = re.search(r'control_rows = \[(.*?)\]', g)
        assert match
        assert [int(x) for x in re.findall(r'(\d+) : i64', match.group(1))] == words[2:18]
        if args.fixture:
            assert image.read_bytes() == args.fixture.read_bytes()
        candidate = HERE / 'candidates/memory-spi-graph.mlir'
        report = json.loads(run(args.mapper, '--map-graph', candidate, work).stdout)
        report_file = d / 'report.json'
        report_file.write_text(json.dumps(report))
        rendered = run(sys.executable, HERE / 'show_graph.py', report_file).stdout
        assert '[bank1] memory i8: read_hi, read_lo, write_hi, write_lo' in rendered
        assert '[bank0] memory i8: (unused)' in rendered
        assert 'not hardware utilization' in rendered and 'Not RTL timing' in rendered
        run(sys.executable, HERE / 'show_graph.py', candidate, ok=False)
        mapping = {x['node']: x['tile'] for x in report['assignments']}
        assert len(set(mapping[x] for x in ('read_hi', 'read_lo', 'write_hi', 'write_lo'))) == 1
        assert mapping['read_hi'] == 'bank1'  # The standalone RTL test selects bank 1.
        assert len(set(mapping[x] for x in ('cs', 'sck', 'mosi'))) == 3
        assert not report['rtl_fidelity_verified'] and not report['image_emitted']
        assert report['nominal_pad_frequency_checked']
        assert report['memory_layout_origin'] == 'rtl-test-driver-not-source'
        schedule = {x['node']: x for x in report['schedule']}
        assert schedule['read_hi']['finish'] <= schedule['read_lo']['start']
        assert schedule['write_hi']['finish'] <= schedule['write_lo']['start']
        assert [schedule[x]['address'] for x in ('read_hi','read_lo','write_hi','write_lo')] == [100, 101, 200, 201]
        assert any(x['from']=='shift' and x['to']=='timer' and x['mode']=='feedback' for x in report['routes'])
        # Source-derived types and timing change with the actual source.
        variant = d / 'variant.spt'
        variant.write_text((HERE / 'examples/memory_spi.spt').read_text().replace('data(12, msb_first)', 'data(9, msb_first)').replace('every: 8 cycles', 'every: 10 cycles'))
        variant_front = d / 'variant.mlir'
        variant_front.write_text(run(args.frontend, 'frontend-mlir', variant).stdout)
        variant_graph = run(args.mapper, '--memory-spi-graph', variant_front).stdout
        assert 'outputs = [i9, i1]' in variant_graph and 'half_period = 10 : i64' in variant_graph
        variant_work = d / 'variant-work.mlir'; variant_work.write_text(variant_graph)
        variant_report = json.loads(run(args.mapper, '--map-graph', candidate, variant_work).stdout)
        assert any(x['from']=='pack' and x['to']=='shift' and x['width']==9 for x in variant_report['routes'])
        # Type mismatch is rejected by the registered MLIR verifier.
        bad = d / 'bad.mlir'
        bad.write_text(g.replace('inputs = [i8, i8], outputs = [i12, i1]', 'inputs = [i12, i8], outputs = [i12, i1]', 1))
        assert bad.read_text() != g
        run(args.mapper, '--verify', bad, ok=False)
        # The first bank is unreachable: bounded placement must backtrack.
        alt = d / 'alternate.mlir'
        lines = candidate.read_text().splitlines()
        alt.write_text('\n'.join(line for line in lines if not ('from = "bank1"' in line or 'to = "bank1"' in line)) + '\n')
        alt_report = json.loads(run(args.mapper, '--map-graph', alt, work).stdout)
        assert next(x['tile'] for x in alt_report['assignments'] if x['node']=='read_hi') == 'bank0'
        # Too-narrow shifter, missing memory capacity and an unsupported opcode fail.
        for old, new in [('resource = "shifter", operations = ["shift_msb_first"], width = 16', 'resource = "shifter", operations = ["shift_msb_first"], width = 8'),
                         ('depth = 1024', 'depth = 100'),
                         ('"shift_msb_first"', '"unsupported"'),
                         ('states = 4 : i64', 'states = 1 : i64'),
                         ('control_inputs = 2 : i64', 'control_inputs = 1 : i64'),
                         ('period_min = 8 : i64', 'period_min = 9 : i64'),
                         ('pad_max_output_hz_estimate = 66000000', 'pad_max_output_hz_estimate = 1000000')]:
            text = candidate.read_text()
            assert old in text
            alt.write_text(text.replace(old, new))
            run(args.mapper, '--map-graph', alt, work, ok=False)
        # Independent requests to one single-port bank serialize. Two distinct
        # exclusive banks can serve the same pair concurrently.
        a = node('a', 'memory', 'read', '', 'i8', ', address = 0 : i64')
        b = node('b', 'memory', 'read', '', 'i8', ', address = 1 : i64')
        simple = d / 'simple.mlir'; simple.write_text(graph(a + ', ' + b))
        bank0 = tile('b0', 'memory', '"read"', 8, ', depth = 16 : i64')
        bank1 = tile('b1', 'memory', '"read"', 8, ', depth = 16 : i64')
        one = d / 'one.mlir'; one.write_text(target(bank0))
        r = json.loads(run(args.mapper, '--map-graph', one, simple).stdout)
        assert sorted(x['start'] for x in r['schedule']) == [0,1] and r['resource_wait_cycles']==1
        # Result latency and port initiation interval are separate: a
        # pipelined two-cycle bank can still accept a request every cycle.
        one.write_text(target(bank0.replace('latency = 1 : i64', 'latency = 2 : i64')))
        r = json.loads(run(args.mapper, '--map-graph', one, simple).stdout)
        assert [(x['start'], x['finish']) for x in r['schedule']] == [(0,2),(1,3)]
        one.write_text(target(bank0.replace('latency = 1 : i64', 'latency = 2 : i64').replace('interval = 1 : i64', 'interval = 2 : i64')))
        r = json.loads(run(args.mapper, '--map-graph', one, simple).stdout)
        assert [(x['start'], x['finish']) for x in r['schedule']] == [(0,2),(2,4)]
        one.write_text(target(bank0))
        both = d / 'both.mlir'; both.write_text(target(bank0 + ', ' + bank1))
        simple.write_text(graph(a.replace('address =', 'exclusive = true, address =') + ', ' + b.replace('address =', 'exclusive = true, address =')))
        r = json.loads(run(args.mapper, '--map-graph', both, simple).stdout)
        assert [x['start'] for x in r['schedule']] == [0,0] and r['resource_wait_cycles']==0
        run(args.mapper, '--map-graph', one, simple, ok=False)
        # Directed, width-qualified links and explicit relay paths.
        p = node('p','producer','produce','','i12')
        c = node('c','consumer','consume','i12','')
        simple.write_text(graph(p+', '+c,edge('p','c')))
        pt = tile('p0','producer','"produce"',12)
        ct = tile('c0','consumer','"consume"',12)
        rt = tile('relay','route','"relay"',12)
        one.write_text(target(pt+', '+ct,link('p0','c0',8,1)))
        run(args.mapper, '--map-graph', one, simple, ok=False)
        one.write_text(target(pt+', '+ct+', '+rt,link('p0','relay',12,1)+', '+link('relay','c0',12,2)))
        r = json.loads(run(args.mapper, '--map-graph', one, simple).stdout)
        assert r['routes'][0]['tiles']==['p0','relay','c0'] and r['routes'][0]['latency']==3
        simple.write_text(graph(p+', '+c,edge('p','c')+', '+edge('p','c')))
        run(args.mapper, '--verify', simple, ok=False)
        # Unmarked cycles are rejected; explicit sequential feedback is routed
        # but is not treated as an acyclic scheduling dependency.
        pa = node('a','pipe','step','i1','i1')
        pb = node('b','pipe','step','i1','i1')
        pipe = tile('pipe0','pipe','"step"',1)
        one.write_text(target(pipe))
        simple.write_text(graph(pa+', '+pb,edge('a','b')+', '+edge('b','a')))
        run(args.mapper, '--map-graph', one, simple, ok=False)
        simple.write_text(graph(pa+', '+pb,edge('a','b')+', '+edge('b','a','feedback')))
        r = json.loads(run(args.mapper, '--map-graph', one, simple).stdout)
        assert any(x['mode']=='feedback' for x in r['routes'])
        # Independent exhaustive placement oracle on small directed graphs.
        # Ordinary tiles may be time-shared; an exclusive node reserves its
        # tile. With no route tiles, a data edge needs a direct link or local
        # placement. This checks feasibility, not schedule optimality.
        rng = random.Random(72593)
        fits = rejects = 0
        for case in range(60):
            count = rng.randint(2, 4); tile_count = rng.randint(1, 3)
            arcs = [(a,b) for a in range(count) for b in range(a+1,count) if rng.random()<0.45]
            links_set = {(a,b) for a in range(tile_count) for b in range(tile_count) if a!=b and rng.random()<0.45}
            exclusive = [rng.random()<0.28 for _ in range(count)]
            group = ['g' if rng.random()<0.35 else '' for _ in range(count)]
            def valid(assignment):
                for i in range(count):
                    for j in range(i):
                        if group[i] and group[i]==group[j] and assignment[i]!=assignment[j]: return False
                        if assignment[i]==assignment[j] and (exclusive[i] or exclusive[j]): return False
                return all(assignment[a]==assignment[b] or (assignment[a],assignment[b]) in links_set for a,b in arcs)
            expected = any(valid(choice) for choice in itertools.product(range(tile_count), repeat=count))
            graph_nodes=[]; graph_edges=[]
            for n in range(count):
                incoming = [a for a,b in arcs if b==n]
                extra = (', exclusive = true' if exclusive[n] else '') + (', group = "g"' if group[n] else '')
                graph_nodes.append(node(f'n{n}','compute','step',', '.join('i1' for _ in incoming),'i1',extra))
                for port,a in enumerate(incoming):
                    graph_edges.append(f'{{from = "n{a}", source_port = 0 : i64, to = "n{n}", target_port = {port} : i64, mode = "data"}}')
            simple.write_text(graph(', '.join(graph_nodes), ', '.join(graph_edges)))
            all_tiles = [tile(f't{i}','compute','"step"',1) for i in range(tile_count)]
            all_links = [link(f't{a}',f't{b}',1,1) for a,b in sorted(links_set)]
            one.write_text(target(', '.join(all_tiles), ', '.join(all_links)))
            result = run(args.mapper, '--map-graph', one, simple, ok=expected)
            if expected:
                assigned = {x['node']: int(x['tile'][1:]) for x in json.loads(result.stdout)['assignments']}
                assert valid(tuple(assigned[f'n{i}'] for i in range(count)))
                fits += 1
            else:
                rejects += 1
        assert fits and rejects
    print(f'PASS: typed source graph, contention, alternative routes, and 60 exhaustive placement comparisons ({fits} fit, {rejects} reject)')


if __name__ == '__main__':
    main()
