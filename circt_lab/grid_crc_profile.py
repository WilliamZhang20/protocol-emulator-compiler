#!/usr/bin/env python3
"""Extract CRC parameters from verified frontend IR for the grid CRC tile.

This produces a partial configuration profile, not an executable protocol
image: no source control, datapath connections, placement or routing is lowered.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def integer(field, label):
    if not isinstance(field, dict) or field.get('kind') != 'integer':
        raise ValueError(f'{label} must be a literal integer in this probe')
    return int(field['value'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', required=True)
    parser.add_argument('--mapper', required=True)
    parser.add_argument('programs', nargs='+', type=Path)
    args = parser.parse_args()
    cmd = [sys.executable, str(HERE / 'investigate_workloads.py'), '--frontend', args.frontend,
           '--mapper', args.mapper, *map(str, args.programs)]
    try:
        report = json.loads(subprocess.run(cmd, check=True, text=True, capture_output=True).stdout)
        profiles = []
        for program in report['programs']:
            crcs = [x for x in program['resources'] if x['resource_kind'] == 'crc']
            if len(crcs) != 1:
                raise ValueError(f"{program['program']}: expected exactly one CRC resource")
            crc = crcs[0]
            fields = {x['name']: x['value'] for x in crc['fields']}
            width = integer(fields.get('width'), 'width')
            poly = integer(fields.get('poly'), 'poly')
            check = integer(fields.get('check'), 'check') if 'check' in fields else 0
            if not 1 <= width <= 32 or poly >= 1 << width or check >= 1 << width or min(poly, check) < 0:
                raise ValueError(f"{program['program']}: CRC parameters exceed tile width")
            start = fields.get('start')
            if start is None:
                init_ones = True  # Language default, matching src/elaborate.rs.
            elif start.get('kind') == 'name' and start.get('name') in ('ones', 'zeros'):
                init_ones = start['name'] == 'ones'
            else:
                raise ValueError(f"{program['program']}: unsupported CRC initialization")
            profiles.append({'program': Path(program['program']).name, 'resource': crc['name'],
                'width': width, 'polynomial': poly, 'check': check, 'init_ones': init_ones,
                'grid_words': {'69': poly & 0xffff, '70': poly >> 16,
                    '71': check & 0xffff, '72': check >> 16,
                    '75': (width - 1) | (0x20 if init_ones else 0)},
                'connections_lowered': False, 'protocol_compiled': False})
        print(json.dumps({'schema': 'spatial.grid-crc-profile.v1', 'profiles': profiles}, indent=2))
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
        print(f'grid CRC profile: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
