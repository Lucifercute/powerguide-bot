"""Replay an approved local session through the bot's real API pipeline in isolation.

Usage: .venv/bin/python -m scripts.test_editable_session --session 37 --output snapshots/example
Existing output resumes saved stages; use a new output directory for a fresh experiment.
No Telegram delivery, production DB writes or hand-authored model output.
"""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

from lxml import html as html_parser

from bot.config import load_config
from bot.db import Database
from bot.editable.browser import render
from bot.editable.service import EditableService, sha, write
from bot.llm import TextLLM, ImageLLM
from bot.pipeline import PLAN, TZ, DATA, STYLE, STYLE_PRESET, BACKGROUND, BACKGROUND_ALT, BACKGROUND_CHOICE, BACKGROUND_ACCEPTED


async def main(args):
    config=load_config();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect('file:'+str(config.db_path.resolve())+'?mode=ro',uri=True) as source:
        artifacts=source.execute('select kind,content,model from artifacts where session_id=?',(args.session,)).fetchall()
        answers=source.execute('select question_key,question_text,answer from answers where session_id=?',(args.session,)).fetchall()
    allowed={PLAN.kind,TZ.kind,DATA.kind,STYLE,STYLE_PRESET,BACKGROUND,BACKGROUND_ALT,BACKGROUND_CHOICE,BACKGROUND_ACCEPTED}
    artifacts=[row for row in artifacts if row[0] in allowed or row[0].startswith('user_image_')]
    if not any(row[0]==PLAN.kind for row in artifacts):raise ValueError('Approved source plan not found')
    manifest={'source_session':args.session,'source_db':str(config.db_path.resolve()),
              'artifacts_sha256':sha(json.dumps(artifacts,ensure_ascii=False)),
              'answers_sha256':sha(json.dumps(answers,ensure_ascii=False))}
    mp=out/'source-manifest.json'
    if mp.exists() and json.loads(mp.read_text())!=manifest:raise ValueError('Source session changed; use a new output directory')
    write(mp,manifest)
    db=Database(out/'test.db');await db.connect()
    await db.conn.execute('INSERT OR IGNORE INTO sessions(id,user_id) VALUES (?,0)',(args.session,));await db.conn.commit()
    for kind,content,model in artifacts:await db.save_artifact(args.session,kind,content,model)
    for key,question,answer in answers:await db.save_answer(args.session,key,question,answer)
    llm=TextLLM(config.gemini_api_key,config.gemini_text_models,config.vertex_api_key,config.vertex_text_models)
    images=ImageLLM(config.vertex_api_key,config.image_models)
    started=time.time()
    state=out/'run-state.json';write(state,{'status':'running','pid':os.getpid(),'source_session':args.session})
    async def status(text):print(text,flush=True)
    try:
        baseline_source=config.files_dir/str(args.session)/'editable/presentation.html'
        if args.audit_baseline and baseline_source.exists() and not (out/'baseline/export-check.json').exists():
            baseline=out/'baseline';baseline.mkdir(exist_ok=True)
            doc=html_parser.document_fromstring(baseline_source.read_text())
            scripts=[s for s in doc.xpath('//script') if 'function collectSlides()' in (s.text or '')]
            if len(scripts)!=1:raise ValueError('Cannot find the baseline exporter')
            scripts[0].text=Path('bot/editable/export-pptx.js').read_text()
            (baseline/'presentation.html').write_text('<!DOCTYPE html>\n'+html_parser.tostring(doc,encoding='unicode'))
            meta=json.loads((baseline_source.parent/'completed.json').read_text())
            try:await render(baseline/'presentation.html',baseline/'presentation.pptx',meta['slides'],meta['width'],meta['height'])
            except ValueError as e:write(baseline/'validation.txt',str(e));print('Baseline:',str(e)[:250],flush=True)
        service=EditableService(llm,images,config.prompts_dir,presets_dir=config.presets_dir)
        source,output=await service.generate(db,args.session,out/'data/sessions',status)
        await db.finish_session(args.session)
        write(state,{'status':'completed','source_session':args.session,'html':str(source),'pptx':str(output),
                     'seconds':round(time.time()-started,1)})
        print('HTML:',source,flush=True);print('PPTX:',output,flush=True)
    except Exception as e:
        write(state,{'status':'failed','source_session':args.session,'error':type(e).__name__,'message':str(e)[:2500]})
        raise
    finally:
        await db.close()
        clients={id(t.client):t.client for t in llm.targets+images.targets};clients[id(llm.client)]=llm.client
        for client in clients.values():await client.aio.aclose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--session',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--audit-baseline',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('google_genai.models').setLevel(logging.WARNING)
    with (args.output/'.run.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        asyncio.run(main(args))
