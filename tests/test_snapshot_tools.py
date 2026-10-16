"""Run with: python -m unittest discover -s tests."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from kitsune import bridge, server


class SnapshotToolsTests(unittest.TestCase):
    def test_snapshot_routes_and_tabs(self):
        with patch.object(server, '_get', return_value={'snapshot': 'tree'}) as get:
            self.assertEqual(server.browser_snapshot(), {'snapshot': 'tree'})
            get.assert_called_once_with('/api/snapshot')
        with patch.object(server, '_post', return_value={'snapshot': 'tree'}) as post:
            server.browser_snapshot(tab_id=7)
            post.assert_called_once_with('/api/snapshot', {'tabId': 7})

    def test_actions_accept_ref_without_selector_and_keep_audit_description(self):
        cases = [
            ('click', {}), ('type', {'text': 'hello'}), ('type', {'text': 'hello', 'mode': 'fill'}),
            ('hover', {}), ('highlight', {}), ('select_option', {'value': 'option'}),
            ('check', {}), ('focus', {}), ('double_click', {}),
            ('drag', {'to_ref': 's2e1'}), ('screenshot', {}),
        ]
        for action, kwargs in cases:
            with self.subTest(action=action), patch.object(server, '_post', return_value={'ok': True}) as post:
                getattr(server, 'browser_' + action)(ref='s2e0', element='human description', tab_id=7, **kwargs)
                body = post.call_args.args[1]
                self.assertEqual(body['ref'], 's2e0')
                self.assertEqual(body['element'], 'human description')
                self.assertEqual(body['tabId'], 7)
                self.assertNotEqual(body.get('selector'), 'human description')
                if action == 'drag':
                    self.assertEqual(body['toRef'], 's2e1')

    def test_existing_positional_selector_arguments(self):
        with patch.object(server, '_post', return_value={'ok': True}) as post:
            server.browser_type('#email', 'a@b', clear=True, tab_id=7)
            self.assertEqual(post.call_args.args[1], {
                'selector': '#email', 'text': 'a@b', 'clear': True, 'tabId': 7,
            })

    def test_bridge_snapshot_environment_and_override(self):
        async def check():
            commands = []

            class WebSocket:
                async def send(self, raw):
                    command = json.loads(raw)
                    commands.append(command)
                    bridge.pending_commands[command['id']]['future'].set_result({'ok': True})

            with patch.object(bridge, 'extension_ws', WebSocket()):
                with patch.dict(os.environ, {'KITSUNE_POST_ACTION_SNAPSHOT': '0'}):
                    await bridge._send_to_extension_raw('click', {'ref': 's2e0'})
                    self.assertFalse(commands[-1]['includeSnapshot'])
                    await bridge._send_to_extension_raw('click', {'includeSnapshot': True})
                    self.assertTrue(commands[-1]['includeSnapshot'])
                with patch.dict(os.environ, {'KITSUNE_POST_ACTION_SNAPSHOT': '1'}):
                    await bridge._send_to_extension_raw('click')
                    self.assertTrue(commands[-1]['includeSnapshot'])

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
