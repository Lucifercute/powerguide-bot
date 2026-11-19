"""Local Chrome export in a disposable profile, with no remote content access."""
import asyncio
import base64
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from zipfile import ZipFile

import aiohttp
from lxml import etree
from pptx import Presentation
from .composition import composition_issues
from .layout import MIN_FONT_PX, layout_issues, slide_issues


def chrome_path():
    configured = os.getenv('EDITABLE_CHROME_PATH')
    candidates = [configured] if configured else [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        *(str(Path(os.environ.get(v, '')) / 'Google/Chrome/Application/chrome.exe')
          for v in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if os.environ.get(v)),
        shutil.which('google-chrome'), shutil.which('chromium'), shutil.which('chromium-browser')]
    for p in candidates:
        if p and Path(p).is_file():
            return p
    raise RuntimeError('Chrome для экспорта не найден: настройте EDITABLE_CHROME_PATH')


class CDP:
    def __init__(self, socket):
        self.socket, self.index = socket, 0

    async def call(self, method, params=None):
        self.index += 1
        await self.socket.send_json({'id': self.index, 'method': method, 'params': params or {}})
        async with asyncio.timeout(90):
            while True:
                value = await self.socket.receive_json()
                if value.get('id') != self.index:
                    continue
                if 'error' in value:
                    raise RuntimeError(f'Chrome: {method}: {value["error"]}')
                return value.get('result', {})

    async def evaluate(self, expression):
        r = await self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True})
        if r.get('exceptionDetails'):
            raise RuntimeError('Ошибка HTML-экспорта: ' + json.dumps(r['exceptionDetails'], ensure_ascii=False)[:1500])
        return r['result'].get('value')


def validate_pptx(data, count, width, height):
    with ZipFile(io.BytesIO(data)) as z:
        if z.testzip():
            raise ValueError('Повреждённый PPTX')
        for name in z.namelist():
            if name.endswith(('.xml', '.rels')):
                root = etree.fromstring(z.read(name))
                if root.xpath('//*[local-name()="prstGeom"][@prst="ellipse"]/*[local-name()="avLst"]/*'):
                    raise RuntimeError('Экспортёр добавил неподдерживаемую настройку геометрии эллипса')
    p = Presentation(io.BytesIO(data))
    if len(p.slides) != count or abs(p.slide_width / p.slide_height - width / height) > .001:
        raise ValueError('Размер или количество слайдов PPTX не соответствует запросу')
    if any(not any(s.has_text_frame and s.text.strip() for s in slide.shapes) for slide in p.slides):
        raise ValueError('Потерян редактируемый текст одного из слайдов')


async def render(source: Path, output: Path, count: int, width: int, height: int, *, previews=False):
    """No API calls. On failure keep HTML; publish PPTX only after validation."""
    with tempfile.TemporaryDirectory(prefix='powerguide-export-', ignore_cleanup_errors=True) as profile:
        process = await asyncio.create_subprocess_exec(
            chrome_path(), '--headless=new', '--no-first-run', '--no-default-browser-check',
            '--disable-background-networking', '--disable-extensions', '--disable-sync',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost',
            '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            portfile = Path(profile) / 'DevToolsActivePort'
            async with asyncio.timeout(20):
                while not portfile.exists():
                    if process.returncode is not None:
                        raise RuntimeError('Chrome не запустился')
                    await asyncio.sleep(.1)
            port = int(portfile.read_text().splitlines()[0])
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(f'http://127.0.0.1:{port}/json/list') as response:
                    targets = await response.json()
                target = next(p for p in targets if p.get('type') == 'page')
                async with session.ws_connect(target['webSocketDebuggerUrl'], max_msg_size=100*1024*1024) as socket:
                    cdp = CDP(socket)
                    await cdp.call('Page.enable')
                    await cdp.call('Network.enable')
                    await cdp.call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*', 'ftp://*']})
                    await cdp.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})
                    await cdp.call('Page.navigate', {'url': source.resolve().as_uri()})
                    async with asyncio.timeout(20):
                        while not await cdp.evaluate('document.readyState==="complete" && !!globalThis.PowerGuidePptx'):
                            await asyncio.sleep(.1)
                    result = await cdp.evaluate('''(async()=>{
                      if([...document.images].some(i=>!i.complete||!i.naturalWidth))throw Error('Broken image');
                      const cfg=POWERGUIDE_EXPORT_CONFIG, minFont=''' + str(MIN_FONT_PX) + '''*cfg.width/1600;
                      const scene=await PowerGuidePptx.collectSlides({minFont});
                      const result=PowerGuidePptx.buildPptx(scene,POWERGUIDE_PPTX_TEMPLATE);
                      let binary='';for(let i=0;i<result.bytes.length;i+=32768)binary+=String.fromCharCode(...result.bytes.subarray(i,i+32768));
                      return {pptx:btoa(binary),counters:result.counters,
                        composition:scene.map(s=>s.composition),
                        fonts:[...new Set(scene.flatMap(s=>s.objects.filter(o=>o.type==='text').map(o=>o.font)))],
                        backgroundCrops:scene.map(s=>s.objects.find(o=>o.type==='image')?.crop),
                        textBounds:scene.map((s,i)=>({slide:i+1,issues:s.objects.filter(o=>o.type==='text'&&(o.x< -3||o.y< -3||o.x+o.w>cfg.width+6||o.y+o.h>cfg.height+6)).map(o=>o.text)})),
                        smallText:scene.map((s,i)=>({slide:i+1,items:s.objects.filter(o=>o.type==='text'&&o.fontSize<minFont-.5).map(o=>[o.text.slice(0,40),Math.round(o.fontSize)])})),
                        brokenWords:scene.map((s,i)=>({slide:i+1,items:s.objects.filter(o=>o.type==='text'&&o.broken).map(o=>o.text.slice(0,60))})),
                        wordFit:scene.map((s,i)=>({slide:i+1,...s.fit})),
                        collisions:scene.map((s,i)=>({slide:i+1,...s.collisions})),
                        layout:scene.map(s=>s.layout)};
                    })()''')
                    data = base64.b64decode(result.pop('pptx'))
                    result['compositionIssues'] = composition_issues(result.get('composition', []))
                    result['layoutIssues'] = layout_issues(result.get('layout', []))
                    # Quality findings are hints per slide; the caller decides whether to redo them.
                    result['issues'] = slide_issues(result)
                    (source.parent / 'export-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                                     encoding='utf-8')
                    await asyncio.to_thread(validate_pptx, data, count, width, height)
                    tmp = output.with_suffix('.pptx.tmp')
                    tmp.write_bytes(data)
                    tmp.replace(output)
                    if previews:
                        await cdp.evaluate('''(()=>{
                          document.querySelectorAll('#pg-nav,#export-editable-pptx').forEach(e=>e.style.display='none');
                          const slides=[...document.querySelectorAll('section.slide')];
                          globalThis.previewDisplay=getComputedStyle(slides.find(s=>getComputedStyle(s).display!=='none')).display;
                        })()''')
                        for index in range(count):
                            await cdp.evaluate(f'''(()=>{{
                              [...document.querySelectorAll('section.slide')].forEach((s,i)=>s.style.setProperty('display',i==={index}?previewDisplay:'none','important'));
                            }})()''')
                            shot = await cdp.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
                            (source.parent / f'preview-slide-{index + 1}.png').write_bytes(base64.b64decode(shot['data']))
                    return result
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
