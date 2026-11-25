import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/flow/console"))
from login_sessions import ImportRestored, inject_verified


class ImportSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = [{"name": "fixture-old"}]
        self.bundle = {"email": "fixture@example.com", "cookies": [{"name": "fixture-new"}]}
        self.context = Mock(cookies=AsyncMock(return_value=self.old),
                            clear_cookies=AsyncMock(), add_cookies=AsyncMock())
        self.state = {"signed_in": True, "emails": [self.bundle["email"]], "projects": []}
        self.page = Mock(url="https://flow.google.com/", goto=AsyncMock(),
                         evaluate=AsyncMock(return_value=self.state))
        self.vault = Mock(put=Mock(return_value="backup"), get=Mock(return_value={"cookies": self.old}))

    async def test_commit_only_after_finalization(self):
        finalize = AsyncMock(return_value={"email": self.bundle["email"], "projects": []})
        result = await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertEqual(result["email"], self.bundle["email"])
        finalize.assert_awaited_once_with(self.state)
        self.context.add_cookies.assert_awaited_once_with(self.bundle["cookies"])
        self.vault.remove.assert_called_once_with("backup")

    async def test_window_failure_restores_previous_login(self):
        finalize = AsyncMock(side_effect=ValueError("window failed"))
        with self.assertRaisesRegex(ImportRestored, "邮箱一致，但项目同步或窗口登记未完成"):
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.context.add_cookies.assert_awaited_with(self.old)
        self.assertEqual(self.context.clear_cookies.await_count, 2)

    async def test_cancel_restores_previous_login(self):
        with self.assertRaises(asyncio.CancelledError):
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault,
                                  finalize=AsyncMock(side_effect=asyncio.CancelledError))
        self.context.add_cookies.assert_awaited_with(self.old)

    async def test_wrong_email_never_registers(self):
        self.page.evaluate.return_value = dict(self.state, emails=["other@example.com"])
        finalize = AsyncMock()
        with self.assertRaisesRegex(ImportRestored, "Flow 返回的邮箱与目标账号不一致") as failure:
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertNotIn("other@example.com", str(failure.exception))
        finalize.assert_not_called()
        self.context.add_cookies.assert_awaited_with(self.old)

    async def test_write_and_navigation_failures_restore_previous_login(self):
        finalize = AsyncMock()
        self.context.add_cookies.side_effect = [ValueError("private cookie"), None]
        with self.assertRaisesRegex(ImportRestored, "无法写入本次登录资料") as failure:
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertNotIn("private cookie", str(failure.exception))
        self.page.goto.assert_not_awaited()
        self.context.add_cookies.assert_awaited_with(self.old)

        self.context.add_cookies.side_effect = None
        self.page.goto.side_effect = ValueError("private URL")
        with self.assertRaisesRegex(ImportRestored, "未能打开 Flow 页面") as failure:
            await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertNotIn("private URL", str(failure.exception))
        self.context.add_cookies.assert_awaited_with(self.old)
        finalize.assert_not_awaited()

    async def test_google_login_redirect_does_not_register_account(self):
        self.page.url = "https://accounts.google.com/ServiceLogin?example=private"
        self.page.evaluate.return_value = dict(self.state, signed_in=False, emails=[])
        finalize = AsyncMock()
        with patch("login_sessions.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(ImportRestored, "Flow 跳到了 Google 登录页面") as failure:
                await inject_verified(self.context, self.page, self.bundle, "state", self.vault, finalize=finalize)
        self.assertNotIn("example=private", str(failure.exception))
        self.assertEqual(self.page.evaluate.await_count, 12)
        self.context.add_cookies.assert_awaited_with(self.old)
        finalize.assert_not_awaited()


class ImportedProjectsTest(unittest.TestCase):
    def test_blank_home_title_preserves_known_name_and_duplicate_title(self):
        from adapter import flow_home_projects
        ident = "72591c35-415f-41c7-b2a2-835e716eb303"
        href = "https://flow.google.com/project/" + ident
        empty = {"url": href, "title": " "}
        self.assertEqual(flow_home_projects({"projects": [empty]}, {ident: "Saved name"})[0]["title"], "Saved name")
        named = {"url": href, "title": "Observed name"}
        self.assertEqual(flow_home_projects({"projects": [named, empty]}, {ident: "Saved name"})[0]["title"], "Observed name")
