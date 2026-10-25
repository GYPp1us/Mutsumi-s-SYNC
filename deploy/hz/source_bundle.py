"""Bundle tracked V4 build inputs, including non-Rust embedded resources.

Usage: python deploy/hz/source_bundle.py data/hz-source.tar.gz
Private/ignored files and the frozen V3 tree are never selected.
"""
import argparse
from pathlib import Path
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.check_output([
        'git', '-c', f'safe.directory={root.as_posix()}', '-C', str(root),
        'ls-files', '-z',
    ]).decode('utf-8').split('\0')
    exact = {'Cargo.toml', 'Cargo.lock', 'system-prompts.yaml', 'quick-replies.yaml',
             'workers/md2svg/Cargo.toml', 'workers/md2svg/Cargo.lock'}
    prefixes = ('src/', 'assets/', 'deploy/hz/', 'workers/md2svg/src/')
    selected = [name for name in tracked if name and (
        name in exact or name.startswith(prefixes)
        or name.startswith('config/') and name.endswith('.example.yaml')
    )]
    for required in ['src/schema.sql', 'assets/trial.html', 'assets/trial.js',
                     'assets/playground-report.html', 'Cargo.lock']:
        if required not in selected:
            raise SystemExit(f'missing tracked build input: {required}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, 'w:gz') as bundle:
        for name in sorted(selected):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise SystemExit(f'build input must be a regular file: {name}')
            info = bundle.gettarinfo(path, arcname=name)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            with path.open('rb') as content:
                bundle.addfile(info, content)
    print(f'Bundled {len(selected)} tracked V4 files')


if __name__ == '__main__':
    main()
