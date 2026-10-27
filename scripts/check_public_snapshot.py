"""Inspect the exact tracked publication surface without printing sensitive values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout
paths = [Path(value.decode('utf-8')) for value in tracked.split(b'\0') if value]
issues = []
private_context = re.compile(r'opportunity-engine-pilot-alpha|iamredis-projects|\bdpl_[A-Za-z0-9]+|\bprj_[A-Za-z0-9]+|Pilot Redion|Fitore|/home/redion|C:[/\\]Users[/\\]Lenovo|HYRJA_REDION|HYRJA_SHOQJA', re.I)
for path in paths:
    name = path.as_posix()
    if any(part in ('.private', '.deploy', '.demo', 'uploads', 'backups') for part in path.parts):
        issues.append((name, 'private/runtime directory tracked'))
    if path.name.startswith('.env') and path.name != '.env.example':
        issues.append((name, 'environment file tracked'))
    if path.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.pem', '.key'):
        issues.append((name, 'private data/key file tracked'))
    if path.suffix.lower() in ('.py', '.md', '.json', '.yml', '.yaml', '.html', '.txt', '.toml'):
        text = (ROOT / path).read_text(encoding='utf-8')
        # This scanner necessarily contains the patterns it checks for.
        if name != 'scripts/check_public_snapshot.py' and private_context.search(text):
            issues.append((name, 'private pilot or host metadata'))
for readme in ('README.md', 'README.en.md'):
    text = (ROOT / readme).read_text(encoding='utf-8')
    for link in re.findall(r'\]\(([^)]+)\)', text):
        if not link.startswith(('https://', 'http://', '#', 'mailto:')):
            target = link.split('#')[0]
            if target and not (ROOT / target).is_file():
                issues.append((readme, 'missing local link: ' + target))
for name, reason in issues:
    print(f'{name}: {reason}')
print(f'Publication surface: {len(paths)} tracked files; {len(issues)} issues')
raise SystemExit(bool(issues))
