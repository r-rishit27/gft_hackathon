// Opt-in local browser check: real authentication, model inference and BigQuery.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:8012/ui/');
    const login = JSON.parse(fs.readFileSync(path.resolve('analytics_service/profile-logins.local.json'), 'utf8')).monitoring;
    await page.locator('#username').fill(login.username);
    await page.locator('#password').fill(login.password);
    await page.locator('#sign-in').click();
    await page.waitForFunction(() => document.querySelector('#status')?.textContent === 'Connected', null, {timeout: 60000});
    assert.equal(await page.locator('#password').count(), 0);
    await page.locator('#question').fill('Show transaction count grouped by direction, with one row per direction.');
    const response = page.waitForResponse(r => r.url().endsWith('/query'), {timeout: 300000});
    await page.locator('#run').click();
    const reply = await response;
    assert.equal(reply.status(), 200, await reply.text());
    const result = await reply.json();
    assert.equal(result.mode, 'bigquery');
    assert.equal(result.synthetic, true);
    await page.locator('#results').waitFor({state: 'visible'});
    await page.locator('#run').waitFor({state: 'visible'});
    for (const [width, height, name] of [[1440, 1000, 'desktop'], [390, 844, 'mobile']]) {
      await page.setViewportSize({width, height});
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.waitForFunction(() => document.querySelector('canvas')?.width > 0);
      await page.screenshot({path: `/private/tmp/aml-stakeholder-${name}.png`, fullPage: true});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1).map(e => ({tag:e.tagName,id:e.id,class:e.className,width:e.getBoundingClientRect().width})))));
      const painted = await page.evaluate(() => {
        const canvas = document.querySelector('canvas');
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        let colored = 0;
        for (let i = 0; i < pixels.length; i += 4) if (pixels[i + 3] > 0 && Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) - Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) > 30) colored++;
        return colored;
      });
      assert(painted > 100, `Blank chart at ${name}`);
      await page.screenshot({path: `/private/tmp/aml-stakeholder-${name}.png`, fullPage: true});
    }
    await page.locator('#table-tab').click();
    assert(await page.locator('#overview-panel').isHidden());
    assert.equal(await page.locator('#rows tr').count(), result.rows.length);
    assert.match(await page.locator('#rows').innerText(), /13,035/);
    assert.match(await page.locator('#rows').innerText(), /36,965/);
    assert.equal(await page.locator('.provenance').getAttribute('open'), null);
    assert(!/Ollama|BigQuery|Bytes scanned|Schema/.test(await page.locator('body').innerText()));

    // Isolated rendering edge cases only; never served as runtime results.
    await page.evaluate(base => {
      clearResults();
      render({...base, truncated: true, dashboard: {charts: [], insights: []}});
    }, result);
    assert(await page.locator('#overview-tab').isHidden());
    assert.match(await page.locator('#warnings').innerText(), /Incomplete result/);
    await page.evaluate(base => {
      clearResults();
      render({...base, columns: [{name:'party_id', type:'STRING'}], rows: Array.from({length: 26}, (_, i) => ({party_id: `TEST_${i}`})), dashboard: {charts: [], insights: []}});
    }, result);
    assert.equal(await page.locator('#rows tr').count(), 25);
    await page.locator('#next-page').click();
    assert.equal(await page.locator('#rows tr').count(), 1);
    assert.match(await page.locator('#page-label').innerText(), /26.*26 of 26/);
    assert.equal(await page.evaluate(() => formatValue('12345678901234567890.123456789')), '12,345,678,901,234,567,890.123456789');
    await page.evaluate(base => {clearResults(); render({...base, rows: [], dashboard: {charts: [], insights: ['No matching records were returned by BigQuery.']}});}, result);
    assert.match(await page.locator('#insights').innerText(), /No matching records for this question/);
    await page.locator('#profile-link').click();
    await page.locator('#sign-out').click();
    await page.waitForURL('**/login');
    assert.equal((await page.request.get('http://127.0.0.1:8012/auth/me')).status(), 401);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({status:'passed', job:result.job_id, rows:result.rows, screenshots:['/private/tmp/aml-stakeholder-desktop.png','/private/tmp/aml-stakeholder-mobile.png']}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
