// Opt-in local browser verification. Actual role accounts, model and BigQuery.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const credentials = JSON.parse(fs.readFileSync('analytics_service/profile-logins.local.json', 'utf8'));
const root = 'http://127.0.0.1:8012';
(async () => {
  const browser = await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(root + '/ui/');
    const jobs = [];
    assert(page.url().endsWith('/login'));
    await page.screenshot({path:'/private/tmp/aml-login-desktop.png',fullPage:true});
    for (const [role, question] of [['monitoring','Show transaction count grouped by direction, with one row per direction.'], ['investigation','Count risk case events grouped by type.'], ['admin','Show transaction count grouped by direction, with one row per direction.']]) {
      await page.locator('#username').fill(credentials[role].username);
      await page.locator('#password').fill(credentials[role].password);
      await page.locator('#sign-in').click();
      await page.waitForFunction(() => document.querySelector('#status')?.textContent === 'Connected', null, {timeout:60000});
      assert.equal(await page.locator('#password').count(),0);
      assert.match(await page.locator('#profile-link').innerText(), new RegExp(role,'i'));
      await page.locator('#profile-link').click();
      await page.locator('#profile-content').waitFor({state:'visible'});
      const tables = await page.locator('#profile-tables').innerText();
      if (role === 'monitoring') { assert(tables.includes('Transaction')); assert(!tables.includes('RiskCaseEvent')); }
      else if (role === 'investigation') { assert(tables.includes('RiskCaseEvent')); assert(!tables.includes('Transaction')); }
      else { assert(tables.includes('Transaction')); assert(tables.includes('RiskCaseEvent')); }
      assert.equal(await page.locator('#profile-tables li').count(),{monitoring:7,investigation:4,admin:10}[role]);
      assert.equal(await page.locator('#profile-countries tr').count(),7);
      assert(!tables.includes('ExportedMetadata'));
      for (const [width,height,label] of [[1440,1000,'desktop'],[390,844,'mobile']]) {
        await page.setViewportSize({width,height});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path:`/private/tmp/aml-profile-${role}-${label}.png`,fullPage:true});
      }
      await page.locator('.main-nav a[href="/ui/"]').click();
      await page.waitForFunction(() => document.querySelector('#status')?.textContent === 'Connected',null,{timeout:60000});
      const forbiddenQuestion = {monitoring:'Count risk case events',investigation:'Count transactions',admin:'Show ExportedMetadata'}[role];
      await page.locator('#question').fill(forbiddenQuestion);
      const forbiddenReply = page.waitForResponse(r => r.url().endsWith('/query'));
      await page.locator('#run').click();
      assert.equal((await forbiddenReply).status(),403);
      await page.waitForFunction(() => document.querySelector('#message').textContent.includes('You do not have the required access'));
      await page.locator('#question').fill(question);
      if ((await page.locator('#save-question').getAttribute('aria-label')) === 'Save question to FAQ') await page.locator('#save-question').click();
      await page.locator('#question-library > summary').click();
      await page.waitForFunction(q => [...document.querySelectorAll('#saved-questions .question-link')].some(e => e.textContent === q), question);
      if (role === 'investigation') assert(!(await page.locator('#saved-questions').innerText()).includes('transaction count grouped'));
      await page.locator('#question-library > summary').click();
      const reply = page.waitForResponse(r => r.url().endsWith('/query'), {timeout:300000});
      await page.locator('#run').click();
      const response = await reply;
      assert.equal(response.status(),200,await response.text());
      const result = await response.json();
      assert.equal(result.mode,'bigquery');
      jobs.push({role,job:result.job_id,rows:result.rows.length});
      await page.waitForFunction(() => document.querySelector('#run').textContent === 'Analyze');
      await page.locator('#question-library > summary').click();
      await page.locator('#history-tab').click();
      await page.waitForFunction(q => document.querySelector('#history-questions').textContent.includes(q), question);
      if (role === 'investigation') assert(!(await page.locator('#history-questions').innerText()).includes('transaction count grouped'));
      await page.locator('#history-questions .question-link').first().click();
      assert.equal(await page.locator('#question').inputValue(),question);
      const denied = await page.request.post(root+'/query', {headers:{'X-AML-Request':'1'},data:{question,scope:role==='monitoring'?'investigation':'monitoring'}});
      assert.equal(denied.status(),422);
      for (const [width,height,label] of [[1440,1000,'desktop'],[390,844,'mobile']]) {
        await page.setViewportSize({width,height});
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path:`/private/tmp/aml-${role}-${label}.png`,fullPage:true});
      }
      if (role === 'admin') {
        const caseReply = await page.request.post(root+'/query',{headers:{'X-AML-Request':'1'},data:{question:'Count risk case events grouped by type.'},timeout:300000});
        assert.equal(caseReply.status(),200,await caseReply.text());
        const cases = await caseReply.json();
        assert.equal(cases.mode,'bigquery');
        jobs.push({role,job:cases.job_id,rows:cases.rows.length});
      }
      await page.locator('#profile-link').click();
      await page.locator('#sign-out').click();
      await page.waitForURL('**/login');
      assert.equal((await page.request.get(root+'/workspace')).status(),401);
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed',jobs,checks:'separate login, three profiles, table/country lists, clear permission errors, Admin union, FAQ save, private history, reuse, role spoof rejection, logout, desktop/mobile'}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
