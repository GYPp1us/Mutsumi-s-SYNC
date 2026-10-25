"""HZ-specific trial preparation. Run as root; credentials arrive on stdin, never argv.

Does not start services. Existing V3 archives and conversation databases are untouched.
"""
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import time
import yaml

ROOT = Path('/opt/mutsumi-sync-v4')
SHARED = ROOT / 'shared'
NAPCAT = Path('/root/Napcat/opt/QQ/resources/app/app_launcher/napcat')


def write(path, text, mode=0o640, group=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(text, encoding='utf-8')
    tmp.chmod(mode)
    if group is not None:
        os.chown(tmp, 0, group)
    tmp.replace(path)


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    incoming = json.load(sys.stdin)
    key = incoming.get('commandcode_key')
    if key is not None and (not isinstance(key, str) or not key or '\n' in key or '\r' in key):
        raise SystemExit('invalid provider credential')
    try:
        account = pwd.getpwnam('mutsumi-v4')
    except KeyError:
        subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/mutsumi-v4', '--shell', '/usr/sbin/nologin', 'mutsumi-v4'], check=True)
        account = pwd.getpwnam('mutsumi-v4')
    SHARED.mkdir(parents=True, exist_ok=True)
    os.chown(SHARED, 0, account.pw_gid)
    SHARED.chmod(0o750)
    for path in [Path('/var/lib/mutsumi-v4'), SHARED / 'workspace']:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
        path.chmod(0o700)
    previous = yaml.safe_load(Path('/opt/mutsumi-sync-v3/shared/config.yaml').read_text())
    identity = previous['identity']
    bot = str(identity['bot_user_id'])
    owner = str(identity['owner_user_id'])
    if not bot.isdigit() or not owner.isdigit():
        raise SystemExit('invalid existing QQ identity')
    env_path = SHARED / 'service.env'
    values = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if '=' in line:
                name, value = line.split('=', 1)
                values[name] = value
    for name in ['MUTSUMI_WEB_ACCESS_KEY', 'MUTSUMI_ONEBOT_HOOK_KEY', 'MUTSUMI_NAPCAT_API_KEY', 'MUTSUMI_NAPCAT_WEB_KEY', 'MUTSUMI_HOOK_NEWS_KEY', 'MUTSUMI_HOOK_MARKET_KEY', 'MUTSUMI_HOOK_AGENT_KEY', 'MUTSUMI_HOOK_MAIL_KEY']:
        values.setdefault(name, secrets.token_urlsafe(32))
    if key is not None:
        values['COMMANDCODE_API_KEY'] = key
    if any('\n' in v or '\r' in v or '"' in v or '\\' in v for v in values.values()):
        raise SystemExit('credential requires unsupported environment escaping')
    write(env_path, ''.join(f'{k}={v}\n' for k, v in values.items()), 0o600)
    source = ROOT / 'current'
    runtime = yaml.safe_load((source / 'config/server-runtime.example.yaml').read_text())
    runtime.update(database='/var/lib/mutsumi-v4/events.sqlite3', prompts=str(source / 'system-prompts.yaml'), quick_replies=str(source / 'quick-replies.yaml'), profile=str(SHARED / 'profile.yaml'))
    runtime['workspace']['root'] = str(SHARED / 'workspace')
    runtime['media'].update(root='/var/lib/mutsumi-v4/media', worker_executable=str(source / 'bin/mutsumi-md-worker'), outbox='/var/lib/mutsumi-v4/unused-outbox')
    runtime['directory']['targets']['qq_owner'] = {'label': '主人 QQ 私聊', 'account': bot, 'audience': 'owner', 'transport': 'napcat'}
    write(SHARED / 'runtime.yaml', yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), group=account.pw_gid)
    server = yaml.safe_load((source / 'config/server.example.yaml').read_text())
    server['runtime_config'] = str(SHARED / 'runtime.yaml')
    server['napcat'] = {'base_url': 'http://127.0.0.1:3000', 'token_env': 'MUTSUMI_NAPCAT_API_KEY', 'webui_url': 'http://127.0.0.1:6099', 'webui_token_env': 'MUTSUMI_NAPCAT_WEB_KEY', 'bot_id': bot, 'owner_id': owner, 'targets': {'qq_owner': {'kind': 'private', 'id': owner}}}
    write(SHARED / 'server.yaml', yaml.safe_dump(server, allow_unicode=True, sort_keys=False), group=account.pw_gid)
    profile = yaml.safe_load((source / 'config/server-profile.example.yaml').read_text())
    profile['owner'].update(name=identity.get('owner_nickname', '主人'), qq_id=owner)
    if not (SHARED / 'profile.yaml').exists():
        write(SHARED / 'profile.yaml', yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), group=account.pw_gid)
    backup = SHARED / ('napcat-config-before-' + time.strftime('%Y%m%dT%H%M%S'))
    backup.mkdir(mode=0o700)
    network = {'httpServers': [{'enable': True, 'name': 'mutsumi-v4-http', 'host': '127.0.0.1', 'port': 3000, 'enableCors': False, 'enableWebsocket': False, 'messagePostFormat': 'array', 'token': values['MUTSUMI_NAPCAT_API_KEY'], 'debug': False}], 'httpSseServers': [], 'httpClients': [{'enable': True, 'name': 'mutsumi-v4-events', 'url': 'http://127.0.0.1:18240/onebot/events', 'messagePostFormat': 'array', 'reportSelfMessage': False, 'token': values['MUTSUMI_ONEBOT_HOOK_KEY'], 'debug': False}], 'websocketServers': [], 'websocketClients': [], 'plugins': []}
    for name in ['onebot11.json', f'onebot11_{bot}.json']:
        path = NAPCAT / 'config' / name
        if path.exists():
            shutil.copy2(path, backup / name)
        write(path, json.dumps({'network': network, 'musicSignUrl': '', 'enableLocalFile2Url': False, 'parseMultMsg': False}, indent=2), 0o600)
    web_path = NAPCAT / 'config/webui.json'
    shutil.copy2(web_path, backup / 'webui.json')
    web = json.loads(web_path.read_text())
    web.update(host='127.0.0.1', token=values['MUTSUMI_NAPCAT_WEB_KEY'])
    write(web_path, json.dumps(web, indent=2), 0o600)
    unit = f'''[Unit]
Description=NapCat for Mutsumi V4 trial
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=/root
Environment=HOME=/root
ExecStart=/usr/bin/xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox -q {bot}
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
'''
    write(Path('/etc/systemd/system/mutsumi-napcat.service'), unit, 0o644)
    write(Path('/etc/systemd/system/mutsumi-sync-v4.service'), (source / 'deploy/hz/mutsumi-sync-v4.service').read_text(), 0o644)
    old_cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
    write(backup / 'root-crontab.txt', old_cron, 0o600)
    new_cron = '\n'.join(line for line in old_cron.splitlines() if '/usr/local/bin/napcat_scheduler.sh' not in line) + '\n'
    subprocess.run(['crontab', '-'], input=new_cron, text=True, check=True)
    snippet = '''location = /sync { return 308 https://$host/sync/; }
location ^~ /sync/ {
    if ($scheme = http) { return 308 https://$host$request_uri; }
    client_max_body_size 24m;
    proxy_pass http://127.0.0.1:18240/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 190s;
}
'''
    write(Path('/etc/nginx/snippets/mutsumi-v4-location.conf'), snippet, 0o644)
    nginx_path = Path('/etc/nginx/sites-enabled/disk-cleanup-hz.conf').resolve()
    nginx = nginx_path.read_text()
    if 'include /etc/nginx/snippets/mutsumi-v4-location.conf;' not in nginx:
        import re
        changed, count = re.subn(r'(?m)^(\s*)location\s+/\s*\{', r'\1include /etc/nginx/snippets/mutsumi-v4-location.conf;\n\1location / {', nginx)
        if count not in (1, 2):
            raise SystemExit('unexpected HZ nginx layout')
        write(backup / 'nginx-hz.conf', nginx, 0o600)
        write(nginx_path, changed, 0o644)
    subprocess.run(['nginx', '-t'], check=True)
    write(SHARED / 'access.txt', 'https://hz.arcol.site/sync/#access=' + values['MUTSUMI_WEB_ACCESS_KEY'] + '\n', 0o600)
    print(json.dumps({'prepared': True, 'fresh_v4_database': not Path(runtime['database']).exists(), 'owner_bound': True, 'napcat_configuration_backed_up': True, 'access_file': str(SHARED / 'access.txt')}))


if __name__ == '__main__':
    main()
