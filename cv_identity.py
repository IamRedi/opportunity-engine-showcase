"""Conservative identities for repeated CV imports, without fuzzy name matching."""
import re


def identity(section, value):
    value = re.sub(r'\s+', ' ', value or '').strip().casefold()
    if section == 'skills':
        value = {'node': 'node.js', 'nodejs': 'node.js', 'reactjs': 'react',
                 'react.js': 'react', 'nextjs': 'next.js'}.get(value, value)
    if section in ('projects', 'portfolio'):
        value = value.split('|', 1)[0].strip()
    return value
