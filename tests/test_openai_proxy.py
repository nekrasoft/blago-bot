import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
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
