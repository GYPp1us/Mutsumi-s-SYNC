"""Download original public assets to an ignored cache; never embed gold captions."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', default='benchmarks/v2/assets.json')
    parser.add_argument('--output', default='data/playground/assets')
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    assets = json.loads(Path(args.manifest).read_text(encoding='utf8'))
    for asset in assets:
        path = root / asset['cache_file']
        if not path.exists():
            request = urllib.request.Request(asset['url'], headers={'User-Agent': 'Mutsumi-Playground/0.3 (private model evaluation)'})
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError('Asset exceeds byte budget')
            path.write_bytes(data)
        asset['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        asset['bytes'] = path.stat().st_size
        print(asset['id'], asset['bytes'], asset['sha256'])
    (root / 'resolved.json').write_text(json.dumps(assets, ensure_ascii=False, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
