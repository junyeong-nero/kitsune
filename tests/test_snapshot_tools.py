"""Run with: python -m unittest discover -s tests."""
import asyncio
import json
import os
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, server


class SnapshotToolsTests(unittest.TestCase):
    def test_snapshot_routes_and_tabs(self):
        with patch.object(server, '_get', return_value={'snapshot': 'tree'}) as get:
            self.assertEqual(server.browser_snapshot(), {'snapshot': 'tree'})
            get.assert_called_once_with('/api/snapshot')
        with patch.object(server, '_post', return_value={'snapshot': 'tree'}) as post:
            server.browser_snapshot(tab_id=7)
            post.assert_called_once_with('/api/snapshot', {'tabId': 7})

    def test_snapshot_scope_and_depth(self):
        cases = [
            ({'selector': '#catalog'}, {'selector': '#catalog'}),
            ({'ref': 's2e0'}, {'ref': 's2e0'}),
            ({'max_depth': 0}, {'maxDepth': 0}),
            ({'tab_id': 7, 'selector': '#fallback', 'ref': 's2e0', 'max_depth': 2},
             {'tabId': 7, 'selector': '#fallback', 'ref': 's2e0', 'maxDepth': 2}),
            ({'text': True}, {'text': True}),
            ({'tab_id': 7, 'selector': '#catalog', 'text': True},
             {'tabId': 7, 'selector': '#catalog', 'text': True}),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs), patch.object(server, '_post', return_value={'snapshot': 'tree'}) as post:
                self.assertEqual(server.browser_snapshot(**kwargs), {'snapshot': 'tree'})
                post.assert_called_once_with('/api/snapshot', expected)
        with patch.object(server, '_post') as post, patch.object(server, '_get') as get:
            invalid_cases: list[dict] = [{'max_depth': value} for value in (-1, 1.5, True, '2')]
            for kwargs in invalid_cases:
                with self.subTest(kwargs=kwargs):
                    self.assertEqual(server.browser_snapshot(**kwargs), {
                        'error': 'max_depth must be a non-negative integer',
                    })
            post.assert_not_called()
            get.assert_not_called()

    def test_bridge_snapshot_scope_routes(self):
        for command, body, expected in [
            ('GET', {}, {}),
            ('POST', {'tabId': 7}, {'tabId': 7}),
            ('POST', {'selector': '#catalog', 'ref': 's2e0', 'maxDepth': 0, 'tabId': 7,
                      'ignored': 'not forwarded'},
             {'selector': '#catalog', 'ref': 's2e0', 'maxDepth': 0, 'tabId': 7}),
            ('POST', {'text': True}, {'text': True}),
        ]:
            with self.subTest(command=command, body=body):
                handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
                handler.command = command
                handler.read_body = Mock(return_value=body)
                handler.send_json = Mock()
                handler.run_async = Mock(side_effect=asyncio.run)
                with patch.object(bridge, 'send_to_extension', return_value={'snapshot': 'tree'}) as send:
                    handler.handle_snapshot()
                    send.assert_called_once_with('getSnapshot', expected)
                handler.send_json.assert_called_once_with(200, {'snapshot': 'tree'})
                self.assertEqual(handler.read_body.call_count, int(command == 'POST'))

    def test_bridge_snapshot_rejects_invalid_depth(self):
        for max_depth in (-1, 1.5, True, '2'):
            with self.subTest(max_depth=max_depth):
                handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
                handler.command = 'POST'
                handler.read_body = Mock(return_value={'maxDepth': max_depth})
                handler.run_async = Mock()
                handler.send_json = Mock()
                handler.handle_snapshot()
                handler.run_async.assert_not_called()
                handler.send_json.assert_called_once_with(200, {
                    'error': 'max_depth must be a non-negative integer',
                })

    def test_dom_scope_and_depth(self):
        with patch.object(server, '_get', return_value={'tree': {}}) as get:
            self.assertEqual(server.browser_dom(), {'tree': {}})
            get.assert_called_once_with('/api/dom')
        cases = [
            ({'tab_id': 7}, {'tabId': 7}),
            ({'selector': '#catalog'}, {'selector': '#catalog'}),
            ({'ref': 's2e0'}, {'ref': 's2e0'}),
            ({'max_depth': 0}, {'maxDepth': 0}),
            ({'tab_id': 7, 'selector': '#fallback', 'ref': 's2e0', 'max_depth': 2},
             {'tabId': 7, 'selector': '#fallback', 'ref': 's2e0', 'maxDepth': 2}),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs), patch.object(server, '_post', return_value={'tree': {}}) as post:
                self.assertEqual(server.browser_dom(**kwargs), {'tree': {}})
                post.assert_called_once_with('/api/dom', expected)
        with patch.object(server, '_post') as post, patch.object(server, '_get') as get:
            invalid_cases: list[dict] = [{'max_depth': value} for value in (-1, 1.5, True, '2')]
            for kwargs in invalid_cases:
                with self.subTest(kwargs=kwargs):
                    self.assertEqual(server.browser_dom(**kwargs), {
                        'error': 'max_depth must be a non-negative integer',
                    })
            post.assert_not_called()
            get.assert_not_called()

    def test_bridge_dom_scope_routes(self):
        for command, body, expected in [
            ('GET', {}, {'depth': 6}),
            ('POST', {'tabId': 7}, {'tabId': 7, 'depth': 6}),
            ('POST', {'selector': '#catalog', 'ref': 's2e0', 'maxDepth': 0, 'tabId': 7,
                      'ignored': 'not forwarded'},
             {'selector': '#catalog', 'ref': 's2e0', 'depth': 0, 'tabId': 7}),
        ]:
            with self.subTest(command=command, body=body):
                handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
                handler.command = command
                handler.read_body = Mock(return_value=body)
                handler.send_json = Mock()
                handler.run_async = Mock(side_effect=asyncio.run)
                with patch.object(bridge, 'send_to_extension', return_value={'tree': {}}) as send:
                    handler.handle_dom()
                    send.assert_called_once_with('getAccessibilityTree', expected)
                handler.send_json.assert_called_once_with(200, {'tree': {}})

    def test_bridge_dom_rejects_invalid_depth(self):
        for max_depth in (-1, 1.5, True, '2'):
            with self.subTest(max_depth=max_depth):
                handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
                handler.command = 'POST'
                handler.read_body = Mock(return_value={'maxDepth': max_depth})
                handler.run_async = Mock()
                handler.send_json = Mock()
                handler.handle_dom()
                handler.run_async.assert_not_called()
                handler.send_json.assert_called_once_with(200, {
                    'error': 'max_depth must be a non-negative integer',
                })

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
                    self.assertNotIn('snapshotText', commands[-1])
                with patch.dict(os.environ, {'KITSUNE_POST_ACTION_SNAPSHOT': 'text'}):
                    await bridge._send_to_extension_raw('click')
                    self.assertTrue(commands[-1]['includeSnapshot'])
                    self.assertTrue(commands[-1]['snapshotText'])

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
