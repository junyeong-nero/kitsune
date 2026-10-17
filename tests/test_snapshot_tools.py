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
            ('keypress', {'key': 'Enter'}), ('submit_form', {}), ('scroll', {}),
            ('wait_for', {}), ('blur', {}), ('html', {}), ('bounds', {}),
            ('computed_style', {}), ('click_and_wait_navigation', {}), ('markdown', {}),
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

    def test_new_ref_tools_preserve_selector_and_default_targets(self):
        cases = [
            ('keypress', {'key': 'Enter'}, {'key': 'Enter'}),
            ('submit_form', {}, {}), ('scroll', {}, {'direction': 'down', 'amount': 500}),
            ('blur', {}, {}), ('html', {}, {'selector': ''}), ('bounds', {}, {'selector': ''}),
            ('computed_style', {}, {'selector': ''}),
            ('click_and_wait_navigation', {}, {'selector': '', 'timeout': 15000}),
            ('markdown', {}, {}),
        ]
        for action, kwargs, default_body in cases:
            with self.subTest(action=action), patch.object(server, '_post', return_value={'ok': True}) as post:
                function = getattr(server, 'browser_' + action)
                function(**kwargs)
                self.assertEqual(post.call_args.args[1], default_body)
                function(element='audit only', **kwargs)
                self.assertEqual(post.call_args.args[1], {**default_body, 'element': 'audit only'})
                function(selector='#target', **kwargs)
                self.assertEqual(post.call_args.args[1], {**default_body, 'selector': '#target'})
                function(selector='#target', ref='s2e0', element='audit only', **kwargs)
                self.assertEqual(post.call_args.args[1], {
                    **default_body, 'selector': '#target', 'ref': 's2e0', 'element': 'audit only',
                })

    def test_wait_for_ref_is_one_element_condition(self):
        with patch.object(server, '_post', return_value={'ok': True}) as post:
            server.browser_wait_for(selector='#fallback', ref='s2e0', element='audit only',
                                    timeout=12000, tab_id=7)
            post.assert_called_once_with('/api/wait-for-element', {
                'selector': '#fallback', 'ref': 's2e0', 'element': 'audit only',
                'timeout': 12000, 'pollInterval': 200, 'tabId': 7,
            }, timeout=22)
            post.reset_mock()
            invalid_conditions: list[dict] = [
                {'ref': 's2e0', 'text': 'Ready'}, {'ref': 's2e0', 'js': 'true'},
                {'ref': 's2e0', 'url': 'done'}, {'ref': 's2e0', 'title': 'Done'},
                {'ref': 's2e0', 'text_gone': 'Loading'},
                {'ref': 's2e0', 'network_idle': True}, {'ref': 's2e0', 'time': 0},
                {'ref': 's2e0', 'timeout': 0}, {'element': '#not-a-selector'},
            ]
            for args in invalid_conditions:
                with self.subTest(args=args):
                    self.assertIn('error', server.browser_wait_for(**args))
            post.assert_not_called()

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
                    for action in ('keypress', 'submitForm', 'scroll', 'waitForElement',
                                   'blurElement', 'getHTML', 'getBounds', 'getComputedStyle',
                                   'clickAndWaitNavigation', 'getMarkdown'):
                        await bridge._send_to_extension_raw(action, {
                            'ref': 's2e0', 'element': 'audit only', 'tabId': 7,
                        })
                        self.assertEqual(commands[-1]['action'], action)
                        self.assertEqual(commands[-1]['ref'], 's2e0')
                        self.assertEqual(commands[-1]['element'], 'audit only')
                        self.assertEqual(commands[-1]['tabId'], 7)
                        self.assertFalse(commands[-1]['includeSnapshot'])
                    await bridge._send_to_extension_raw('click', {'includeSnapshot': True})
                    self.assertTrue(commands[-1]['includeSnapshot'])
                with patch.dict(os.environ, {'KITSUNE_POST_ACTION_SNAPSHOT': '1'}):
                    await bridge._send_to_extension_raw('click')
                    self.assertTrue(commands[-1]['includeSnapshot'])

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
