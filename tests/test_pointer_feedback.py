import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/workbench"))
import unittest
from unittest.mock import AsyncMock, patch

from pointer_sequence import confirm_pointer_over_input, wait_for_input_layout, PointerPreparationError
from wayland_desktop_driver import MAX_COMPOSER_TABS, WaylandDesktopDriver


class PointerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.driver = AsyncMock()
        self.driver._identity_confirmed.return_value = True
        self.driver._os_focus_confirmed.return_value = True
        self.composer, self.pointer = AsyncMock(), AsyncMock()

    async def check(self):
        return await confirm_pointer_over_input(composer=self.composer, target=object(),
            driver=self.driver, pointer=self.pointer)

    async def test_delayed_event_does_not_cause_immediate_failure(self):
        self.composer.evaluate.side_effect = [{'hit': False}, {'hit': True}]
        self.assertEqual(await self.check(), 0)
        self.pointer.move.assert_not_called()

    async def test_observed_offset_is_corrected_before_confirming_hit(self):
        miss = dict(hit=False, visible=True, x=100, y=110, centerX=120, centerY=130)
        self.composer.evaluate.side_effect = [miss, miss, miss, {'hit': True}]
        self.assertEqual(await self.check(), 1)
        self.pointer.move.assert_awaited_once_with(20, 20)

    async def test_missing_observation_never_authorizes_motion_or_click(self):
        self.composer.evaluate.return_value = dict(hit=False, visible=True, x=None, y=None)
        with self.assertRaisesRegex(PointerPreparationError, 'pointer_not_over_input'):
            await self.check()
        self.pointer.move.assert_not_called()
        self.pointer.click.assert_not_called()

    async def test_focus_loss_stops_correction(self):
        self.driver._os_focus_confirmed.return_value = False
        with self.assertRaisesRegex(PointerPreparationError, 'window_focus_lost'):
            await self.check()
        self.composer.evaluate.assert_not_called()
        self.pointer.move.assert_not_called()

    async def test_window_and_editor_must_settle(self):
        self.composer.bounding_box.side_effect = [dict(x=x,y=10,width=100,height=20) for x in (10,20,20)]
        self.driver.window_geometry.return_value = dict(x=0,y=0,width=1920,height=1080)
        await wait_for_input_layout(composer=self.composer,target=object(),driver=self.driver)
        self.assertEqual(self.composer.bounding_box.await_count,3)
        self.composer.scroll_into_view_if_needed.assert_awaited_once()


class TabFocusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.driver = WaylandDesktopDriver({})
        self.driver._identity_confirmed = AsyncMock(return_value=True)
        self.driver._os_focus_confirmed = AsyncMock(return_value=True)
        self.page, self.composer = AsyncMock(), AsyncMock()
        self.target = object()

    async def test_tab_focus_checks_the_editor_without_clicking(self):
        self.composer.evaluate.side_effect = ['other', 'Shift+Tab', 'composer']
        with patch('task_coordinator.record') as record:
            self.assertTrue(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
            record.assert_called_once_with('composer_tab_focused', steps=1)
        self.page.keyboard.press.assert_awaited_once_with('Shift+Tab')
        self.composer.click.assert_not_awaited()

    async def test_focus_loss_stops_further_tab_keys(self):
        self.composer.evaluate.side_effect = ['other', 'Tab']
        self.driver._os_focus_confirmed.side_effect = [True, True, False]
        self.assertFalse(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
        self.page.keyboard.press.assert_awaited_once_with('Tab')

    async def test_tab_search_does_not_continue_indefinitely(self):
        self.composer.evaluate.side_effect = ['other', 'Tab'] + ['other'] * MAX_COMPOSER_TABS
        self.assertFalse(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
        self.assertEqual(self.page.keyboard.press.await_count, MAX_COMPOSER_TABS)
        self.composer.click.assert_not_awaited()
