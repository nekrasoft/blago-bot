import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
import httpx
from unittest.mock import patch

from openai import APIConnectionError

from app.config import load_settings
from app.summarizer import TenderSummarizer

spec = importlib.util.spec_from_file_location(
    'subscription', Path(__file__).resolve().parents[1] / 'deploy/openai-proxy/update_subscription.py'
)
subscription = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subscription)


class OpenAIProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_proxy_never_connects_directly(self):
        received = []

        async def handler(reader, writer):
            received.append(True)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            proxy_port = reserved.getsockname()[1]
            summarizer = TenderSummarizer(
                api_key='test-key', model='test', language='ru',
                max_doc_chars=1000, chunk_size=1000, chunk_overlap=0,
                proxy_url=f'http://127.0.0.1:{proxy_port}',
            )
            summarizer.client.base_url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/'
            summarizer.client.max_retries = 0
            try:
                with self.assertRaises(APIConnectionError):
                    await summarizer._ask_llm('test', 'test')
                self.assertEqual(received, [])
            finally:
                await summarizer.client.close()
                server.close()
                await server.wait_closed()

    def test_settings_load_optional_proxy(self):
        env = {'BOT_PLATFORM': 'max', 'MAX_BOT_TOKEN': 'test',
               'OPENAI_API_KEY': 'test', 'ALLOWED_SOURCE_USER_ID': '123'}
        for value in ('', 'http://127.0.0.1:18080'):
            with self.subTest(proxy=value), patch.dict('os.environ', {**env, 'OPENAI_PROXY_URL': value}, clear=True):
                with patch('app.config.load_dotenv'):
                    self.assertEqual(load_settings().openai_proxy_url, value)


class SubscriptionTests(unittest.TestCase):
    def test_healthy_proxy_does_not_fetch_subscription(self):
        with patch.object(subscription, 'check_proxy') as check, \
             patch.object(subscription, 'urlopen') as fetch:
            subscription.update(health_only=True)
        check.assert_called_once_with()
        fetch.assert_not_called()

    def test_account_errors_do_not_switch_nodes(self):
        for status in (401, 429):
            with self.subTest(status=status), \
                 patch.object(subscription, 'dotenv_values', return_value={'OPENAI_API_KEY': 'test'}), \
                 patch.object(subscription.httpx, 'Client') as client:
                client.return_value.__enter__.return_value.get.return_value = httpx.Response(
                    status, request=httpx.Request('GET', 'https://api.openai.com/v1/models'))
                with self.assertRaises(subscription.OpenAIAccountError):
                    subscription.update(health_only=True)

    def test_failed_node_switches_only_after_candidate_probe(self):
        first = self.profile()
        second = self.profile()
        second['remarks'] = 'fallback'
        second['outbounds'][1]['settings'] = {'vnext': [{'address': 'candidate'}]}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_bytes(b'working')
            with patch.object(subscription, 'CONFIG_PATH', config), \
                 patch.object(subscription, 'dotenv_values', return_value={
                     'VPN_SUBSCRIPTION_URL': 'https://example.com/subscription',
                     'VPN_PROFILE_NAME': 'selected'}), \
                 patch.object(subscription, 'urlopen') as fetch, \
                 patch.object(subscription, 'check_proxy', side_effect=[
                     httpx.ConnectTimeout('offline'), httpx.ConnectTimeout('offline'), None]), \
                 patch.object(subscription, 'probe_config', side_effect=[httpx.ConnectTimeout('offline'), None]) as probe, \
                 patch.object(subscription, 'set_key') as save, \
                 patch.object(subscription.time, 'sleep'), \
                 patch.object(subscription.os, 'chown'), \
                 patch.object(subscription.grp, 'getgrnam') as group, \
                 patch.object(subscription.subprocess, 'run'):
                group.return_value.gr_gid = 0
                fetch.return_value.__enter__.return_value.read.return_value = json.dumps([first, second]).encode()
                subscription.update(health_only=True)
                self.assertEqual(probe.call_count, 2)
                save.assert_called_once_with(str(subscription.ENV_PATH), 'VPN_PROFILE_NAME', 'fallback')
            self.assertEqual(json.loads(config.read_text())['outbounds'][0]['settings'],
                             second['outbounds'][1]['settings'])

    def test_failed_live_probe_restores_previous_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_bytes(b'working')
            with patch.object(subscription, 'CONFIG_PATH', config), \
                 patch.object(subscription, 'dotenv_values', return_value={
                     'VPN_SUBSCRIPTION_URL': 'https://example.com/subscription',
                     'VPN_PROFILE_NAME': 'selected'}), \
                 patch.object(subscription, 'urlopen') as fetch, \
                 patch.object(subscription, 'probe_config'), \
                 patch.object(subscription, 'check_proxy', side_effect=httpx.ConnectTimeout('offline')), \
                 patch.object(subscription, 'set_key') as save, \
                 patch.object(subscription.time, 'sleep'), \
                 patch.object(subscription.os, 'chown'), \
                 patch.object(subscription.grp, 'getgrnam') as group, \
                 patch.object(subscription.subprocess, 'run'):
                group.return_value.gr_gid = 0
                fetch.return_value.__enter__.return_value.read.return_value = json.dumps([self.profile()]).encode()
                with self.assertRaises(httpx.ConnectTimeout):
                    subscription.update()
                save.assert_not_called()
            self.assertEqual(config.read_bytes(), b'working')

    def profile(self):
        return {'remarks': 'selected', 'inbounds': [{'listen': '0.0.0.0'}], 'outbounds': [
            {'protocol': 'freedom'},
            {'protocol': 'vless', 'settings': {'vnext': []}, 'streamSettings': {
                'network': 'tcp', 'security': 'reality',
                'realitySettings': {'masterKeyLog': '/tmp/not-allowed'},
                'sockopt': {'dialerProxy': 'direct'},
            }},
        ]}

    def test_only_selected_tunnel_and_loopback_listener_are_used(self):
        config = subscription.build_config([self.profile()], 'selected')
        self.assertEqual(config['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual([o['protocol'] for o in config['outbounds']], ['vless'])
        self.assertNotIn('sockopt', config['outbounds'][0]['streamSettings'])
        self.assertEqual(config['log']['loglevel'], 'none')
        self.assertNotIn('masterKeyLog', config['outbounds'][0]['streamSettings']['realitySettings'])

    def test_missing_or_duplicate_profile_is_rejected(self):
        for profiles in ([], [self.profile(), self.profile()]):
            with self.assertRaises(ValueError):
                subscription.build_config(profiles, 'selected')

    def test_invalid_candidate_preserves_working_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_bytes(b'working')
            with patch.object(subscription, 'CONFIG_PATH', config), \
                 patch.object(subscription, 'dotenv_values', return_value={
                     'VPN_SUBSCRIPTION_URL': 'https://example.com/subscription',
                     'VPN_PROFILE_NAME': 'selected',
                 }), \
                 patch.object(subscription, 'urlopen') as fetch, \
                 patch.object(subscription.subprocess, 'run', side_effect=RuntimeError('invalid candidate')):
                fetch.return_value.__enter__.return_value.read.return_value = json.dumps([self.profile()]).encode()
                with self.assertRaises(RuntimeError):
                    subscription.update()
            self.assertEqual(config.read_bytes(), b'working')
