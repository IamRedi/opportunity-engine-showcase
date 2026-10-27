"""Build an allowlisted, secret-free Vercel release without committing the worktree."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(root=ROOT):
    target_parent = root / '.deploy'
    target_parent.mkdir(exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix='release-', dir=target_parent))
    candidates = list(root.glob('*.py'))
    candidates += [root/'requirements.txt', root/'vercel.json', root/'pyproject.toml']
    for directory, suffixes in (('templates', {'.html'}), ('static', {'.js','.css','.json','.svg','.woff2'})):
        candidates.extend(p for p in (root/directory).rglob('*') if p.is_file() and p.suffix in suffixes)
    manifest = {}
    for source in sorted(candidates):
        if source.is_symlink() or root.resolve() not in source.resolve().parents:
            raise RuntimeError('Symlinks and files outside the source tree cannot be deployed')
        rel = source.relative_to(root)
        dest = target/rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        manifest[str(rel)] = hashlib.sha256(dest.read_bytes()).hexdigest()
    # Project identity is nonsecret. Never copy .env, local DBs, uploads or credentials.
    identity = root/'.vercel/project.json'
    if identity.exists():
        (target/'.vercel').mkdir()
        shutil.copyfile(identity, target/'.vercel/project.json')
    (target/'.vercelignore').write_text('.vercel\n.env*\n')
    (target_parent/(target.name+'-manifest.json')).write_text(json.dumps(manifest, indent=2)+'\n')
    return target, len(manifest)


if __name__ == '__main__':
    path, count = prepare()
    print(f'Release: {path}\nAllowlisted source files: {count}')
