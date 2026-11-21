// The same check as a website and as the app's window (in the box), then as the real app (on Windows).
const ui = require(process.env.FACTORY_UI);
ui.run(async (t) => {
  const kind = !t.desktop ? 'web' : t.desktop.real ? 'app' : 'desktop';
  await t.step(kind + ': the page opened', async () => {
    if (kind !== 'app') await t.goto(process.env.BASE_URL + '/');
    await t.page.waitForSelector('#mode:text("' + (kind === 'web' ? 'web' : 'desktop') + '")', { timeout: 15000 });
  });
  await t.step(kind + ': the API answered', async () => {
    await t.page.waitForSelector('#greeting:text("hello from the API")', { timeout: 30000 });
  });
  const text = await t.page.textContent('#greeting');
  const api = await t.page.textContent('#api');
  console.log('[' + kind + '] ' + api + ' | ' + text);
  await t.snap('main-' + kind, { a11y: false });
});
