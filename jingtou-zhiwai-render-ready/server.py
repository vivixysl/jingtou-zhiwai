#!/usr/bin/env python3
"""镜头之外 V0.4: private cloud game; keep OpenAI credentials on the server."""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, unquote
from collections import defaultdict, deque
import time

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(ROOT / 'data'))).resolve()
DB = DATA_DIR / 'world.sqlite3'
AVATAR_DIR = DATA_DIR / 'avatars'
SEED = json.loads((ROOT / 'seed.json').read_text(encoding='utf-8'))
BIBLE = (ROOT / 'STORY_BIBLE.md').read_text(encoding='utf-8')
CHARACTERS = SEED['characters']
PEOPLE = set(CHARACTERS) | set(SEED['npc'])
API_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini').strip()
PIN = os.environ.get('GAME_PIN', '').strip()
PORT = int(os.environ.get('PORT', '4181'))
HOST = os.environ.get('HOST', '0.0.0.0' if PIN else '127.0.0.1')
USE_MOCK = os.environ.get('MOCK_AI_FOR_TESTS') == '1' and HOST == '127.0.0.1'
CLOUD_MODE = os.environ.get('CLOUD_MODE', '0') == '1'
SESSION_SECRET = os.environ.get('SESSION_SECRET', '').encode('utf-8')
SESSION_AGE = 60 * 60 * 24 * 14
LOGIN_ATTEMPTS = defaultdict(deque)
LOGIN_LOCK = Lock()
GEN_LOCK = Lock()
DATE0 = dt.date(2030, 10, 12)
INITIAL_LAST = dt.date(2030, 10, 13)

def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=15000')
    return con

def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS generations(
          date TEXT PRIMARY KEY, model TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, date TEXT NOT NULL, time TEXT NOT NULL,
          title TEXT NOT NULL, summary TEXT NOT NULL, participants TEXT NOT NULL,
          known_by TEXT NOT NULL, parent_ids TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, members TEXT NOT NULL, created_on TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
          id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
          chat_id TEXT NOT NULL, day TEXT NOT NULL, time TEXT NOT NULL,
          sender TEXT NOT NULL, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts(
          id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
          day TEXT NOT NULL, time TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preferences(
          key TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        ''')

def existing_max(con):
    row = con.execute('SELECT MAX(date) AS d FROM generations').fetchone()['d']
    return max(INITIAL_LAST, dt.date.fromisoformat(row)) if row else INITIAL_LAST

def all_batches(con):
    return [json.loads(r['payload']) for r in con.execute('SELECT payload FROM generations ORDER BY date')]

def avatars():
    result = {}
    for person in CHARACTERS:
        for ext in ('png','jpg','jpeg','webp'):
            f = next((f for f in (AVATAR_DIR/f'{person}.{ext}', ROOT/'assets'/'avatars'/f'{person}.{ext}') if f.is_file()), None)
            if f:
                result[person] = f'/assets/avatars/{person}.{ext}?v={int(f.stat().st_mtime_ns)}'
                break
    return result

def state():
    with connect() as con:
        prefs=con.execute("SELECT payload FROM preferences WHERE key='single_player'").fetchone()
        return {'user_state':json.loads(prefs['payload']) if prefs else None,'max_day': (existing_max(con)-DATE0).days,
                'generations': all_batches(con), 'avatars': avatars(),
                'configured': bool(API_KEY or USE_MOCK), 'model':MODEL,
                'pin_enabled':bool(PIN)}

def hhmm(v):
    return isinstance(v, str) and bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', v))

def text_ok(t, cap=500):
    return isinstance(t,str) and 0<len(t.strip())<=cap

def extract_response_text(obj):
    if isinstance(obj.get('output_text'), str):
        return obj['output_text']
    segments=[]
    for item in obj.get('output',[]):
        for chunk in item.get('content', []):
            if chunk.get('type')=='output_text': segments.append(chunk.get('text',''))
    if not segments: raise ValueError('模型没有返回文字内容')
    return ''.join(segments)

EVENT_SCHEMA={
  'type':'object','additionalProperties':False,
  'required':['day_summary','new_chats','events'],
  'properties':{
    'day_summary':{'type':'string'},
    'new_chats':{'type':'array','items':{'type':'object','additionalProperties':False,
      'required':['id','title','members'],
      'properties':{'id':{'type':'string'},'title':{'type':'string'},'members':{'type':'array','items':{'type':'string'}}}}},
    'events':{'type':'array','items':{'type':'object','additionalProperties':False,
      'required':['time','title','summary','participants','known_by','parent_ids','messages','posts'],
      'properties':{
       'time':{'type':'string'},'title':{'type':'string'},'summary':{'type':'string'},
       'participants':{'type':'array','items':{'type':'string'}},
       'known_by':{'type':'array','items':{'type':'string'}},
       'parent_ids':{'type':'array','items':{'type':'string'}},
       'messages':{'type':'array','items':{'type':'object','additionalProperties':False,
          'required':['chat_id','sender','time','text'],
          'properties':{'chat_id':{'type':'string'},'sender':{'type':'string'},'time':{'type':'string'},'text':{'type':'string'}}}},
       'posts':{'type':'array','items':{'type':'object','additionalProperties':False,
          'required':['author','time','text'],
          'properties':{'author':{'type':'string'},'time':{'type':'string'},'text':{'type':'string'}}}}
      }}}
  }
}

def context_for_generation(con, target):
    gen_rows=con.execute('SELECT date,payload FROM generations ORDER BY date DESC LIMIT 6').fetchall()
    past_events=con.execute('SELECT id,date,time,title,summary,participants,known_by,parent_ids FROM events ORDER BY date DESC,time DESC LIMIT 55').fetchall()
    chat_info={k:{'title':c['title'],'members':c['members']} for k,c in SEED['chats'].items()}
    for row in con.execute('SELECT * FROM chats'):
        chat_info[row['id']]={'title':row['title'],'members':json.loads(row['members'])}
    last_chat={}
    for cid,c in SEED['chats'].items():
        last_chat[cid]=c['messages'][-4:]
    for m in con.execute('SELECT chat_id,day,time,sender,body FROM messages ORDER BY day,time'):
        last_chat.setdefault(m['chat_id'],[]).append([m['day'][5:],m['time'],m['sender'],m['body']])
        last_chat[m['chat_id']]=last_chat[m['chat_id']][-6:]
    prior_summaries=[]
    for row in reversed(gen_rows):
        batch=json.loads(row['payload'])
        prior_summaries.append({'date':row['date'],'summary':batch['day_summary']})
    return {'target_day':str(target), 'last_written_day':str(existing_max(con)),
      'characters':{k:{'name':v['name'],'job':v['job']} for k,v in CHARACTERS.items()},
      'npc':{k:v['name'] for k,v in SEED['npc'].items()},
      'chat_directory':chat_info,'last_messages':last_chat,
      'recent_generated_summaries':prior_summaries,
      'event_ledger':[dict(r) for r in reversed(past_events)],
      'recent_seed_notes':{k:SEED['notes'][k][-2:] for k in CHARACTERS},
      'projects':['潮汐来时','第十七页','雾面','飞鸟剧场']}

SYSTEM_PROMPT='''你是一部原创架空娱乐圈捡手机游戏《镜头之外》的严谨世界编剧和事件调度器。输出符合 JSON Schema 的原创虚构内容，不要写任何解释。

绝对规则：
1. 2030架空城市临川，开局两圈独立：丁程鑫(ding)、宋亚轩(song)、贺峻霖(he)是大学闺蜜组；马嘉祺(ma)、刘耀文(liu)、张真源(zhang)、严浩翔(yan)是少年兄弟组。祺鑫、文轩、翔霖必须从陌生人职业接触开始；不能偷造旧恋爱旧好友旧通讯录。10月14日起可由有明确工作原因的正式引荐逐步认识，不能一天让三对都相遇；新聊天必须有引荐/来源。
2. 丁是小演员争取《潮汐来时》试镜，马是该电影影帝；宋编剧《第十七页》，刘导演商业长片；贺经纪人刚谈《雾面》项目，严是歌手；张创作《飞鸟剧场》音乐剧，必须有自主事业情节和角色，不是所有人恋爱军师。
3. 友情、日常工作、吃饭、荒诞琐事等占至少一半；不要所有人说同一种机智金句、不要没有铺垫就狗仔热搜轰炸；不提前让角色知道自己尚未知道的信息。
4. 一天新增约 4-7 个不同事件；每事件包含1-7条自然微信群/私聊消息；一天总体至少16条信息；可加入0-2条微博。群聊中各人能说自己的话，左/右气泡由手机端负责。每句简洁具体，有口语断句，少宏大总结。
5. 必须尊重传入既有事件账本、聊天历史和时间。旧冲突不无故重来，已经交代的决策不能回滚；新事件 parent_ids 可引用已有事件 ID，仅能引用给出的真实 ID，没有则空数组。不要引用当天其他新事件的 ID。
6. 工作日安排符合日期、合理速度；感情张力来自决定及行动后果，而非固定配对发糖。任何公众关系都是此虚构世界，绝不是真实艺人经历。
7. 只使用给出的角色/NPC ID 和已存在的聊天 ID，新建聊天 id 必须以 new_ 开头且只由[a-z0-9_]组成；新职业聊天必须由此日事件合理引荐。当前不允许任何人给真实人发消息或获得现实隐私资料。
8. 每事件 time 是引发事件的最早时间，消息和微博都不能早于事件 time；所有时间为 HH:MM，排序自然且同一天。'''

def ask_openai(context):
    if USE_MOCK:
        return {'day_summary':'一次关于排练、工作和宵夜的普通日子。','new_chats':[], 'events':[
          {'time':'10:11','title':'试镜资料核对','summary':'丁确认试镜档期仍未正式公布，继续排练。','participants':['ding','lin'],'known_by':['ding','lin'],'parent_ids':[],
           'messages':[{'chat_id':'ding_lin','sender':'lin','time':'10:12','text':'正式通知还在走流程，暂时别急。'},{'chat_id':'ding_lin','sender':'ding','time':'10:16','text':'好的林姐，我今天继续练台词。'}], 'posts':[]},
          {'time':'13:05','title':'电影节选片','summary':'兄弟群聊一部老电影。','participants':['ma','liu'],'known_by':['ma','liu','zhang','yan'],'parent_ids':[],
           'messages':[{'chat_id':'boys','sender':'liu','time':'13:06','text':'昨晚那个长镜头是谁推荐的？'},{'chat_id':'boys','sender':'ma','time':'13:09','text':'先把电影名字记对。'}], 'posts':[]},
          {'time':'17:35','title':'打印剧本','summary':'编剧在跑打印店','participants':['song','he'],'known_by':['song','he','ding'],'parent_ids':[],
           'messages':[{'chat_id':'girls','sender':'song','time':'17:36','text':'打印店今天居然没有A4纸'}, {'chat_id':'girls','sender':'he','time':'17:37','text':'那他们有什么'}, {'chat_id':'girls','sender':'song','time':'17:38','text':'态度'}], 'posts':[]}
        ]}
    prompt=json.dumps(context,ensure_ascii=False,separators=(',',':'))
    data={'model':MODEL,'instructions':SYSTEM_PROMPT,
          'input':'请仅为 '+context['target_day']+' 生成新一天的后续世界事件。现有世界状态JSON：\n'+prompt,
          'text':{'format':{'type':'json_schema','name':'found_phone_increment','strict':True,'schema':EVENT_SCHEMA}},
          'store':False,'max_output_tokens':6000}
    req=Request('https://api.openai.com/v1/responses',data=json.dumps(data,ensure_ascii=False).encode('utf-8'),method='POST',headers={
          'Authorization':'Bearer '+API_KEY,'Content-Type':'application/json'})
    try:
        with urlopen(req, timeout=110) as resp:
            obj=json.loads(resp.read(3_500_000))
    except HTTPError as exc:
        detail=exc.read(2500).decode('utf-8','replace')
        raise RuntimeError(f'OpenAI API HTTP {exc.code}: {detail}')
    except URLError as exc:
        raise RuntimeError('OpenAI API 网络连接失败：'+str(exc.reason))
    return json.loads(extract_response_text(obj))

def normalize_generated(raw, target, context):
    if not isinstance(raw,dict): raise ValueError('AI 返回的不是 JSON 对象')
    if not text_ok(raw.get('day_summary'),1500): raise ValueError('缺少当天剧情摘要')
    events=raw.get('events',[])
    if not isinstance(events,list) or not 2<=len(events)<=12: raise ValueError('当天事件数不合理')
    new_chats=raw.get('new_chats',[])
    if not isinstance(new_chats,list) or len(new_chats)>5: raise ValueError('新建聊天数不合理')
    known_chats=dict(context['chat_directory'])
    normalized_chats=[]
    for ch in new_chats:
        cid=ch.get('id')
        members=ch.get('members',[])
        if not isinstance(cid,str) or not re.fullmatch(r'new_[a-z0-9_]{2,50}',cid) or cid in known_chats: raise ValueError('新增聊天 ID 不合法或重复')
        if not text_ok(ch.get('title'),80) or not isinstance(members,list) or not 2<=len(members)<=8 or len(set(members))!=len(members) or not all(p in PEOPLE for p in members):
            raise ValueError('新聊天成员非法')
        if target <= dt.date(2030,10,13) and not (set(members)<= {'ding','song','he'} or set(members)<= {'ma','liu','zhang','yan'}):
            raise ValueError('开局不能跨圈相识')
        known_chats[cid]={'title':ch['title'],'members':members}
        normalized_chats.append({'id':cid,'title':ch['title'],'members':members,'day':str(target)})
    valid_prior={v['id'] for v in context['event_ledger']}
    normalized_events=[]
    total_msgs=0
    for i,e in enumerate(events,1):
        if not isinstance(e,dict) or not hhmm(e.get('time')) or not text_ok(e.get('title'),110) or not text_ok(e.get('summary'),1100):raise ValueError('事件字段不合法')
        participants=e.get('participants'); known_by=e.get('known_by'); parents=e.get('parent_ids')
        if not all(isinstance(x,list) for x in (participants,known_by,parents)) or not participants or not set(participants)<=PEOPLE or not set(known_by)<=PEOPLE or not set(parents)<=valid_prior:raise ValueError('事件的角色/历史引用不合法')
        if not set(participants)<=set(known_by): raise ValueError('已知者名单缺少当事人')
        eid=f'E{target:%Y%m%d}-{i:02d}'
        msgs=[]; posts=[]
        for j,m in enumerate(e.get('messages',[]),1):
            cid=m.get('chat_id');who=m.get('sender');at=m.get('time');body=m.get('text')
            if cid not in known_chats or who not in known_chats[cid]['members'] or who not in known_by or not hhmm(at) or at<e['time'] or not text_ok(body,450):
                raise ValueError('聊天记录成员、时间、内容不合法')
            recipients=set(known_chats[cid]['members'])
            # A chat distributes knowledge to all members; effective known_by is union below.
            msgs.append({'id':f'{eid}-M{j:02d}','chat_id':cid,'sender':who,'day':str(target),'time':at,'text':body})
            total_msgs+=1
        for j,p in enumerate(e.get('posts',[]),1):
            who=p.get('author');at=p.get('time');body=p.get('text')
            if who not in PEOPLE or not hhmm(at) or at<e['time'] or not text_ok(body,700):raise ValueError('微博字段不合法')
            posts.append({'id':f'{eid}-P{j:02d}','author':who,'day':str(target),'time':at,'text':body})
        effective_known=set(known_by)
        for m in msgs:effective_known.update(known_chats[m['chat_id']]['members'])
        normalized_events.append({'id':eid,'day':str(target),'time':e['time'],'title':e['title'],'summary':e['summary'],
            'participants':sorted(set(participants)), 'known_by':sorted(effective_known),'parent_ids':list(dict.fromkeys(parents)),
            'messages':msgs,'posts':posts})
    if total_msgs<5:raise ValueError('AI 生成的聊天过少')
    normalized_events.sort(key=lambda x:x['time'])
    return {'day':str(target),'day_summary':raw['day_summary'],'new_chats':normalized_chats,'events':normalized_events}

def next_day(expected_day):
    if not API_KEY and not USE_MOCK:raise RuntimeError('尚未配置 OPENAI_API_KEY；请先按 README 启动。')
    if not GEN_LOCK.acquire(blocking=False):raise BlockingIOError('已有一天正在生成，请完成后再点击。')
    try:
        with connect() as con:
            last=existing_max(con)
            # Explicit expected day prevents accidental duplicate regeneration.
            if expected_day is not None and expected_day!=str(last):
                return {'cached':True, **state()}
            target=last+dt.timedelta(days=1)
            if target > dt.date(2030,12,31):raise ValueError('V0.2 最多支持生成到2030年12月31日')
            context=context_for_generation(con,target)
        raw=ask_openai(context)
        batch=normalize_generated(raw,target,context)
        with connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if existing_max(con)!=last:
                con.rollback()
                return {'cached':True,**state()}
            for ch in batch['new_chats']:
                con.execute('INSERT INTO chats VALUES(?,?,?,?)',(ch['id'],ch['title'],json.dumps(ch['members'],ensure_ascii=False),ch['day']))
            for e in batch['events']:
                con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(e['id'],e['day'],e['time'],e['title'],e['summary'],json.dumps(e['participants']),json.dumps(e['known_by']),json.dumps(e['parent_ids'])))
                for m in e['messages']:
                    con.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',(m['id'],e['id'],m['chat_id'],m['day'],m['time'],m['sender'],m['text']))
                for p in e['posts']:
                    con.execute('INSERT INTO posts VALUES(?,?,?,?,?,?)',(p['id'],e['id'],p['day'],p['time'],p['author'],p['text']))
            con.execute('INSERT INTO generations VALUES(?,?,?,?)',(str(target),MODEL,json.dumps(batch,ensure_ascii=False),dt.datetime.now(dt.timezone.utc).isoformat()))
            con.commit()
        return {'cached':False,**state()}
    finally:GEN_LOCK.release()

def make_session():
    body=json.dumps({'exp':int(time.time())+SESSION_AGE,'salt':secrets.token_hex(12)},separators=(',',':')).encode('utf-8')
    payload=base64.urlsafe_b64encode(body).decode('ascii').rstrip('=')
    signature=hmac.new(SESSION_SECRET,payload.encode('ascii'),hashlib.sha256).hexdigest()
    return payload+'.'+signature

def has_session(cookie_header):
    if not CLOUD_MODE: return False
    try:
        cookies=dict(part.strip().split('=',1) for part in cookie_header.split(';') if '=' in part)
        token=cookies.get('jtz_session','')
        payload,sig=token.split('.',1)
        expected=hmac.new(SESSION_SECRET,payload.encode('ascii'),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected):return False
        data=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        return isinstance(data.get('exp'),int) and data['exp']>int(time.time())
    except (ValueError,KeyError,TypeError,UnicodeError): return False

def login_limited(ip):
    now=time.monotonic()
    with LOGIN_LOCK:
        attempts=LOGIN_ATTEMPTS[ip]
        while attempts and now-attempts[0]>15*60:attempts.popleft()
        if len(attempts)>=12:return True
        attempts.append(now)
        return False

LOGIN_HTML='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#f4edf8"><title>镜头之外 · 私人入口</title><style>*{box-sizing:border-box}body{background:linear-gradient(145deg,#eee4f6,#fbf7fc 70%,#e4e9f8);height:100dvh;margin:0;display:grid;place-items:center;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#373044}.panel{width:min(92vw,390px);background:#fffe;border:1px solid #ffffff95;box-shadow:0 20px 70px #42374d15;border-radius:30px;padding:36px 27px}.eyebrow{font-size:11px;color:#998ca8;letter-spacing:.25em}.title{font-size:33px;letter-spacing:.12em;margin:20px 0 8px}.sub{font-size:13px;color:#807789;line-height:1.9;margin:0 0 30px}.field{font-size:12px;color:#777184;margin-bottom:9px}input{display:block;width:100%;border:1px solid #ddd4e9;border-radius:15px;font:inherit;padding:15px;background:white;outline:none}input:focus{border-color:#9180ae}button{margin-top:18px;background:#665a82;border:0;color:white;padding:15px;width:100%;font:inherit;border-radius:15px;font-weight:600}button:disabled{opacity:.6}.error{min-height:30px;color:#ae5068;font-size:12px;padding-top:13px}.foot{color:#a49aaa;font-size:11px;text-align:center;margin-top:18px}</style></head><body><main class="panel"><div class="eyebrow">FOUND PHONE · PRIVATE WORLD</div><h1 class="title">镜头之外</h1><p class="sub">2030 · 临川<br>一部只属于你的平行世界手机。</p><form id="form"><div class="field">游戏访问密码</div><input id="pw" aria-label="游戏访问密码" type="password" placeholder="输入访问密码" autocomplete="current-password" required><button id="go" type="submit">进入游戏</button><div class="error" id="error" role="alert"></div></form><div class="foot">平行 AU · 所有私人内容均为虚构</div></main><script>document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();const btn=document.getElementById('go');btn.disabled=true;document.getElementById('error').textContent='';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});if(!r.ok){document.getElementById('error').textContent=r.status===429?'尝试次数太多，请稍后再试':'密码不正确';return;}location.replace('/');}catch{document.getElementById('error').textContent='暂时无法连接服务器';}finally{btn.disabled=false;}});</script></body></html>'''

def validate_preferences(payload):
    if not isinstance(payload,dict):raise ValueError('存档格式错误')
    owner=payload.get('owner','ding')
    day=payload.get('day',0)
    bookmarks=payload.get('bookmarks',[])
    if owner not in CHARACTERS or not isinstance(day,int) or day<0 or day>81:raise ValueError('机主或日期错误')
    if not isinstance(bookmarks,list) or len(bookmarks)>500:raise ValueError('手账条目过多')
    clean=[]
    for b in bookmarks:
        if not isinstance(b,dict) or not all(isinstance(b.get(k),str) and len(b[k])<=1500 for k in ('id','owner','date','chat','sender','text')):raise ValueError('手账条目格式错误')
        clean.append({k:b[k] for k in ('id','owner','date','chat','sender','text')})
    return {'owner':owner,'day':day,'bookmarks':clean}

MIME={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        # Never log credentials or request bodies.
        print('%s - %s' % (self.address_string(), fmt%args),flush=True)
    def security_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if CLOUD_MODE:self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
    def html_page(self,status,html):
        data=html.encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.security_headers()
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def origin_ok(self):
        if not CLOUD_MODE:return True
        origin=self.headers.get('Origin','')
        host=self.headers.get('Host','')
        # Browsers send Origin on same-origin fetch POST; block foreign origins.
        return not origin or origin == ('https://' + host)
    def reply(self,status,body):
        raw=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.security_headers()
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def authorized(self):
        if CLOUD_MODE:return has_session(self.headers.get('Cookie',''))
        if not PIN:return self.client_address[0] in ('127.0.0.1','::1')
        return hmac.compare_digest(self.headers.get('X-Game-Pin',''),PIN)
    def read_body(self,limit=4_000_000):
        length=int(self.headers.get('Content-Length','0'))
        if length<1 or length>limit:raise ValueError('请求大小不合法（照片不超过 3 MB）')
        return self.rfile.read(length)
    def do_GET(self):
        path=unquote(urlsplit(self.path).path)
        if path=='/api/health':return self.reply(200,{'ok':True}) if CLOUD_MODE else self.reply(200,{'ok':True,'pin_enabled':bool(PIN),'model':MODEL, 'configured':bool(API_KEY or USE_MOCK)})
        if CLOUD_MODE and path in ('/login','/login/'):
            if self.authorized():
                self.send_response(303);self.send_header('Location','/');self.end_headers();return
            return self.html_page(200,LOGIN_HTML)
        if path.startswith('/api/'):
            if not self.authorized():return self.reply(401,{'error':'请先登录'})
            if path=='/api/user-state':
                with connect() as con:
                    row=con.execute("SELECT payload FROM preferences WHERE key='single_player'").fetchone()
                return self.reply(200,{'user_state':json.loads(row['payload']) if row else None})
            if path in ('/api/state','/api/backup'):
                data=state()
                if path=='/api/backup':data['seed_version']='v0.1';data['exported_at']=dt.datetime.now(dt.timezone.utc).isoformat()
                return self.reply(200,data)
            return self.reply(404,{'error':'接口不存在'})
        if CLOUD_MODE and not self.authorized():
            self.send_response(303);self.send_header('Location','/login');self.send_header('Cache-Control','no-store');self.end_headers();return
        if path=='/':path='/index.html'
        if path not in ('/index.html','/manifest.json','/assets/icon.png') and not path.startswith('/assets/avatars/'):
            return self.reply(404,{'error':'文件不存在'})
        f=(ROOT / path.lstrip('/')).resolve()
        if path.startswith('/assets/avatars/'):
            name=path.split('/')[-1]
            if not re.fullmatch(r'(ding|song|he|ma|liu|zhang|yan)\.(png|jpg|jpeg|webp)',name):return self.reply(404,{'error':'头像不存在'})
            if (AVATAR_DIR / name).is_file():f=AVATAR_DIR / name
        if not f.is_relative_to(ROOT / 'assets') and not f.is_relative_to(AVATAR_DIR) and f not in (ROOT/'index.html',ROOT/'manifest.json'):
            return self.reply(403,{'error':'不允许访问该文件'})
        if not f.is_file():return self.reply(404,{'error':'文件不存在'})
        data=f.read_bytes(); self.send_response(200)
        self.send_header('Content-Type',MIME.get(f.suffix,'application/octet-stream'))
        self.security_headers()
        self.send_header('Cache-Control','private,no-store' if CLOUD_MODE else ('no-cache' if f.suffix in ('.html','.json') else 'public,max-age=86400'))
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        path=urlsplit(self.path).path
        if not self.origin_ok():return self.reply(403,{'error':'不允许跨网站请求'})
        if path=='/api/login' and CLOUD_MODE:
            ip=self.client_address[0]
            if login_limited(ip):return self.reply(429,{'error':'尝试过于频繁，请稍后再试'})
            try:
                data=json.loads(self.read_body(2000))
                valid=hmac.compare_digest(str(data.get('password','')).encode(),PIN.encode())
            except (ValueError,UnicodeError,TypeError):valid=False
            if not valid:return self.reply(401,{'error':'访问密码错误'})
            # login_limited counted successful login attempts too; harmless under normal use.
            cookie=make_session()
            raw=b'{"ok":true}'
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Set-Cookie',f'jtz_session={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={SESSION_AGE}')
            self.send_header('Cache-Control','no-store');self.security_headers()
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if path=='/api/logout' and CLOUD_MODE:
            self.send_response(303);self.send_header('Set-Cookie','jtz_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0');self.send_header('Location','/login');self.end_headers();return
        if path not in ('/api/advance','/api/avatar','/api/user-state'):return self.reply(404,{'error':'接口不存在'})
        if not self.authorized():return self.reply(401,{'error':'请先登录' if CLOUD_MODE else 'PIN 不正确，请在页面重新登录'})
        try:
            if path=='/api/user-state':
                prefs=validate_preferences(json.loads(self.read_body(210_000)))
                with connect() as con:
                    con.execute("INSERT INTO preferences(key,payload) VALUES('single_player',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",(json.dumps(prefs,ensure_ascii=False),))
                return self.reply(200,{'ok':True})
            if path=='/api/advance':
                payload=json.loads(self.read_body(2000))
                expected=payload.get('expected_day')
                if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'2030-\d{2}-\d{2}',expected)):
                    raise ValueError('日期无效')
                return self.reply(200,next_day(expected))
            person=self.headers.get('X-Person-Id','')
            if person not in CHARACTERS:raise ValueError('未知机主')
            typ=self.headers.get('Content-Type','').split(';')[0].lower()
            types={'image/png':('.png',b'\x89PNG\r\n\x1a\n'),'image/jpeg':('.jpg',b'\xff\xd8\xff'),'image/webp':('.webp',b'RIFF')}
            if typ not in types:raise ValueError('请上传 PNG/JPEG/WebP 照片')
            data=self.read_body(3_500_000)
            ext,magic=types[typ]
            if not data.startswith(magic) or (typ=='image/webp' and data[8:12]!=b'WEBP'):raise ValueError('图片格式和内容不匹配')
            folder=AVATAR_DIR
            folder.mkdir(parents=True,exist_ok=True)
            for old in folder.glob(person+'.*'): old.unlink()
            target=folder/(person+ext)
            target.write_bytes(data)
            return self.reply(200,{'ok':True,'avatars':avatars()})
        except BlockingIOError as exc:return self.reply(409,{'error':str(exc)})
        except (ValueError,sqlite3.Error,json.JSONDecodeError) as exc:return self.reply(422,{'error':str(exc)})
        except Exception as exc:
            # Do not reveal secret values, stack traces, or API request details to the browser.
            err=str(exc)
            if 'OpenAI API HTTP' in err:
                try:
                    num=int(err.split('HTTP ')[1].split(':')[0])
                    messages={401:'API Key 无效或已失效',403:'API 账户没有调用权限',429:'API 余额不足/限流，请检查账单与使用额度'}
                    err=messages.get(num, 'OpenAI API 返回 HTTP '+str(num))
                except (ValueError, IndexError):err='OpenAI API 调用失败'
            self.reply(503,{'error':err[:350]})


def main():
    init_db()
    if CLOUD_MODE:
        if HOST not in ('0.0.0.0','::'):raise SystemExit('云端模式必须绑定 0.0.0.0')
        if not API_KEY:raise SystemExit('云端模式必须提供 OPENAI_API_KEY')
        if len(PIN)<16:raise SystemExit('云端模式的 GAME_PIN 必须至少16位随机字符')
        if len(SESSION_SECRET)<32:raise SystemExit('云端模式的 SESSION_SECRET 必须至少32位')
        if os.environ.get('RENDER') == 'true':
            if not os.path.ismount(DATA_DIR):
                raise SystemExit('Render 持久化磁盘未挂载：请按 render.yaml 将磁盘挂载到 /app/data。')
        elif not os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') and not os.environ.get('ALLOW_EPHEMERAL_DATA'):
            raise SystemExit('云端模式缺少持久化存储卷：请先挂载持久化磁盘到 /app/data。')
    elif HOST=='0.0.0.0' and not PIN:raise SystemExit('局域网模式必须设置 GAME_PIN')
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print('镜头之外 V0.4 已启动，端口',PORT, '模型',MODEL, 'API', '已配置' if API_KEY else '未配置',flush=True)
    if not CLOUD_MODE:print(f'电脑打开: http://127.0.0.1:{PORT}',flush=True)
    if HOST=='0.0.0.0' and not CLOUD_MODE:
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            s.connect(('192.0.2.1',80))
            local=s.getsockname()[0];s.close()
            print(f'iPhone 同一 Wi-Fi 打开: http://{local}:{PORT} （网页需输入 PIN）',flush=True)
        except OSError:print('请通过 Mac 的 Wi-Fi IP 地址在 iPhone 打开，端口',PORT,flush=True)
    server.serve_forever()

if __name__=='__main__':main()
