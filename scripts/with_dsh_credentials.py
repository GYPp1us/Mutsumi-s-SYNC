"""Run a child with the user-authorized dsh credential, without copying it to config."""
import argparse
import os
from pathlib import Path
import subprocess
import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', type=Path, default=Path.home() / '.dsh' / '.credentials.yaml')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error('a child command is required')
    values = yaml.safe_load(args.credentials.read_text(encoding='utf8'))
    key = values.get('COMMANDCODE_API_KEY')
    if not isinstance(key, str) or not key.strip():
        raise SystemExit('COMMANDCODE_API_KEY is not configured in the selected credential file')
    environment = os.environ.copy()
    environment['COMMANDCODE_API_KEY'] = key
    raise SystemExit(subprocess.run(args.command, env=environment, check=False).returncode)


if __name__ == '__main__':
    main()
