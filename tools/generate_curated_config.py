"""Optional: update ONLY research keywords and fetch cap in existing config.json.

Usage on repository root: python tools/generate_curated_config.py [--apply]
Preview by default. Output also packaged as a ready-to-upload config.json.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

KEYWORDS = [
    {
        'label': '磁隧道结、TMR与自旋阀', 'main': True, 'weight': 6,
        'terms': ['magnetic tunnel junction', 'magnetic tunnel junctions',
                  'tunnel magnetoresistance', 'tunneling magnetoresistance',
                  'perpendicular magnetic tunnel junction', 'spin-filter tunnel junction',
                  'spin valve', 'giant magnetoresistance', 'CoFeB/MgO',
                  'spin-dependent tunneling', 'double-barrier magnetic tunnel junction'],
    },
    {
        'label': 'STT/SOT-MRAM与自旋存储', 'main': True, 'weight': 6,
        'terms': ['STT-MRAM', 'SOT-MRAM', 'MRAM', 'magnetoresistive random access memory',
                  'spin-transfer torque', 'spin transfer torque',
                  'spin-transfer switching', 'spin torque switching',
                  'spintronic memory', 'spintronic storage',
                  'voltage-controlled MRAM'],
    },
    {
        'label': '自旋轨道力矩与轨道霍尔效应', 'main': True, 'weight': 6,
        'terms': ['spin-orbit torque', 'spin orbit torque', 'spin Hall effect',
                  'spin Hall angle', 'spin-orbit torque switching', 'SOT switching',
                  'field-free spin-orbit torque switching', 'orbital Hall effect',
                  'orbital torque', 'orbital-to-spin conversion',
                  'spin-orbit torque efficiency'],
    },
    {
        'label': '交换偏置与界面交换耦合', 'main': True, 'weight': 6,
        'terms': ['exchange bias', 'exchange-bias', 'exchange biased',
                  'exchange anisotropy', 'interfacial exchange coupling',
                  'interlayer exchange coupling', 'exchange spring', 'exchange-spring',
                  'antiferromagnetic pinning', 'RKKY coupling',
                  'unidirectional anisotropy', 'exchange bias field'],
    },
    {
        'label': '反铁磁、亚铁磁与交替磁自旋电子学', 'main': True, 'weight': 5,
        'terms': ['antiferromagnetic spintronics', 'antiferromagnetic switching',
                  'antiferromagnetic memory', 'synthetic antiferromagnet',
                  'synthetic antiferromagnetic', 'compensated ferrimagnet',
                  'ferrimagnetic switching', 'Néel vector', 'Neel vector',
                  'altermagnet', 'altermagnetism', 'antiferromagnetic domain wall',
                  'Néel order'],
    },
    {
        'label': '磁斯格明子与手性磁结构', 'main': True, 'weight': 5,
        'terms': ['magnetic skyrmion', 'skyrmions', 'skyrmion',
                  'skyrmion motion', 'skyrmion nucleation',
                  'Dzyaloshinskii-Moriya interaction', 'interfacial DMI',
                  'chiral domain wall', 'domain-wall motion', 'domain wall motion',
                  'chiral magnetic texture', 'topological Hall effect'],
    },
    {
        'label': '磁振子、磁子结与自旋波器件', 'main': True, 'weight': 4,
        'terms': ['magnonics', 'magnonic', 'magnon', 'magnon transport',
                  'magnon transistor', 'magnon valve', 'magnon junction',
                  'spin-wave logic', 'spin-wave device', 'spin wave', 'spin waves',
                  'spin Seebeck effect', 'magnon spin current'],
    },
    {
        'label': '垂直磁各向异性与电压调控', 'main': True, 'weight': 4,
        'terms': ['perpendicular magnetic anisotropy', 'interfacial perpendicular anisotropy',
                  'voltage-controlled magnetic anisotropy', 'VCMA',
                  'voltage-induced magnetization switching',
                  'magnetoelectric switching', 'electric-field-controlled magnetization',
                  'interface magnetic anisotropy'],
    },
    {
        'label': '自旋流、自旋注入与转换', 'main': True, 'weight': 4,
        'terms': ['spin current', 'spin transport', 'spin injection',
                  'spin pumping', 'spin mixing conductance',
                  'spin-to-charge conversion', 'charge-to-spin conversion',
                  'spin Hall magnetoresistance', 'inverse spin Hall effect',
                  'spin accumulation', 'spin filtering'],
    },
    {
        'label': '自旋存算一体与神经形态器件', 'main': False, 'weight': 4,
        'terms': ['spintronic in-memory computing', 'spintronic computing in memory',
                  'magnetic in-memory computing', 'spintronic neural network',
                  'spintronic synapse', 'spintronic neuron',
                  'magnetic synapse', 'probabilistic bit', 'p-bit',
                  'stochastic magnetic tunnel junction', 'spintronic logic'],
    },
    {
        'label': '二维磁体与拓扑自旋电子学', 'main': False, 'weight': 3,
        'terms': ['two-dimensional magnet', '2D magnet',
                  'van der Waals magnet', 'van der Waals ferromagnet',
                  'magnetic van der Waals heterostructure',
                  'magnetic topological insulator', 'quantum anomalous Hall',
                  'topological spintronics', 'spin-momentum locking'],
    },
]


def modified_config(original: dict) -> dict:
    import copy
    cfg = copy.deepcopy(original)
    # Do not overwrite user-curated research groups when this optional tool runs.
    cfg['keywords'] = copy.deepcopy(original.get('keywords') or KEYWORDS)
    cfg['window']['max_items_per_journal'] = max(200, int(cfg['window'].get('max_items_per_journal', 100)))
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, default=ROOT / 'config.json')
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    original = json.loads(args.config.read_text(encoding='utf-8'))
    proposed = modified_config(original)
    print('Proposed:', len(original.get('keywords', [])), 'groups ->', len(proposed['keywords']),
          'groups; fetch limit:', original['window']['max_items_per_journal'], '->', proposed['window']['max_items_per_journal'])
    print('Journals unchanged:', len(proposed['journals']))
    if args.apply:
        args.config.write_text(json.dumps(proposed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('config.json updated')
    else:
        print('Preview only. Add --apply to update config.json.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
