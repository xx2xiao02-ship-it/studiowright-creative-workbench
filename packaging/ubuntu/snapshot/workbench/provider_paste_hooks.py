"""Submit Flow prompts using the caller's verified account and desktop window."""
import asyncio
import time
from foreground_input import paste_and_submit, paste_and_submit_reserved


async def flow_submit(*, page, composer, submit_button, credits_warning,
                      target, prompt, queue, driver, on_submit=None, reserved=False, on_click=None):
    async def checkpoint():
        deadline = time.monotonic() + 5
        while await submit_button.is_disabled():
            if await credits_warning.count():
                raise ValueError('flow_insufficient_credits')
            if time.monotonic() >= deadline:
                raise ValueError('flow_submit_disabled')
            await asyncio.sleep(.05)
        if on_submit:
            on_submit()

    async def submit():
        if on_click is not None:
            on_click()
        await submit_button.click()

    kwargs = dict(driver=driver, page=page, composer=composer, target=target,
                  prompt=prompt, checkpoint=checkpoint, submit=submit, hybrid=True)
    if reserved:
        return await paste_and_submit_reserved(**kwargs)
    return await paste_and_submit(queue=queue, **kwargs)
