# -*- coding: utf-8 -*-
"""TSM300 ↔ 매직차트(읽기만) · 예진이 서버 · 상쾌한캡처 결과지.

VNGLive v1.12 의 live_emr.py(환자 찾기 · 오늘 오더)와 live_capture.py(상쾌한캡처 결과지 보내기)에서
SQL · 규칙 · 오류 처리를 그대로 옮기고, 검사 코드만 인자로 받게 바꾼 것(2026-11-26).
비밀값(DB 아이디 · 비밀번호, 예진이 토큰, 상쾌한캡처 키)은 코드에 넣지 않음 — 부르는 쪽이 설정 파일에서 읽어 넘김.

    db = MagicDb(dsn='', uid='', pwd='')            # 다 비우면 이 PC 의 SQL Anywhere DSN 을 찾아 DSN 에 저장된 계정으로
    p = db.find_patient('00512')                     # 환자 한 명 dict 또는 None
    rows = db.todays_orders(TYMP_CODES)              # 오늘 중이검사 오더 명단
    r = save_to_entcapture('result.jpg', p, '2026-11-26', 'tymp', url=..., key=...)
"""
import datetime
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# 수가표의 임피던스 청력검사 코드. 실제 오더는 거의 F6361(고막운동성계측)
TYMP_CODES = ('F6361', 'F6362', 'F6363')
TYMP_NAMES = {'F6361': '고막운동성계측', 'F6362': '등골근반사', 'F6363': '등골근반사소실'}
SA_RX = re.compile(r'anywhere|sybase|adaptive|\basa\b', re.I)       # 매직차트 DB(SQL Anywhere) ODBC 드라이버 이름
LIMIT = 30                                                          # 예진이와 같게: 차트번호 5명, 나머지 30명


# ------------------------------------------------------------------ 공통: 생년월일 · 나이 · 성별
def birth_age_sex(j7, today=None, birthday=None):
    """주민번호 앞 7자리 → ('1960-01-02', 65, '남'). 주민번호로 안 되면 생일 칸(YYYY-MM-DD, 성별 모름). 모르면 ('', None, '')."""
    d = re.sub(r'\D', '', str(j7 or ''))
    b = sex = None
    if len(d) >= 7:
        g = d[6]
        century = {'1': 1900, '2': 1900, '5': 1900, '6': 1900, '3': 2000, '4': 2000, '7': 2000, '8': 2000, '9': 1800, '0': 1800}.get(g)
        if century is not None:
            sex = '남' if g in '13579' else '여'
            try:
                b = datetime.date(century + int(d[0:2]), int(d[2:4]), int(d[4:6]))
            except ValueError:
                b = None
    if b is None:
        t8 = re.sub(r'\D', '', str(birthday or ''))[:8]
        try:
            b = datetime.date(int(t8[0:4]), int(t8[4:6]), int(t8[6:8])) if len(t8) == 8 else None
        except ValueError:
            b = None
    if b is None:
        return '', None, ''
    t = today or datetime.date.today()
    return b.isoformat(), t.year - b.year - ((t.month, t.day) < (b.month, b.day)), sex or ''


def classify(q):
    """검색어 → [(종류, 값)]. 숫자만: 차트번호(넣은 글자 그대로 — 앞자리 0 유지) + (6·8자리면 생년월일) + (4자리면 휴대폰 끝 4자리).
    그 밖: 이름(일부). 공백은 지우고 20자까지, 이름에 % · _ 가 있으면 찾지 않음."""
    q = re.sub(r'\s', '', q or '')[:20]
    digits = re.sub(r'[.\-/]', '', q)
    if digits.isdigit():
        out = [('chart', q)]
        if len(digits) == 8:
            out.append(('birth8', digits))
        elif len(digits) == 6:
            out.append(('birth', digits))
        if len(digits) == 4:
            out.append(('phone', digits))
        return out
    if not q or '%' in q or '_' in q:
        return []
    return [('name', q)]


# ------------------------------------------------------------------ 1 · 2. 매직차트 DB 직접 (ODBC, 읽기만)
class MagicDb:
    """매직차트(비트컴퓨터, SQL Anywhere) 연결 하나. 필요할 때 열고, 끊기면 한 번 다시. SELECT 만 보냄.
    connect_fn 을 주면 그것으로 연결(시험: sqlite)."""

    def __init__(self, dsn='', uid='', pwd='', connect_fn=None):
        self.dsn, self.uid, self.pwd, self.connect_fn = dsn or '', uid or '', pwd or '', connect_fn
        self.conn = None; self.lock = threading.Lock(); self.used_dsn = ''

    @staticmethod
    def find_dsns():
        """이 PC 의 ODBC 데이터 원본 중 SQL Anywhere 드라이버인 것 [(이름, 드라이버)] (64비트 파이썬이면 64비트 쪽만 보임)."""
        try:
            import pyodbc
            return [(n, d) for n, d in pyodbc.dataSources().items() if SA_RX.search(d)]
        except Exception:
            return []

    def _connect(self):
        if self.connect_fn:
            self.conn = self.connect_fn(); return
        import pyodbc
        dsn = self.dsn
        if not dsn:
            cands = self.find_dsns()
            if not cands:
                raise RuntimeError('이 PC 에는 매직차트 DB 연결(ODBC, SQL Anywhere)이 없습니다')
            dsn = cands[0][0]
        cs = f'DSN={dsn}' + (f';UID={self.uid};PWD={self.pwd}' if self.pwd else '')      # 비우면 DSN 에 저장된 계정
        try:
            self.conn = pyodbc.connect(cs, autocommit=True, readonly=True, timeout=5)
        except pyodbc.Error:
            self.conn = pyodbc.connect(cs, autocommit=True, timeout=5)    # 병원 드라이버가 읽기 전용 속성을 안 받는 경우(VNGLive 병원 시험에서 확인)
        self.used_dsn = dsn

    def _rows(self, sql, params=(), limit=None):
        if not sql.lstrip().upper().startswith('SELECT'):
            raise ValueError('읽기(SELECT)만 합니다')
        with self.lock:
            for attempt in range(2):
                try:
                    if self.conn is None:
                        self._connect()
                    cur = self.conn.cursor()
                    cur.execute(sql, tuple(params))
                    cols = [c[0].lower() for c in cur.description]
                    rs = cur.fetchmany(limit) if limit else cur.fetchall()
                    cur.close()
                    return [dict(zip(cols, r)) for r in rs]
                except Exception:
                    self.conn = None
                    if attempt == 1:
                        raise
        return []

    def close(self):
        with self.lock:
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None

    def test(self):
        n = self._rows('SELECT COUNT(*) AS n FROM zupsu')[0]['n']
        return f"연결됨 ({self.used_dsn or '시험'}) — 환자 {int(n):,}명"

    # 주민번호는 DB 안에서 앞 7자리만 잘라 읽음 — 뒷자리는 프로그램에 들어오지 않음
    _P = ("SELECT chart_code, show_chart_code, susin_name, SUBSTR(REPLACE(jumin_number, '-', ''), 1, 7) AS j7, birthday FROM zupsu ")

    def _patients(self, rows, how):
        out = []
        for r in rows:
            b, age, sex = birth_age_sex(r.get('j7'), birthday=r.get('birthday'))
            out.append(dict(chart=str(r.get('show_chart_code') or '').strip(), code=r.get('chart_code'),
                            name=str(r.get('susin_name') or '').strip(), birth=b, age=age, sex=sex, how=how))
        return out

    def _add_last(self, found):
        codes = ','.join(str(int(p['code'])) for p in found if p['code'] is not None)
        last = {r['chart_code']: r['d'] for r in self._rows(
            f'SELECT chart_code, MAX(exam_date) AS d FROM exam_date WHERE chart_code IN ({codes}) GROUP BY chart_code')} if codes else {}
        for p in found:
            d = last.get(p['code'])
            p['last'] = str(d)[:10] if d else ''

    def find_patient(self, chart_no):
        """화면 차트번호(글자 그대로, 앞자리 0 포함) → 환자 dict 또는 None. 같은 번호가 둘 이상이면 LookupError(사람 확인)."""
        chart_no = str(chart_no or '').strip()
        if not chart_no:
            return None
        found = [p for p in self._patients(self._rows(self._P + 'WHERE show_chart_code = ?', (chart_no,), 5), '차트번호') if p['chart']]
        if len(found) > 1:
            raise LookupError(f'차트번호 {chart_no} 환자가 {len(found)}명입니다 — 사람이 확인해 주세요')
        if not found:
            return None
        self._add_last(found)
        return found[0]

    def search(self, q):
        """검색어(차트번호 · 이름 일부 · 생년월일 6/8자리 · 휴대폰 끝 4자리) → 환자 목록(차트번호로 맞은 사람 먼저, 그다음 최근 진료 순)."""
        found, seen = [], set()
        for kind, v in classify(q):
            if kind == 'chart':
                rows = self._rows(self._P + 'WHERE show_chart_code = ?', (v,), 5); how = '차트번호'
            elif kind in ('birth', 'birth8'):
                rows = self._rows(self._P + 'WHERE jumin_number LIKE ?', ((v[2:] if kind == 'birth8' else v) + '%',), LIMIT); how = '생년월일'
            elif kind == 'phone':
                rows = self._rows(self._P + "WHERE REPLACE(hpon, '-', '') LIKE ?", ('%' + v,), LIMIT); how = '휴대폰 끝 4자리'
            else:
                rows = self._rows(self._P + 'WHERE susin_name LIKE ?', ('%' + v + '%',), LIMIT); how = '이름'
            for p in self._patients(rows, how):
                if kind == 'birth8' and p['birth'][:4] != v[:4]:
                    continue
                if p['chart'] and p['code'] not in seen:
                    seen.add(p['code']); found.append(p)
        if found:
            self._add_last(found)
            found.sort(key=lambda p: p['last'], reverse=True)
            found.sort(key=lambda p: p['how'] != '차트번호')
        return found

    def todays_orders(self, order_codes=TYMP_CODES, day=None):
        """그날(YYYY-MM-DD, 없으면 오늘) order_codes 오더가 난 진료 → 진료 1건당 1줄, 접수 시각 순.
        [{chart, code, name, birth, age, sex, how, time 'HH:MM', room, codes [...], tests '…', visit}]"""
        day = day or datetime.date.today().isoformat()
        want = {str(c).strip().upper() for c in order_codes if str(c).strip()}
        if not want:
            return []
        like = ' OR '.join('b.yak_code LIKE ?' for _ in want)
        rows = self._rows("SELECT a.exam_date_code, a.chart_code, a.exam_date, a.jinro_room, b.yak_code FROM exam_date a, exam_yak b "
                          f"WHERE a.exam_date BETWEEN ? AND ? AND b.exam_date_code = a.exam_date_code AND ({like}) "
                          "ORDER BY a.exam_date, b.yak_code",
                          (f'{day} 00:00:00', f'{day} 23:59:59', *sorted(c + '%' for c in want)))
        visits = {}
        for r in rows:
            c = str(r['yak_code'] or '').strip().upper()
            if c not in want:                                    # LIKE 'F6361%' 에 걸린 다른 긴 코드는 뺌
                continue
            v = visits.setdefault(r['exam_date_code'], dict(code=r['chart_code'], time=str(r['exam_date'])[11:16],
                                                            room=str(r.get('jinro_room') or '').strip(), codes=[]))
            if c not in v['codes']:
                v['codes'].append(c)
        if not visits:
            return []
        codes = ','.join(sorted({str(int(v['code'])) for v in visits.values()}))
        pats = {p['code']: p for p in self._patients(self._rows(self._P + f'WHERE chart_code IN ({codes})'), '오늘 오더')}
        out = []
        for ec, v in visits.items():
            p = dict(pats.get(v['code']) or dict(chart='', code=v['code'], name='(환자 정보 없음)', birth='', age=None, sex='', how='오늘 오더'))
            p.update(time=v['time'], room=v['room'], codes=v['codes'], tests=' · '.join(TYMP_NAMES.get(c, c) for c in v['codes']), visit=ec)
            out.append(p)
        out.sort(key=lambda p: p['time'])
        return out


# ------------------------------------------------------------------ 1. 예진이 서버가 대신 읽어 줌 (매직차트가 없는 PC)
YEJIN_PORT = 5050


def server_url(u, port):
    """'192.168.0.10' · '192.168.0.10:5050' · 'http://…/' → 'http://192.168.0.10:5050' (빈 값이면 '')."""
    u = (u or '').strip().rstrip('/')
    if not u:
        return ''
    if '://' not in u:
        u = 'http://' + u
    pr = urllib.parse.urlsplit(u)
    return f"{pr.scheme}://{pr.netloc if pr.port else f'{pr.hostname}:{port}'}"


class YejinServer:
    """MagicDb 와 같은 쓰임 — 매직차트 DB 대신 예진이 중계서버의 VNG 전용 읽기 주소에 물음(머리글 X-VNG-Token).
    지금 서버에 있는 주소: /vng/api/patients?q= · /vng/api/vest_orders?day= (평형검사 F631~F633 만).
    TSM300 이 이 토큰을 같이 써도 되는지, 중이검사 오더 주소를 만들지는 예진이 세션이 정함(확인 필요)."""

    def __init__(self, url, token, opener=None):
        self.base, self.token, self.used_dsn = server_url(url, YEJIN_PORT), (token or '').strip(), '예진이 서버'
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))     # 병원망 — 프록시 안 탐

    def _get(self, path, params):
        if not self.base or not self.token:
            raise RuntimeError('예진이 서버 주소 · 토큰이 비어 있습니다')
        req = urllib.request.Request(f'{self.base}{path}?{urllib.parse.urlencode(params)}',
                                     headers={'X-VNG-Token': self.token, 'User-Agent': 'TSM300'})
        try:
            with self.opener.open(req, timeout=8) as r:
                d = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read().decode('utf-8')).get('error') or ''
            except Exception:
                msg = ''
            raise RuntimeError(f'예진이 서버 {ex.code}: {msg or ex.reason}') from None
        except (urllib.error.URLError, OSError, socket.timeout) as ex:
            raise RuntimeError(f'예진이 서버({self.base})에 연결하지 못했습니다 ({getattr(ex, "reason", ex)})') from None
        except ValueError:
            raise RuntimeError(f'예진이 서버({self.base}) 응답을 읽지 못했습니다') from None
        if not isinstance(d, dict) or not d.get('ok'):
            raise RuntimeError(f"예진이 서버: {(d or {}).get('error') if isinstance(d, dict) else ''}".rstrip(': ') or '예진이 서버 오류')
        return d

    @staticmethod
    def _norm(p, how):
        try:
            age = int(p.get('age')) if p.get('age') not in (None, '') else None
        except (TypeError, ValueError):
            age = None
        sex = {'M': '남', 'F': '여'}.get(str(p.get('sex') or ''), str(p.get('sex') or ''))
        return dict(chart=str(p.get('chart') or '').strip(), code=None, name=str(p.get('name') or '').strip(),
                    birth=str(p.get('birth') or '').replace('.', '-'), age=age, sex=sex if sex in ('남', '여') else '',
                    how=how, last=str(p.get('last') or '')[:10])

    def search(self, q):
        q = re.sub(r'\s', '', q or '')[:20]
        if not q:
            return []
        return [x for x in (self._norm(p, p.get('how') or '') for p in self._get('/vng/api/patients', {'q': q}).get('patients') or []) if x['chart']]

    def find_patient(self, chart_no):
        chart_no = str(chart_no or '').strip()
        hit = [p for p in self.search(chart_no) if p['chart'] == chart_no and p['how'] == '차트번호']
        if len(hit) > 1:
            raise LookupError(f'차트번호 {chart_no} 환자가 {len(hit)}명입니다 — 사람이 확인해 주세요')
        return hit[0] if hit else None

    def todays_orders(self, order_codes=TYMP_CODES, day=None):
        raise NotImplementedError('예진이 서버에는 아직 평형검사(F631~F633) 오더 주소만 있습니다 — 중이검사 오더 주소는 예진이 세션에 요청')

    def close(self):
        pass


# ------------------------------------------------------------------ 3. 상쾌한캡처 결과지 보내기 (0.4.66 이상)
CAP_PORT = 5090
RETRY = {'connect', 'rejected', 'save_failed', 'emr_error', 'emr_off', 'demo'}     # 나중에 다시 보내면 되는 실패


class SendError(Exception):
    def __init__(self, code, msg, status=None, raw=''):
        super().__init__(msg); self.code, self.status, self.raw = code, status, raw

    @property
    def retry(self):
        return self.code in RETRY


def _json(b):
    try:
        d = json.loads(b.decode('utf-8-sig'))               # 상쾌한캡처 답 앞에 BOM 이 붙을 수 있음
        return d if isinstance(d, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def _snip(b, n=160):
    t = re.sub(r'<[^>]+>', ' ', b.decode('utf-8-sig', 'replace')); t = re.sub(r'\s+', ' ', t).strip()
    return t[:n]


def _cap_open(base, key, req, timeout):
    req.add_header('X-EntCapture-Key', key); req.add_header('User-Agent', 'TSM300')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            b = r.read(); d = _json(b)
            if d is None:
                raise SendError('reply', f'상쾌한캡처({base}) 답을 읽지 못했습니다', raw=_snip(b))
            return d
    except urllib.error.HTTPError as ex:
        try:
            b = ex.read()
        except Exception:
            b = b''
        d = _json(b) or {}
        code = d.get('code') or ('no_route' if ex.code == 404 else f'http{ex.code}')     # 코드 없는 404 = 옛 판 · 다른 PC
        raise SendError(code, d.get('error') or str(ex.reason), ex.code, raw=_snip(b) if not d.get('code') else '') from None
    except (urllib.error.URLError, OSError, socket.timeout) as ex:
        raise SendError('connect', f'상쾌한캡처({base})에 연결하지 못했습니다 ({getattr(ex, "reason", ex)})') from None


def entcapture_hello(url, key, timeout=10):
    """연결 시험 → {'device': 장비 이름, 'pc': 받는 PC 이름}."""
    base = server_url(url, CAP_PORT)
    if not base or not key:
        raise SendError('setup', '상쾌한캡처 주소 · 키가 비어 있습니다')
    d = _cap_open(base, key, urllib.request.Request(base + '/api/hello'), timeout)
    if 'device' not in d:
        raise SendError('old_version', '검사 장비 답이 아닙니다(상쾌한캡처 0.4.66 이상 필요)', raw=json.dumps(d, ensure_ascii=False)[:160])
    return d


def save_to_entcapture(image_path, patient, taken_at, kind='tymp', *, url, key, note='', timeout=30):
    """결과지 JPG(또는 PDF) 하나 → 진료실 PC 상쾌한캡처. patient = {'chart' 화면 차트번호, 'name' 이름(대조용, 꼭), 'birth' YYYY-MM-DD(선택)}.
    taken_at = 검사일(date · datetime · 'YYYY-MM-DD…'). 성공하면 상쾌한캡처 답 dict, 실패는 SendError(.code, .retry)."""
    base, key = server_url(url, CAP_PORT), (key or '').strip()
    if not base or not key:
        raise SendError('setup', '상쾌한캡처 주소 · 키가 비어 있습니다')
    name = str(patient.get('name') or '').strip()
    if not name:
        raise SendError('name_missing', '환자 이름을 몰라 보낼 수 없습니다(상쾌한캡처가 이름으로 대조함)')
    day = taken_at.strftime('%Y-%m-%d') if hasattr(taken_at, 'strftime') else str(taken_at)[:10]
    with open(image_path, 'rb') as fh:
        data = fh.read()
    is_pdf = data[:5] == b'%PDF-'
    fields = [('chart', str(patient.get('chart') or '').strip()), ('date', day), ('exam', kind), ('note', note), ('name', name)]
    if patient.get('birth'):
        fields.append(('birth', str(patient['birth'])[:10]))
    bd = uuid.uuid4().hex; parts = []
    for k, v in fields:
        parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode('utf-8') + b'\r\n')
    fname, ctype = ('result.pdf', 'application/pdf') if is_pdf else ('result.jpg', 'image/jpeg')
    parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                 + data + b'\r\n')
    body = b''.join(parts) + f'--{bd}--\r\n'.encode()

    def req():
        return urllib.request.Request(base + '/api/results', data=body, method='POST',
                                      headers={'Content-Type': f'multipart/form-data; boundary={bd}'})
    try:
        d = _cap_open(base, key, req(), timeout)
    except SendError as ex:
        if ex.code != 'connect':
            raise
        # 받는 쪽이 본문을 다 읽기 전에 거절(401 · 503 등)하면 윈도에선 '연결 끊김'으로 보임 → 연결 시험으로 진짜 이유를 보고, 두 번 더
        try:
            entcapture_hello(base, key)
        except SendError as h:
            if h.code != 'connect':
                raise h from None
            raise ex from None
        d = None
        for _ in range(2):
            time.sleep(0.3)
            try:
                d = _cap_open(base, key, req(), timeout); break
            except SendError as ex2:
                if ex2.code != 'connect':
                    raise
        if d is None:
            raise SendError('rejected', '상쾌한캡처가 받지 않고 끊었습니다') from None
    if not d.get('ok'):
        raise SendError(d.get('code') or 'reply', d.get('error') or '상쾌한캡처 오류')
    return d
