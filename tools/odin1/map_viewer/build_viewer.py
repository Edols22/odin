#!/usr/bin/env python3
"""Assemble a portable offline viewer from the validated map export."""
import argparse
import base64
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('directory', type=Path)
parser.add_argument('bundle', type=Path)
args = parser.parse_args()
template = Path(__file__).with_name('template.html').read_text()
for key, filename in [('__POINTS__', 'points.f32'), ('__TRAJECTORY__', 'trajectory.f32')]:
    template = template.replace(key, base64.b64encode((args.directory / filename).read_bytes()).decode('ascii'))
template = template.replace('__METADATA__', (args.directory / 'metadata.json').read_text())
template = template.replace('__BUNDLE__', args.bundle.read_text().replace('</script', '<\\/script'))
(args.directory / 'index.html').write_text(template)
print(args.directory / 'index.html')
