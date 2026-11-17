const ui = require(process.env.FACTORY_UI);
ui.run(async (t) => {
  await t.step('the real Tauri app opened', async () => {
    if (!t.desktop || !t.desktop.real) throw new Error('not the real app');
    await t.page.waitForSelector('#mode:text("desktop")', { timeout: 15000 });
    const before = await t.page.evaluate(() => localStorage.getItem('kept'));
    if (before !== null) throw new Error('not a fresh start: ' + before);
  });
  await t.step('its Rust side answers', async () => {
    await t.page.waitForSelector('#greet:text("Hello, Linh!")', { timeout: 15000 });
    await t.page.waitForSelector('#err:text("error disk full")', { timeout: 15000 });
    await t.page.click('#again');
    await t.page.waitForSelector('#greet:text("Hello, again!")', { timeout: 15000 });
    const c = t.ipc.calls('greet');
    if (!c.some((x) => x.args && x.args.name === 'again')) throw new Error('greet calls seen: ' + JSON.stringify(c));
  });
  await t.step('an event reaches the page', async () => {
    await t.ipc.emit('ping', 'pong');
    await t.page.waitForSelector('#sync:text("got pong")', { timeout: 15000 });
  });
  await t.page.evaluate(() => localStorage.setItem('kept', 'yes'));
  await t.snap('main');
  await t.restart();
  await t.step('its data is kept after a restart', async () => {
    const v = await t.page.evaluate(() => localStorage.getItem('kept'));
    if (v !== 'yes') throw new Error('after the restart: ' + v);
  });
});
