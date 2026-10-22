"""Validate declared, desensitized DOM samples without executing or fetching them."""
import argparse
from datetime import date
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote

class FixtureError(ValueError):pass

PSEUDONYM=re.compile(r'^(?:合成|匿名|测试|假名|示例|synthetic|fixture|placeholder|sample)[\w\s.:-]*$',re.I)
PRIVATE=re.compile(r'cookie|token|[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}|\d{7,}',re.I)
EXTERNAL=re.compile(r'https?://|//[A-Za-z0-9]|(?:file|javascript):',re.I)
REAL_CLAIM=re.compile(r'["\']?source["\']?\s*[:=]\s*["\']?real\b',re.I)
ROW_TYPES={'with_streak','no_streak','unread','streak_unread','ending','list','有火花','无火花','未读','火花及未读','即将中断','列表'}
UI_TEXT={'消息','私信','通知','朋友私信','未读','无火花','火花','天','即将中断','续火花','已经中断','加载中','搜索'}

def _safe(path):
    info=path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise FixtureError('fixture link/reparse point rejected: '+path.name)

def _private_text(text,name):
    decoded=unescape(unquote(unquote(text)))
    # A namespace is not a resource request. Every other external URI is rejected.
    decoded=decoded.replace('http://www.w3.org/2000/svg','')
    for number,line in enumerate(decoded.splitlines(),1):
        if PRIVATE.search(line):raise FixtureError(f'{name}:{number}: possible private data')
        if EXTERNAL.search(line):raise FixtureError(f'{name}:{number}: external reference')

class _RowParser(HTMLParser):
    def handle_starttag(self,tag,attrs):
        if tag.lower() in {'script','iframe','object','embed'}:raise FixtureError('row.html: executable content rejected')
        for key,value in attrs:
            value=value or ''
            if key.lower().startswith('on'):raise FixtureError('row.html: event handler rejected')
            if key.lower() in {'src','href','xlink:href'} and value and not value.startswith('#'):
                if not re.fullmatch(r'(?:placeholder|synthetic|fixture)[\w./-]*\.(?:svg|png|jpg|webp)',value,re.I):
                    raise FixtureError('row.html: resource must use a local placeholder filename')
            if key.lower() in {'user_id','uid','sec_uid','short_id','data-user-id','data-uid','data-sec-uid','data-short-id'}:
                if value and not PSEUDONYM.fullmatch(value):raise FixtureError('row.html: personal ID must be a placeholder')
    def handle_data(self,data):
        text=data.strip()
        if not text or not any(char.isalnum() for char in text):return
        if text in UI_TEXT or PSEUDONYM.fullmatch(text) or re.fullmatch(r'(?:🔥\s*)?[0-9]{1,3}(?:\s*天)?',text):return
        raise FixtureError('row.html: visible content must use explicit pseudonyms/placeholders')

def check_fixtures(root):
    root=Path(root).absolute();_safe(root)
    for filename in ('synthetic.html','expected.json'):
        path=root/filename
        if path.exists():
            _safe(path)
            if REAL_CLAIM.search(path.read_text(encoding='utf-8')):
                raise FixtureError(filename+': synthetic file cannot claim real provenance')
    real=root/'real'
    if not real.exists():return {'real_samples':0,'message':'尚无真实样本','platform_dom_verified':False}
    _safe(real)
    count=0
    for directory in sorted(real.iterdir()):
        _safe(directory)
        if not directory.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}-[A-Za-z0-9_-]+',directory.name):
            raise FixtureError('real fixture directory must be date-number')
        if {item.name for item in directory.iterdir()} != {'meta.json','row.html'}:
            raise FixtureError(directory.name+': exactly meta.json and row.html required')
        for filename in ('meta.json','row.html'):_safe(directory/filename)
        raw=(directory/'meta.json').read_text(encoding='utf-8');_private_text(raw,'meta.json')
        try:meta=json.loads(raw)
        except ValueError as error:raise FixtureError('meta.json: invalid JSON') from error
        if not isinstance(meta,dict) or set(meta)!={'source','collected_at','row_type','expected'}:
            raise FixtureError('meta.json: required schema fields are source/collected_at/row_type/expected')
        try:collected=date.fromisoformat(meta['collected_at'])
        except (TypeError,ValueError) as error:raise FixtureError('meta.json: invalid collection date') from error
        if meta['source']!='real' or collected.isoformat()!=directory.name[:10] or not isinstance(meta['row_type'],str) or meta['row_type'] not in ROW_TYPES:
            raise FixtureError('meta.json: source/date/row type mismatch')
        expected=meta['expected']
        if not isinstance(expected,dict):raise FixtureError('meta.json: expected must be an object')
        if meta['row_type'] not in {'list','列表'}:
            if set(expected)!={'name','streak_status','streak_days'} or not PSEUDONYM.fullmatch(str(expected.get('name',''))):
                raise FixtureError('meta.json: expected name must be an explicit pseudonym')
            state,days=expected['streak_status'],expected['streak_days']
            if not isinstance(state,str) or state not in {'confirmed','none','unknown','suspect'}:raise FixtureError('meta.json: invalid expected state')
            if state=='confirmed' and (type(days) is not int or not 1<=days<=999):raise FixtureError('meta.json: invalid confirmed days')
            if state=='none' and (type(days) is not int or days!=0):raise FixtureError('meta.json: none must be zero')
            if state in {'unknown','suspect'} and days is not None:raise FixtureError('meta.json: unknown/suspect must have no current value')
        elif set(expected)!={'rows'} or type(expected['rows']) is not int or expected['rows']<0:
            raise FixtureError('meta.json: list expected must contain a nonnegative rows count')
        html=(directory/'row.html').read_text(encoding='utf-8');_private_text(html,'row.html')
        parser=_RowParser();parser.feed(html);parser.close();count+=1
    return {'real_samples':count,'message':'样本格式与脱敏约束通过；来源真实性和平台选择器仍须人工核验','platform_dom_verified':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures/chat_rows')
    args=parser.parse_args()
    try:print(json.dumps(check_fixtures(args.root),ensure_ascii=False));return 0
    except (FixtureError,OSError,UnicodeError) as error:print('fixture validation failed: '+str(error));return 1

if __name__=='__main__':raise SystemExit(main())
