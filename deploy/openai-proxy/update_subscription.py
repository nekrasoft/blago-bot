"""Update the selected VLESS/TCP/REALITY profile without replacing a working config on failure."""
from __future__ import annotations

import copy
import fcntl
import grp
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import httpx
from dotenv import dotenv_values, set_key

ENV_PATH = Path('/etc/blago/openai-proxy.env')
CONFIG_PATH = Path('/etc/blago/xray/config.json')
XRAY = '/usr/local/bin/xray'


def build_config(profiles: list[dict], profile_name: str, port: int = 18080) -> dict:
    matches = [p for p in profiles if p.get('remarks') == profile_name]
    if not profile_name or len(matches) != 1:
        raise ValueError('Selected subscription profile missing or ambiguous')
    nodes = [o for o in matches[0]['outbounds'] if o.get('protocol') == 'vless']
    if len(nodes) != 1:
        raise ValueError('Selected profile must contain exactly one VLESS node')
    node = copy.deepcopy(nodes[0])
    stream = node['streamSettings']
    if stream.get('network') not in ('tcp', 'raw') or stream.get('security') != 'reality':
        raise ValueError('Selected profile must use VLESS/TCP/REALITY')
    # Subscription routing, listeners and direct/fallback outbounds are deliberately excluded.
    outbound = {
        'tag': 'vpn', 'protocol': 'vless', 'settings': node['settings'],
        'streamSettings': {
            'network': stream['network'], 'security': 'reality',
            'realitySettings': {
                key: stream['realitySettings'][key]
                for key in ('serverName', 'publicKey', 'shortId', 'fingerprint')
                if key in stream['realitySettings']
            },
        },
    }
    return {
        'log': {'loglevel': 'none'},
        'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'http', 'settings': {}}],
        'outbounds': [outbound],
    }


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


class OpenAIAccountError(RuntimeError):
    """Changing the network cannot repair authentication or rate limits."""


def check_proxy(port: int = 18080) -> None:
    key = dotenv_values('/root/blago/blago-bot/.env')['OPENAI_API_KEY']
    with httpx.Client(proxy=f'http://127.0.0.1:{port}', trust_env=False, timeout=8) as client:
        response = client.get('https://api.openai.com/v1/models',
                              headers={'Authorization': 'Bearer ' + key})
        if response.status_code in (401, 429):
            raise OpenAIAccountError('OpenAI authentication or rate limit error')
        response.raise_for_status()


def probe_config(config: dict) -> None:
    with tempfile.TemporaryDirectory(prefix='blago-xray-', dir='/tmp') as directory:
        probe = Path(directory) / 'probe.json'
        probe_config = copy.deepcopy(config)
        probe_config['inbounds'][0]['port'] = 18081
        write_private(probe, json.dumps(probe_config).encode())
        subprocess.run([XRAY, 'run', '-test', '-config', str(probe)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process = subprocess.Popen([XRAY, 'run', '-config', str(probe)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for attempt in range(20):
                if process.poll() is not None:
                    raise RuntimeError('Candidate proxy exited')
                try:
                    check_proxy(18081)
                    return
                except httpx.ConnectError:
                    if attempt == 19:
                        raise
                    time.sleep(0.1)
        finally:
            process.terminate()
            process.wait(timeout=10)


def update(health_only: bool = False) -> None:
    if health_only:
        for attempt in range(2):
            try:
                check_proxy()
                print('Current proxy passed OpenAI probe')
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 0:
                    time.sleep(1)
        print('Current proxy failed two probes; searching subscription')
    env = dotenv_values(ENV_PATH)
    url = env.get('VPN_SUBSCRIPTION_URL', '')
    if urlsplit(url).scheme != 'https':
        raise ValueError('Subscription must use HTTPS')
    with urlopen(Request(url, headers={'User-Agent': 'v2rayNG'}), timeout=30) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Subscription exceeds size limit')
    profiles = json.loads(raw)
    selected = env.get('VPN_PROFILE_NAME', '')
    names = list(dict.fromkeys([selected] + [p.get('remarks', '') for p in profiles]))
    for index, name in enumerate(names):
        try:
            config = build_config(profiles, name)
        except (ValueError, KeyError):
            continue
        try:
            probe_config(config)
            break
        except OpenAIAccountError:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError, subprocess.CalledProcessError, RuntimeError):
            print(f'Candidate {index + 1} failed probe')
    else:
        raise RuntimeError('No working subscription profile; live configuration preserved')
    data = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
    previous = CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else None
    if data == previous:
        if health_only:
            subprocess.run(['systemctl', 'restart', 'xray-openai.service'], check=True)
            time.sleep(1)
            check_proxy()
        if name != selected:
            set_key(str(ENV_PATH), 'VPN_PROFILE_NAME', name)
        print('Subscription checked; configuration unchanged; OpenAI probe passed')
        return
    candidate = CONFIG_PATH.with_suffix('.new')
    write_private(candidate, data)
    os.chown(candidate, 0, grp.getgrnam('xray-openai').gr_gid)
    os.chmod(candidate, 0o640)
    candidate.replace(CONFIG_PATH)
    try:
        subprocess.run(['systemctl', 'restart', 'xray-openai.service'], check=True)
        time.sleep(1)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'xray-openai.service'], check=True)
        check_proxy()
    except Exception:
        if previous is not None:
            CONFIG_PATH.write_bytes(previous)
            subprocess.run(['systemctl', 'restart', 'xray-openai.service'], check=True)
        raise
    set_key(str(ENV_PATH), 'VPN_PROFILE_NAME', name)
    print('Subscription updated; candidate passed OpenAI probe; proxy restarted')


if __name__ == '__main__':
    try:
        # Daily refresh and health timer must never replace configurations concurrently.
        with open('/run/blago-openai-proxy.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(0)
            update(health_only='--health' in sys.argv[1:])
    except Exception as exc:
        # Exceptions can contain subscription URLs or node credentials.
        raise SystemExit('Subscription update failed (' + type(exc).__name__ + '); inspect configuration privately')
