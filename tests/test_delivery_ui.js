// Run against the local preview; update requests are intercepted, never billed.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.env.RADAR_PREVIEW_URL||'http://127.0.0.1:8768';
(async()=>{
    const browser=await chromium.launch({headless:true,channel:'msedge'});
    try {
        const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'}),errors=[];
        page.on('pageerror',e=>errors.push(e.message));
        await page.goto(base);
        await page.locator('.entry-summary').waitFor();
        assert.equal(await page.getByRole('button',{name:'更新研究',exact:true}).count(),1);
        assert.equal(await page.locator('#overview .scan-strip').count(),0);
        await page.screenshot({path:'artifacts/delivery-desktop.png'});
        for(const width of [1440,1024,768,375]){
            await page.setViewportSize({width,height:1050});
            for(const title of ['今日判断','板块机会','个股研究','我的自选','复盘与历史']){
                await page.getByRole('navigation').getByRole('button',{name:title,exact:true}).click();
                assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width}: ${title} overflow`);
            }
        }
        await page.getByRole('navigation').getByRole('button',{name:'今日判断',exact:true}).click();
        await page.screenshot({path:'artifacts/delivery-mobile.png'});
        await page.getByRole('button',{name:'数据与设置',exact:true}).click();
        assert.equal(await page.locator('#run').isVisible(),false);
        assert.equal(await page.locator('#observations-update').isVisible(),false);
        await page.locator('#settings-close').click();
        await page.locator('#update-open').click();
        assert.equal(await page.getByRole('radio').count(),3);
        await page.getByRole('radio',{name:/补齐所选交易日研究/}).check();
        assert.equal(await page.locator('#update-date').inputValue(),'2026-09-30');
        assert.ok(await page.locator('#update-scope').textContent().then(t=>t.includes('23:59')));
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await page.screenshot({path:'artifacts/update-options-mobile.png'});
        await page.getByRole('radio',{name:/更新当前研究/}).check();
        const state=await (await page.request.get(base+'/api/status')).json();
        let updates=0;
        await page.route('**/api/status',route=>route.fulfill({json:state}));
        await page.route('**/api/run',async route=>{
            updates++;
            assert.deepEqual(route.request().postDataJSON(),{date:'',mode:'current',refresh:false});
            state.running=true;state.stage='正在更新海外行情与事件';
            state.elapsed_seconds=82;state.progress={completed:1,total:4,steps:['检查行情','补充证据','分析与核验','保存结果']};
            await route.fulfill({json:{ok:true}});
        });
        await page.locator('#update-start').click();
        await page.getByText('正在更新海外行情与事件 · 可以继续阅读上次结果',{exact:true}).waitFor();
        assert.equal(updates,1);
        assert.equal(await page.locator('#update-open').isDisabled(),true);
        assert.equal(await page.locator('.entry-summary').isVisible(),true);
        assert.equal(await page.locator('#research-progress').getAttribute('value'),'1');
        assert.ok((await page.locator('#progress-time').textContent()).includes('1分22秒'));
        await page.setViewportSize({width:1440,height:1050});
        await page.screenshot({path:'artifacts/update-progress-desktop.png'});
        state.running=false;state.exit_code=1;
        await page.evaluate(()=>refresh());
        assert.equal(await page.locator('#research-progress').getAttribute('value'),'1');
        assert.equal(await page.locator('#update-open').isDisabled(),false);
        assert.ok((await page.locator('#update-progress').textContent()).includes('保留原研究'));
        const visible=await page.locator('body').innerText();
        assert.ok(!visible.includes('供应商'));
        assert.deepEqual(errors,[]);
        console.log('Delivery UI: 20 responsive views, three update modes, stage progress/elapsed time, duplicate lock and failure preservation passed.');
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
