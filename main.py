from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import random, string, os, uuid, time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = 'majabeed_2025_secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                    ping_timeout=60, ping_interval=25)

users_db   = {}   # uid → {name, avatar, wins}
servers_db = {}   # sid → server
games_db   = {}   # sid → game
# reconnect: uid → {sid, last_seen}
reconnect_db = {}

SUITS = ['♠','♥','♦','♣']
RANKS = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
POINTS = {'10':10,'J':10,'Q':10,'K':10,'A':10,'JOKER':50}

def cpts(r): return POINTS.get(r,0)

def make_deck(n):
    base_cards = 108 + (n-2)*18
    base_jokers = 6 + (n-2)*2
    full=[]
    rep=0
    while len(full)<base_cards:
        for s in SUITS:
            for r in RANKS:
                full.append({'rank':r,'suit':s,'id':f"{r}{s}_{rep}"})
        rep+=1
    cards = full[:base_cards]
    for i in range(base_jokers):
        cards.append({'rank':'JOKER','suit':'★','id':f'JOK{i}'})
    for i in range(len(cards)-1,0,-1):
        j=random.randint(0,i)
        cards[i],cards[j]=cards[j],cards[i]
    return cards

def deal(deck,n):
    idx=0; hands={}
    for i in range(n):
        hands[i]=deck[idx:idx+7]; idx+=7
    field=deck[idx:idx+7]; idx+=7
    return hands, field, deck[idx:]

def make_teams(order, mode='auto'):
    n=len(order); sh=order[:]
    random.shuffle(sh)
    if mode=='solo' or n<4:
        return {p:f"solo_{i}" for i,p in enumerate(sh)}, [[p] for p in sh]
    elif n==4:
        teams={sh[0]:'A',sh[1]:'B',sh[2]:'A',sh[3]:'B'}
        tlist=[[sh[0],sh[2]],[sh[1],sh[3]]]
    elif n in (5,7):
        teams={p:('A' if i<2 else 'B') for i,p in enumerate(sh)}
        tlist=[sh[:2],sh[2:]]
    else:
        half=n//2
        teams={p:('A' if i<half else 'B') for i,p in enumerate(sh)}
        tlist=[sh[:half],sh[half:]]
    return teams, tlist

def uname(u): return users_db.get(u,{}).get('name',u)
def uav(u):   return users_db.get(u,{}).get('avatar','')

def bcast_players(sid):
    if sid not in servers_db: return
    srv=servers_db[sid]
    def inf(p): return {'id':p,'name':uname(p),'avatar':uav(p)}
    emit('players_update',{
        'players':[inf(p) for p in srv['players']],
        'spectators':[inf(p) for p in srv.get('spectators',[])],
        'host_id':srv['host_id']
    },room=sid)

def top_card(bank):
    if not bank: return None
    c=bank[-1]
    return {'rank':c['rank'],'suit':c['suit'],'id':c['id']}

def hcount(g): return {p:len(g['hands'][p]) for p in g['players']}
def bcount(g): return {p:len(g['banks'][p]) for p in g['players']}
def tops(g):   return {p:top_card(g['banks'][p]) for p in g['players']}

def auto_draw(g,pid):
    drawn=[]
    while len(g['hands'][pid])<7 and g['draw']:
        c=g['draw'].pop(0); g['hands'][pid].append(c); drawn.append(c)
    return drawn

def advance_turn(g):
    n=len(g['players'])
    g['turn_idx']=(g['turn_idx']+1)%n
    g['eaten']=False; g['grab_mode']=False
    nxt=g['players'][g['turn_idx']]
    drawn=auto_draw(g,nxt)
    return nxt,drawn

# ── HTTP ────────────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_file(os.path.join(BASE_DIR,'index.html'))

@app.route('/api/register',methods=['POST'])
def register():
    d=request.json or {}
    name=d.get('name','').strip(); avatar=d.get('avatar','')
    if not name: return jsonify({'error':'الاسم مطلوب'}),400
    uid=str(uuid.uuid4())
    users_db[uid]={'name':name,'avatar':avatar,'wins':[]}
    return jsonify({'user_id':uid,'name':name})

@app.route('/api/user/<uid>')
def get_user(uid):
    if uid in users_db: return jsonify({'ok':True,'user':users_db[uid]})
    return jsonify({'ok':False}),404

@app.route('/api/user/<uid>',methods=['PATCH'])
def upd_user(uid):
    if uid not in users_db: return jsonify({'error':'not found'}),404
    d=request.json or {}
    if 'name' in d and d['name'].strip(): users_db[uid]['name']=d['name'].strip()
    if 'avatar' in d: users_db[uid]['avatar']=d['avatar']
    return jsonify({'ok':True,'user':users_db[uid]})

@app.route('/api/user/<uid>/wins')
def get_wins(uid):
    wins=list(reversed(users_db.get(uid,{}).get('wins',[])))
    return jsonify({'wins':wins})

@app.route('/api/servers')
def list_servers():
    return jsonify([{'id':sid,'name':s['name'],'players':len(s['players']),
        'max_players':s['max_players'],'status':s['status'],
        'has_password':bool(s['password']),'mode':s.get('mode','auto'),
        'spectators':len(s.get('spectators',[]))}
        for sid,s in servers_db.items()])

@app.route('/api/servers/create',methods=['POST'])
def create_server():
    d=request.json or {}
    name=d.get('name','').strip(); pw=d.get('password','')
    hid=d.get('user_id',''); maxp=int(d.get('max_players',4))
    mode=d.get('mode','auto')
    if not name: return jsonify({'error':'اسم الخادم مطلوب'}),400
    if hid not in users_db: return jsonify({'error':'يجب تسجيل الدخول أولاً'}),401
    sid=''.join(random.choices(string.ascii_uppercase+string.digits,k=6))
    servers_db[sid]={'name':name,'password':pw,'host_id':hid,
        'players':[hid],'spectators':[],'status':'waiting',
        'max_players':max(2,min(8,maxp)),'mode':mode,
        'created_at':datetime.now().isoformat()}
    return jsonify({'server_id':sid,'name':name})

@app.route('/api/servers/join',methods=['POST'])
def join_server():
    d=request.json or {}
    sid=d.get('server_id',''); pw=d.get('password','')
    uid=d.get('user_id',''); spec=d.get('spectator',False)
    if sid not in servers_db: return jsonify({'error':'الخادم غير موجود'}),404
    if uid not in users_db:   return jsonify({'error':'يجب تسجيل الدخول'}),401
    srv=servers_db[sid]
    if srv['password'] and srv['password']!=pw: return jsonify({'error':'كلمة المرور خاطئة'}),403
    if spec:
        if uid not in srv['spectators']: srv['spectators'].append(uid)
    else:
        if srv['status']=='playing':
            # السماح بإعادة الانضمام
            if uid in srv.get('disconnected',[]):
                srv.get('disconnected',[]).remove(uid)
                return jsonify({'success':True,'rejoin':True,
                    'players':[{'id':p,'name':uname(p),'avatar':uav(p)} for p in srv['players']]})
            return jsonify({'error':'اللعبة جارية'}),400
        if len(srv['players'])>=srv['max_players']: return jsonify({'error':'الخادم ممتلئ'}),400
        if uid not in srv['players']: srv['players'].append(uid)
    return jsonify({'success':True,
        'players':[{'id':p,'name':uname(p),'avatar':uav(p)} for p in srv['players']]})

# ── SOCKET ──────────────────────────────────────────────────────────────────
@socketio.on('join_personal_room')
def on_join_personal(data):
    uid=data.get('user_id')
    if uid:
        join_room(uid)
        reconnect_db[uid]={'last_seen':time.time()}

@socketio.on('join_server_room')
def on_join_server(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    join_room(sid)
    reconnect_db[uid]={'sid':sid,'last_seen':time.time()}
    bcast_players(sid)
    # إذا كان في لعبة جارية، أرسل له الحالة
    if sid in games_db:
        g=games_db[sid]
        if g['status']=='playing' and uid in g['players']:
            _send_rejoin(uid,sid,g)

def _send_rejoin(uid,sid,g):
    emit('game_rejoin',{
        'your_hand':g['hands'].get(uid,[]),
        'field':g['field'],'draw_count':len(g['draw']),
        'players':g['players'],'player_names':{p:uname(p) for p in g['players']},
        'player_avatars':{p:uav(p) for p in g['players']},
        'current_turn':g['players'][g['turn_idx']],
        'your_id':uid,'teams':g['teams'],'tnames':g['tnames'],
        'tlist':g['tlist'],'tcolors':g['tcolors'],
        'banks_count':bcount(g),'banks_tops':tops(g),
        'my_bank':g['banks'].get(uid,[]),
        'hands_count':hcount(g),'eaten':g.get('eaten',False),
    },to=uid)

@socketio.on('leave_server')
def on_leave(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid in servers_db:
        srv=servers_db[sid]
        if uid in srv['players']: srv['players'].remove(uid)
        if uid in srv.get('spectators',[]): srv['spectators'].remove(uid)
        if srv['host_id']==uid and srv['players']:
            srv['host_id']=srv['players'][0]
        leave_room(sid)
        bcast_players(sid)
        if not srv['players']:
            servers_db.pop(sid,None)

@socketio.on('kick_player')
def on_kick(data):
    sid=data.get('server_id'); hid=data.get('host_id'); tid=data.get('target_id')
    if sid in servers_db:
        srv=servers_db[sid]
        if srv['host_id']==hid and tid in srv['players']:
            srv['players'].remove(tid)
            emit('kicked',{'user_id':tid},room=sid)
            bcast_players(sid)

@socketio.on('force_end_game')
def on_force_end(data):
    sid=data.get('server_id'); hid=data.get('host_id')
    if sid not in servers_db: return
    if servers_db[sid]['host_id']!=hid: return
    emit('game_force_ended',{'msg':'أنهى المضيف اللعبة'},room=sid)
    games_db.pop(sid,None)
    if sid in servers_db: servers_db[sid]['status']='waiting'

@socketio.on('start_game')
def on_start(data):
    sid=data.get('server_id'); hid=data.get('host_id')
    if sid not in servers_db: return
    srv=servers_db[sid]
    if srv['host_id']!=hid: return
    pls=srv['players']; n=len(pls)
    if n<2:
        emit('error',{'msg':'يجب أن يكون هناك لاعبان على الأقل'},room=sid); return
    deck=make_deck(n)
    hands,field,draw=deal(deck,n)
    order=pls[:]
    random.shuffle(order)
    teams,tlist=make_teams(order,srv.get('mode','auto'))
    tnames={}
    for i,grp in enumerate(tlist):
        t=chr(65+i)
        for p in grp: tnames[p]=f"الفريق {t}"
    g={
        'sid':sid,'players':order,
        'hands':{order[i]:hands[i] for i in range(n)},
        'field':field,'draw':draw,
        'banks':{p:[] for p in order},
        'turn_idx':0,'status':'playing','claim':None,
        'teams':teams,'tlist':tlist,'tnames':tnames,
        'tcolors':{'A':'#4488ff','B':'#ff6644'},
        'start_time':datetime.now().isoformat(),
        'eaten':False,'grab_mode':False,
        'chat':[],
    }
    games_db[sid]=g; srv['status']='playing'
    pn={p:uname(p) for p in order}; pa={p:uav(p) for p in order}
    for p in order:
        emit('game_started',{
            'your_hand':g['hands'][p],'field':field,'draw_count':len(draw),
            'players':order,'player_names':pn,'player_avatars':pa,
            'current_turn':order[0],'your_id':p,
            'teams':teams,'tnames':tnames,'tlist':tlist,
            'tcolors':g['tcolors'],'total_deck':len(deck),'num_players':n,
        },room=p)

# ── CLAIM ───────────────────────────────────────────────────────────────────
@socketio.on('claim_card')
def on_claim(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    hid=data.get('hand_card'); fid=data.get('field_card')
    if sid not in games_db: return
    g=games_db[sid]
    cur=g['players'][g['turn_idx']]
    if cur!=uid: emit('error',{'msg':'ليس دورك'},to=uid); return
    hc=next((c for c in g['hands'][uid] if c['id']==hid),None)
    fc=next((c for c in g['field']       if c['id']==fid),None)
    if not hc: emit('error',{'msg':'الورقة غير في يدك'},to=uid); return
    if not fc: emit('error',{'msg':'الورقة غير في الميدان'},to=uid); return
    if hc['rank']!=fc['rank'] and hc['rank']!='JOKER' and fc['rank']!='JOKER':
        emit('error',{'msg':'الأرقام لا تتطابق'},to=uid); return
    g['claim']={'pid':uid,'hcard':hc,'fcard':fc,
        'challengers':[],'chal_cards':{},'chal_times':{},'resolved':False}
    g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=hid]
    emit('claim_announced',{'player_id':uid,'player_name':uname(uid),'hand_card':hc,'field_card':fc},room=sid)

# ── CHALLENGE ── زر معارضة مستقل ────────────────────────────────────────────
@socketio.on('challenge_claim')
def on_chal(data):
    sid=data.get('server_id'); uid=data.get('user_id'); cid=data.get('card')
    if sid not in games_db: return
    g=games_db[sid]; cl=g.get('claim')
    if not cl or cl['resolved']: return
    if uid==cl['pid']: return
    # ✅ أي لاعب يعارض أي لاعب بدون قيود
    card=next((c for c in g['hands'][uid] if c['id']==cid),None)
    if not card: return
    if card['rank']!=cl['hcard']['rank'] and card['rank']!='JOKER':
        emit('error',{'msg':'الورقة لا تطابق'},to=uid); return
    now=time.time()
    if len(cl['challengers'])==0:
        cl['challengers'].append(uid); cl['chal_cards'][uid]=card; cl['chal_times'][uid]=now
        g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
        emit('challenge_announced',{'player_id':uid,'player_name':uname(uid),'card':card},room=sid)
    else:
        first_t=list(cl['chal_times'].values())[0]
        if now-first_t<=3.0:
            emit('error',{'msg':'تم قبول معارضة أخرى'},to=uid)
        else:
            cl['challengers'].append(uid); cl['chal_cards'][uid]=card; cl['chal_times'][uid]=now
            g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
            emit('challenge_announced',{'player_id':uid,'player_name':uname(uid),'card':card},room=sid)

@socketio.on('resolve_claim')
def on_resolve(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]; cl=g.get('claim')
    if not cl or cl['pid']!=uid or cl['resolved']: return
    cl['resolved']=True
    challengers=cl['challengers']
    if len(challengers)==0:
        won=[cl['hcard'],cl['fcard']]; winner=uid
    else:
        first=challengers[0]
        won=[cl['hcard'],cl['fcard'],cl['chal_cards'][first]]; winner=first
    # ✅ الأوراق → البنك مباشرة (ليس اليد)
    g['banks'][winner].extend(won)
    g['field']=[c for c in g['field'] if c['id']!=cl['fcard']['id']]
    g['claim']=None; g['eaten']=True
    drawn=auto_draw(g,winner)
    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done: _end_game(sid,g); return
    hc=hcount(g); bc=bcount(g); tp=tops(g)
    emit('claim_resolved',{
        'winner':winner,'winner_name':uname(winner),'won_cards':won,
        'banks_count':bc,'banks_tops':tp,'field':g['field'],
        'draw_count':len(g['draw']),'same_turn':True,
        'current_turn':g['players'][g['turn_idx']],'hands_count':hc,'drawn':drawn
    },room=sid)
    for p in g['players']:
        u={'hand':g['hands'][p],'my_bank':g['banks'][p] if p==winner else None}
        if p==winner and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

# ── MEYDANA ──────────────────────────────────────────────────────────────────
@socketio.on('meydana')
def on_meydana(data):
    sid=data.get('server_id'); uid=data.get('user_id'); cid=data.get('card_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid: emit('error',{'msg':'ليس دورك'},to=uid); return
    card=next((c for c in g['hands'][uid] if c['id']==cid),None)
    if not card: emit('error',{'msg':'الورقة غير في يدك'},to=uid); return
    g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
    g['field'].append(card)
    nxt,drawn=advance_turn(g)
    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done: _end_game(sid,g); return
    emit('meydana_done',{'player_id':uid,'player_name':uname(uid),'card':card,
        'field':g['field'],'next_turn':nxt,'draw_count':len(g['draw']),'hands_count':hcount(g)},room=sid)
    for p in g['players']:
        u={'hand':g['hands'][p]}
        if p==nxt and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

# ── END TURN ─────────────────────────────────────────────────────────────────
@socketio.on('end_turn')
def on_end_turn(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid: emit('error',{'msg':'ليس دورك'},to=uid); return
    nxt,drawn=advance_turn(g)
    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done: _end_game(sid,g); return
    emit('turn_changed',{'next_turn':nxt,'draw_count':len(g['draw']),'hands_count':hcount(g)},room=sid)
    for p in g['players']:
        u={'hand':g['hands'][p]}
        if p==nxt and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

# ── GRAB BANK ────────────────────────────────────────────────────────────────
@socketio.on('request_grab_bank')
def on_req_grab(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid: emit('error',{'msg':'ليس دورك'},to=uid); return
    banks=[{'player_id':p,'player_name':uname(p),'count':len(g['banks'][p]),
            'top_card':top_card(g['banks'][p])} for p in g['players'] if p!=uid and g['banks'][p]]
    emit('grab_bank_list',{'banks':banks,'your_hand':g['hands'][uid]},to=uid)

@socketio.on('grab_bank')
def on_grab(data):
    sid=data.get('server_id'); uid=data.get('user_id'); tid=data.get('target_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid: emit('error',{'msg':'ليس دورك'},to=uid); return
    if not g['banks'].get(tid): emit('error',{'msg':'البنك فارغ'},to=uid); return
    # ✅ الورقة → بنك اللاعب مباشرة (ليس اليد)
    top=g['banks'][tid].pop()
    g['banks'][uid].append(top)
    bc=bcount(g); tp=tops(g)
    emit('bank_grabbed',{'grabber_id':uid,'grabber_name':uname(uid),
        'target_id':tid,'target_name':uname(tid),'card':top,
        'banks_count':bc,'banks_tops':tp},room=sid)
    emit('your_hand',{'hand':g['hands'][uid],'my_bank':g['banks'][uid]},to=uid)
    emit('banks_top_update',{'tops':tp,'counts':bc},room=sid)

# ── BURY ─────────────────────────────────────────────────────────────────────
@socketio.on('bury_cards')
def on_bury(data):
    sid=data.get('server_id'); uid=data.get('user_id'); ids=data.get('card_ids',[])
    if sid not in games_db: return
    g=games_db[sid]
    bury=[c for c in g['field'] if c['id'] in ids]
    if not bury: return
    g['banks'][uid].extend(bury)
    g['field']=[c for c in g['field'] if c['id'] not in ids]
    emit('cards_buried',{'player_id':uid,'cards':bury,'field':g['field'],'banks_count':bcount(g)},room=sid)
    emit('banks_top_update',{'tops':tops(g),'counts':bcount(g)},room=sid)

# ── CHAT ─────────────────────────────────────────────────────────────────────
@socketio.on('chat_msg')
def on_chat(data):
    sid=data.get('server_id'); uid=data.get('user_id'); msg=data.get('msg','').strip()
    if not msg or len(msg)>200: return
    entry={'uid':uid,'name':uname(uid),'msg':msg,'time':datetime.now().strftime('%H:%M')}
    if sid in games_db: games_db[sid]['chat'].append(entry)
    emit('chat_msg',entry,room=sid)

# ── VOICE (WebRTC signaling) ──────────────────────────────────────────────────
@socketio.on('voice_offer')
def on_voice_offer(data):
    target=data.get('target'); offer=data.get('offer'); uid=data.get('user_id')
    emit('voice_offer',{'from':uid,'offer':offer},to=target)

@socketio.on('voice_answer')
def on_voice_answer(data):
    target=data.get('target'); answer=data.get('answer'); uid=data.get('user_id')
    emit('voice_answer',{'from':uid,'answer':answer},to=target)

@socketio.on('voice_ice')
def on_ice(data):
    target=data.get('target'); cand=data.get('candidate'); uid=data.get('user_id')
    emit('voice_ice',{'from':uid,'candidate':cand},to=target)

@socketio.on('voice_speaking')
def on_speaking(data):
    sid=data.get('server_id'); uid=data.get('user_id'); speaking=data.get('speaking',False)
    emit('voice_speaking',{'uid':uid,'name':uname(uid),'speaking':speaking},room=sid)

# ── END GAME ──────────────────────────────────────────────────────────────────
def _end_game(sid,g):
    scores={}; details={}
    for p in g['players']:
        bank=g['banks'][p]; pt=sum(cpts(c['rank']) for c in bank)
        scores[p]=pt
        details[p]={'no_pts':[c for c in bank if cpts(c['rank'])==0],
                    'with_pts':[c for c in bank if cpts(c['rank'])>0],'total':pt}
    ranked=sorted(scores.keys(),key=lambda p:scores[p],reverse=True)
    ts={}
    for p,t in g['teams'].items(): ts[t]=ts.get(t,0)+scores.get(p,0)
    g['status']='ended'
    if sid in servers_db: servers_db[sid]['status']='waiting'
    now=datetime.now()
    for i,p in enumerate(ranked[:3]):
        if p in users_db:
            if 'wins' not in users_db[p]: users_db[p]['wins']=[]
            users_db[p]['wins'].append({'date':now.strftime('%Y-%m-%d'),'time':now.strftime('%H:%M'),
                'points':scores[p],'rank':i+1,'match_id':sid,
                'mode':servers_db.get(sid,{}).get('mode','auto'),
                'players':len(g['players']),'timestamp':now.isoformat()})
    emit('game_ended',{'scores':scores,'details':details,'ranking':ranked,
        'pnames':{p:uname(p) for p in g['players']},'pavatars':{p:uav(p) for p in g['players']},
        'top3':ranked[:3],'teams':g['teams'],'tnames':g['tnames'],'tlist':g['tlist'],
        'team_scores':ts,'start_time':g.get('start_time','')},room=sid)

if __name__ == '__main__':
    import os
    # المنصة تعطي السيرفر بورت تلقائي، وإذا ما لقيته بنشغل على 10000 وهو البورت الافتراضي لـ Render
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 تشغيل لعبة مجابيد على البورت: {port}")
    # تشغيل السيرفر مباشرة عبر socketio بالاعتماد على eventlet القوي
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)