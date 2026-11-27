"""Shared, fail-closed foreground paste/submit sequence (staging only).

The desktop driver must establish actual OS-window identity, not browser focus
emulation. This module does not provide a desktop driver or enable itself.
"""
import asyncio
import json
import random
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path


FLOW_PROMPT_TEXT = r"""editor => {
    const paragraphs = Array.from(editor.childNodes);
    if (!editor.classList.contains('ProseMirror') || !paragraphs.length ||
        paragraphs.some(node => node.nodeName !== 'P')) {
        return editor.innerText;
    }
    // Chrome adds an extra visual line break between paragraphs in editor.innerText.
    return paragraphs.map(paragraph => {
        const child = paragraph.firstChild;
        if (paragraph.childNodes.length === 1 && child.nodeName === 'BR' &&
            child.classList.contains('ProseMirror-trailingBreak')) {
            return '';
        }
        return paragraph.innerText;
    }).join('\n');
}"""

FLOW_PREFIX_CHARACTERS = """prompt => {
    const characters = [];
    for (const {segment} of new Intl.Segmenter().segment(prompt)) {
        characters.push(segment);
        if (characters.length === 5) break;
    }
    return characters;
}"""

FLOW_SELECT_PREFIX = r"""(editor, expected) => {
    if (!document.hasFocus() || !editor.contains(document.activeElement) ||
        (""" + FLOW_PROMPT_TEXT + r""")(editor).replace(/\r\n/g, '\n') !== expected) {
        return false;
    }
    const range = document.createRange();
    range.selectNodeContents(editor);
    const selection = document.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
}"""


async def read_flow_prompt(composer):
    return (await composer.evaluate(FLOW_PROMPT_TEXT)).replace('\r\n', '\n')


def choose_prompt_input(prompt):
    import re
    if re.fullmatch(r'[A-Za-z0-9 ,.]{1,120}', prompt) and random.randrange(2) == 0:
        return 'type'
    return 'cdp_type'


class InputNotSubmitted(Exception):
    """The provider submission boundary was not crossed."""


class InputOutcomeUnknown(Exception):
    """Submission may have happened; callers must not replay automatically."""


async def _pacing_wait(record, account_key):
    """Same-account de-burst only. Cross-account spacing is already provided by
    the serialized foreground queue (one focused desktop window at a time), so a
    global gate would throttle without adding any real separation."""
    path = Path.home() / '.local/state/creative-unified-workbench/submit-pacing.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    account_wait = (state.get('per_account') or {}).get(account_key, 0) + random.uniform(45.0, 90.0) - time.time()
    if account_wait > 0:
        record('submit_pacing_wait', seconds=round(account_wait, 1), scope='account')
        await asyncio.sleep(min(account_wait, 120.0))


async def _hid_keyboard(driver, target):
    """Return a ready HID keyboard, or ``None`` when the portal backend is chosen."""
    from input_backend import open_keyboard
    keyboard = open_keyboard(target, gui_environment=getattr(driver, 'env', None))
    try:
        await keyboard.ensure_ready()
    except Exception:
        # A fixed hid policy must fail closed instead of silently typing through
        # the browser; an auto policy may fall back to the portal.
        if getattr(keyboard, 'allow_fallback', True) is False:
            raise
        return None
    if getattr(keyboard, 'backend', 'hid') != 'hid':
        return None
    return keyboard


async def _type_prompt_on_device(driver, page, composer, target, keyboard, prompt, record, *, maximum=400):
    """Type ASCII on the device with focus checks; paste the rest via clipboard."""
    from uinput_hid import can_type
    # A literal control key such as Enter can trigger the editor's submit action.
    typed = (keyboard is not None and can_type(prompt, maximum)
             and all(32 <= ord(char) < 127 for char in prompt))
    record('typed_text_started' if typed else 'clipboard_paste_started', characters=len(prompt))
    if not typed:
        await driver.copy_prompt_and_paste(page, composer, target, prompt)
        return False

    async def guard():
        return bool(await driver.confirm_focus(target, page, composer))

    try:
        await keyboard.type(prompt, guard=guard)
    except Exception as exc:
        if str(exc) != 'window_focus_lost':
            raise
        # Never continue typing into whatever window now owns the keyboard.
        try:
            if await driver.confirm_focus(target, page, composer):
                await driver.clear_composer(page, composer)
        except Exception:
            pass
        raise InputNotSubmitted('window_focus_lost_during_typing') from exc
    return True


async def _verify_prompt(driver, page, composer, target, prompt):
    """Poll for an exact match within the caller's verification budget."""
    observed = await read_flow_prompt(composer)
    while observed != prompt:
        if not await driver.confirm_focus(target, page, composer):
            raise InputNotSubmitted('window_focus_lost')
        await asyncio.sleep(.05)
        observed = await read_flow_prompt(composer)
    if not await driver.confirm_focus(target, page, composer):
        raise InputNotSubmitted('window_focus_lost')


FLOW_PROMPT_TIMEOUT = 4.0


@asynccontextmanager
async def _flow_prompt_step(step, deadline, record):
    loop = asyncio.get_running_loop()
    started = loop.time()
    record(f'flow_prompt_{step}_started',
           remaining_seconds=round(max(0, deadline - started), 3))
    try:
        async with asyncio.timeout_at(deadline):
            if loop.time() >= deadline:
                raise TimeoutError
            yield
            if loop.time() >= deadline:
                raise TimeoutError
    except InputNotSubmitted as exc:
        record(f'flow_prompt_{step}_failed', reason=str(exc))
        raise
    except Exception as exc:
        outcome = 'timeout' if isinstance(exc, TimeoutError) else 'failed'
        reason = f'prompt_{step}_{outcome}'
        record(f'flow_prompt_{step}_failed', reason=reason,
               error=type(exc).__name__)
        raise InputNotSubmitted(reason) from exc
    else:
        record(f'flow_prompt_{step}_completed',
               elapsed_ms=round((loop.time() - started) * 1000))


async def _prepare_flow_prefix(driver, page, composer, target, prompt, record):
    if await driver.composer_text(page, composer) not in ('', '\n'):
        raise InputNotSubmitted('composer_not_empty')
    if not await driver.confirm_focus(target, page, composer):
        raise InputNotSubmitted('window_focus_lost')

    characters = await page.evaluate(FLOW_PREFIX_CHARACTERS, prompt)
    if len(characters) >= 2:
        characters = characters[:random.randint(2, len(characters))]
        prefix = ''.join(characters)
        record('flow_prefix_input_started', characters=len(characters))
        if '\n' in prefix or '\r' in prefix:
            # Paste preserves line breaks without triggering an Enter shortcut.
            await driver.copy_prompt_and_paste(page, composer, target, prefix)
        else:
            for character in characters:
                if not await driver.confirm_focus(target, page, composer):
                    raise InputNotSubmitted('window_focus_lost_during_typing')
                await page.keyboard.insert_text(character)
        await _verify_prompt(driver, page, composer, target, prefix)
        if not await composer.evaluate(FLOW_SELECT_PREFIX, prefix):
            raise InputNotSubmitted('flow_prefix_changed_before_deletion')
        await page.keyboard.press('Backspace')
        if await driver.composer_text(page, composer) not in ('', '\n'):
            raise InputNotSubmitted('flow_prefix_deletion_incomplete')

async def enter_hybrid_prompt(driver, page, composer, target, prompt, record, *, deadline):
    async with _flow_prompt_step('prefix', deadline, record):
        await _prepare_flow_prefix(driver, page, composer, target, prompt, record)
    async with _flow_prompt_step('paste', deadline, record):
        await driver.copy_prompt_and_paste(page, composer, target, prompt)
    async with _flow_prompt_step('verify', deadline, record):
        await _verify_prompt(driver, page, composer, target, prompt)


@dataclass(frozen=True)
class Target:
    account_key: str
    process_id: int
    window_id: int
    workspace: int


async def paste_and_submit(*, queue, driver, page, composer, target, prompt,
                           checkpoint, submit, timeout=300, hybrid=False):
    """Serialize focus, copy, paste, verification and exactly one submission.

    The caller must already hold the selected account's existing profile lock.
    It may await the provider's response only after this function returns.
    The driver must fail rather than guess when OS focus cannot be established.
    """
    async with queue.acquire_async(timeout=timeout):
        return await paste_and_submit_reserved(driver=driver, page=page, composer=composer,
            target=target, prompt=prompt, checkpoint=checkpoint, submit=submit, hybrid=hybrid)


async def _require_empty_prompt_target(driver, page, composer):
    from task_coordinator import preparation_safe
    if not preparation_safe():
        raise InputNotSubmitted('submission_boundary_already_recorded')
    if await driver.composer_text(page, composer) not in ('', '\n'):
        raise InputNotSubmitted('composer_not_empty')


async def paste_and_submit_reserved(*, driver, page, composer, target, prompt,
                                    checkpoint, submit, hybrid=False):
    """Execute while the caller holds the shared foreground queue lease."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputNotSubmitted('invalid_prompt')
    # Chrome exposes pasted Windows line breaks as LF in the editor.
    prompt = prompt.replace('\r\n', '\n')
    if not isinstance(target, Target) or target.process_id <= 0 or target.window_id <= 0:
        raise InputNotSubmitted('invalid_target')
    from task_coordinator import record
    await _pacing_wait(record, target.account_key)
    mode = "hybrid" if hybrid else choose_prompt_input(prompt)
    try:
            await _require_empty_prompt_target(driver, page, composer)
            # The editor has answered. Start the Flow budget before any activation or Tab.
            deadline = asyncio.get_running_loop().time() + FLOW_PROMPT_TIMEOUT if hybrid else None
            focus_step = _flow_prompt_step('focus', deadline, record) if hybrid else nullcontext()
            async with focus_step:
                # Retry focus only; prompt input and submission stay single-shot.
                for attempt in range(3):
                    if attempt:
                        await _require_empty_prompt_target(driver, page, composer)
                    try:
                        if not await driver.activate_and_confirm(target):
                            raise InputNotSubmitted('window_activation_unconfirmed')
                        ready = await (driver.composer_ready_cdp(page, composer, target)
                                       if mode in ('cdp_type', 'hybrid')
                                       else driver.composer_ready(page, composer, target))
                        if not ready:
                            raise InputNotSubmitted('composer_unavailable')
                        break
                    except Exception as exc:
                        record('input_preparation_recovery',attempt=attempt+1,error=type(exc).__name__)
                        if attempt==2:
                            raise
                        await asyncio.sleep(.6)
            if hybrid:
                await enter_hybrid_prompt(driver, page, composer, target, prompt, record, deadline=deadline)
                record('hybrid_input_verified', characters=len(prompt))
            else:
                # Empty contenteditable paragraphs such as <p><br></p> expose
                # a single newline through innerText; they contain no draft text.
                if await driver.composer_text(page, composer) not in ('', '\n'):
                    raise InputNotSubmitted('composer_not_empty')
                keyboard = await _hid_keyboard(driver, target)
                # A hardware backend must stay hardware end to end: text that the
                # HID keymap can reproduce is typed on the device, anything else
                # (CJK and the like) goes through the clipboard with a real Ctrl+V.
                # Only the portal path may still fall back to CDP typing.
                hid_backend = keyboard is not None
                if hid_backend:
                    typed_on_device = await _type_prompt_on_device(
                        driver, page, composer, target, keyboard, prompt, record)
                else:
                    typed_on_device = False
                    mode = mode if mode == 'type' else 'cdp_type'
                    record(('typing_started' if mode == 'type' else mode + '_started'),
                           characters=len(prompt))
                    if mode == 'type':
                        if not await driver.confirm_focus(target, page, composer):
                            raise InputNotSubmitted('window_focus_lost')
                        await driver.type_prompt_chars(page, prompt)
                    else:
                        try:
                            await driver.type_prompt_chars(page, prompt)
                        except Exception as exc:
                            record('cdp_typing_fallback', error=type(exc).__name__)
                            if not await driver.confirm_focus(target, page, composer):
                                raise InputNotSubmitted('window_focus_lost')
                            await driver.clear_composer(page, composer)
                            await driver.copy_prompt_and_paste(page, composer, target, prompt)
                # Only poll; never repeat a partially entered prompt.
                deadline = time.monotonic() + 2
                while (await driver.composer_text(page, composer)).replace('\r\n', '\n') != prompt:
                    if time.monotonic() >= deadline:
                        raise InputNotSubmitted('pasted_text_mismatch')
                    if not await driver.confirm_focus(target, page, composer):
                        raise InputNotSubmitted('window_focus_lost')
                    await asyncio.sleep(.05)
                if not await driver.confirm_focus(target, page, composer):
                    raise InputNotSubmitted('window_focus_lost')
                from pointer_sequence import record_input_action
                record_input_action(target, 'typed_text_verified' if typed_on_device else
                                   ('hid_clipboard_paste_verified' if hid_backend else
                                    ('cdp_typed_verified' if mode == 'cdp_type' else 'pasted_text_verified')),
                                   characters=len(prompt))
            # Must durably mark the boundary before invoking the submit action.
            await checkpoint()
    except InputNotSubmitted:
        raise
    except Exception as exc:
        import re
        reason=str(exc)
        reason=reason if re.fullmatch(r'[a-z_]{3,80}',reason) else type(exc).__name__
        record('input_preparation_stopped',reason=reason)
        raise InputNotSubmitted('pre_submit_failed:'+reason) from exc
    try:
        # The checkpoint may have persisted a submission boundary, so a focus
        # loss from here on is conservatively recorded as an unknown outcome.
        if not await driver.confirm_focus(target, page, composer):
            raise InputOutcomeUnknown('window_focus_lost_after_checkpoint')
        await submit()
        try:
            pacing_path = Path.home() / '.local/state/creative-unified-workbench/submit-pacing.json'
            try:
                state = json.loads(pacing_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                state = {}
            accounts = state.get('per_account') or {}
            accounts[target.account_key] = time.time()
            accounts = {k: v for k, v in accounts.items() if time.time() - v < 86400}
            pacing_path.parent.mkdir(parents=True, exist_ok=True)
            pacing_path.write_text(json.dumps({'last': time.time(), 'per_account': accounts}), encoding='utf-8')
        except OSError:
            pass
        from pointer_sequence import record_input_action
        record_input_action(target, 'send_action_completed')
    except BaseException as exc:
        # Includes cancellation after the ambiguous boundary.
        raise InputOutcomeUnknown('submission_outcome_unknown') from exc
