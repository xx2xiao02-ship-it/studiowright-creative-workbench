"""Submit Flow prompts using the caller's verified account and desktop window."""
import asyncio
import time
from foreground_input import paste_and_submit, paste_and_submit_reserved


async def flow_submit(*, page, composer, submit_button, credits_warning,
                      target, prompt, queue, driver, on_submit=None, reserved=False):
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
        # An eased in-page trajectory instead of an instant synthetic teleport.
        box = await submit_button.bounding_box()
        if not box:
            await submit_button.click()
            return
        import random
        target_x = box['x'] + box['width'] * random.uniform(.4, .6)
        target_y = box['y'] + box['height'] / 2
        cursor_x = target_x - random.uniform(90, 160)
        cursor_y = target_y - random.uniform(50, 110)
        steps = random.randint(9, 14)
        for index in range(1, steps + 1):
            t = index / steps
            ease = t * t * (3 - 2 * t)
            await page.mouse.move(cursor_x + (target_x - cursor_x) * ease + random.uniform(-2, 2),
                                  cursor_y + (target_y - cursor_y) * ease + random.uniform(-2, 2))
            await asyncio.sleep(random.uniform(.02, .05))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(.06, .14))
        await page.mouse.up()
    kwargs = dict(driver=driver, page=page, composer=composer, target=target,
                  prompt=prompt, checkpoint=checkpoint, submit=submit, hybrid=True)
    if reserved:
        return await paste_and_submit_reserved(**kwargs)
    return await paste_and_submit(queue=queue, **kwargs)
