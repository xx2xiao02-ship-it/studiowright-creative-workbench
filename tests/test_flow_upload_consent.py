import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/flow/console/browser_rendering.py'


class FlowUploadConsent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_bytes())
        choice = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'reference_dialog_choice')
        studio = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == 'RenderingStudio')
        studio.body = [node for node in studio.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name in ('configure_reference_uploads', '_media_ids')]
        self.scope = dict(Studio=object, asyncio=asyncio, Path=Path, handle_reference_dialog=AsyncMock(return_value=True))
        exec(compile(ast.Module(body=[choice, studio], type_ignores=[]), str(SOURCE), 'exec'), self.scope)

    def test_previous_button_count_rules(self):
        choose = self.scope['reference_dialog_choice']
        cases = (
            (['我同意'], '我同意'),
            (['取消'], '取消'),
            (['取消', '我同意'], '我同意'),
            (['I agree', 'Cancel'], 'I agree'),
            (['我同意', '查看说明', '取消'], '取消'),
            (['我同意', '取消', '稍后', '帮助'], None),
            (['取消', '取消', '我同意'], None),
            (['我同意', '同意'], None),
            (['删除', '取消'], None),
            (['我同意', '查看说明'], None),
            (['我同意', '查看说明', '稍后'], None),
            (['删除'], None),
            ([], None),
        )
        for labels, expected in cases:
            with self.subTest(labels=labels):
                self.assertEqual(choose(labels), expected)

    async def test_new_first_frame_uses_dialog_handler_without_library_history(self):
        studio = self.scope['RenderingStudio']()
        project, email = 'sample', 'owner@example.test'
        files = ('new-first-frame.png',)
        studio.configure_reference_uploads(project, files, email=email)
        page = SimpleNamespace(url='https://flow.google.com/project/' + project)
        button_dialog = object()

        async def upload(paths, *, dialog_handler):
            self.assertEqual(paths, [Path(files[0])])
            self.assertTrue(await dialog_handler(button_dialog))
            return [SimpleNamespace(media_id='frame', name=files[0])]

        flow = SimpleNamespace(page=page, project_id=project, upload=upload)
        with patch.dict(sys.modules, {'flow_references': SimpleNamespace(reference_ids=tuple)}):
            self.assertEqual(await studio._media_ids(flow, files), ('frame',))
        self.scope['handle_reference_dialog'].assert_awaited_once_with(page, button_dialog, email, project)
