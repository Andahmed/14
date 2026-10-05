# -*- coding: utf-8 -*-
# ★ للتعديل: افتح EDIT_MAP.txt - ابحث في الملف ده عن [EDIT-MAP] عشان توصل للأماكن المهمة ★
"""
سيرفر نقطة البيع - إستاكوزا  (Istakoza POS server)
- بايثون فقط (من غير مكتبات خارجية): http.server + sqlite3
- بيقدّم الصفحة pos_istakoza.html وبيحفظ كل البيانات في قاعدة SQLite (pos_data.db)
- مكان البيانات: C:\\ProgramData\\Istakoza  (لما يتشغل كـ exe)  أو فولدر البرنامج (لما يتشغل من السورس)
  ممكن تغييره بمتغير البيئة ISTAKOZA_DATA، والبورت بـ ISTAKOZA_PORT (الافتراضي 8080)
"""
import time
import os, sys, re, json, base64, hashlib, secrets, threading, queue, socket, ipaddress, sqlite3, webbrowser, subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ★★★ [EDIT-MAP] رقم إصدار السيرفر (لو غيّرته غيّر معاه pos-ui-version في أول الصفحة) ★★★
VERSION = 13
FROZEN = getattr(sys, 'frozen', False)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
# MODE: cashier = كاشير+صالة+استلام فرع فقط | backoffice = مخازن+كل الأوردرات+دليفري (من غير نافذة الكاشير)
MODE = os.environ.get('ISTAKOZA_MODE', 'full').lower().strip()
if MODE not in ('cashier', 'backoffice', 'full'):
    MODE = 'full'


def data_dir():
    d = os.environ.get('ISTAKOZA_DATA')
    if not d and os.name == 'nt':
        d = os.path.join(os.environ.get('PROGRAMDATA', APP_DIR), 'Istakoza')
    d = d or APP_DIR
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(rel):
    # لو شغال كـ exe: الصفحة متحشورة جوه الـ exe (PyInstaller --add-data) وبتتفك في sys._MEIPASS
    base = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def ui_version(path):
    try:
        m = re.search(r'name="pos-ui-version" content="(\d+)"', open(path, encoding='utf-8').read(65536))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def find_html():
    # 1) نسخة معدّلة اختيارية في فولدر الداتا (تتجاهل لو أقدم من السيرفر عشان ماتبوّظش التوافق)
    # 2) الصفحة المتحشورة جوه الـ exe  3) ملف جنب البرنامج (للتشغيل من السورس)
    custom = os.path.join(data_dir(), 'pos_istakoza.html')
    if os.path.isfile(custom):
        if ui_version(custom) >= VERSION:
            return custom
        print('تجاهل pos_istakoza.html القديمة في فولدر الداتا (إصدارها أقدم من السيرفر):', custom)
    for p in (resource_path('pos_istakoza.html'), os.path.join(APP_DIR, 'pos_istakoza.html')):
        if os.path.isfile(p):
            return p
    return resource_path('pos_istakoza.html')


DB = os.path.join(data_dir(), 'pos_data.db')
HTML = find_html()
PORT = int(os.environ.get('ISTAKOZA_PORT', '8080'))
LOCK = threading.Lock()
TOK = {}
SUBS, SUBS_LOCK = set(), threading.Lock()


def now_s():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------------ live events (SSE)
def broadcast(ev):
    with SUBS_LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ------------------------------------------------------------------ database
# ★★★ [EDIT-MAP] جداول قاعدة البيانات (SQLite) ★★★
T = {
    'Products': 'PID INTEGER PRIMARY KEY,PName TEXT,Cat TEXT,Unit TEXT,Price REAL,Icon TEXT,Code TEXT,Hidden INTEGER DEFAULT 0,Cost REAL DEFAULT 0',
    'Stock': 'PID INTEGER PRIMARY KEY,Qty REAL',
    'Users': 'UName TEXT PRIMARY KEY,PHash TEXT,Role TEXT,FullName TEXT',
    'Settings': 'K TEXT PRIMARY KEY,V TEXT',
    'Sales': 'SID TEXT PRIMARY KEY,SNo INTEGER,SDay TEXT,STime TEXT,SHour INTEGER,SType TEXT,Cashier TEXT,CName TEXT,CPhone TEXT,CAddr TEXT,SSub REAL,SDisc REAL,STax REAL,SFee REAL,STotal REAL,Closed INTEGER DEFAULT 0',
    'SaleItems': 'SID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL',
    # v2: سجل الإلغاء والتعديل
    # v3: قفل الأيام بعد التقفيل النهائي
    'day_locks': 'day TEXT PRIMARY KEY,locked_at TEXT,locked_by TEXT',
    # v4: مرتجع + طلبيات الشراء
    'Returns': 'RID TEXT PRIMARY KEY,RNo INTEGER,SID TEXT,SNo INTEGER,RDay TEXT,RTime TEXT,Reason TEXT,Cashier TEXT,Manager TEXT,Total REAL,Status TEXT,PaidBy TEXT,PaidAt TEXT,Log TEXT',
    'ReturnItems': 'RID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL,Refund REAL',
    'Suppliers': 'SupID INTEGER PRIMARY KEY AUTOINCREMENT,SName TEXT,Phone TEXT,Note TEXT',
    'StoreItems': 'Code TEXT PRIMARY KEY,IName TEXT,Unit TEXT,Cat TEXT,Price REAL,SupID INTEGER',
    'PoTemplates': 'TID INTEGER PRIMARY KEY AUTOINCREMENT,TName TEXT,Items TEXT',
    'POrders': 'POID TEXT PRIMARY KEY,PONo TEXT,PODay TEXT,POTime TEXT,Supplier TEXT,Total REAL,Status TEXT,CreatedBy TEXT,SentAt TEXT,Note TEXT,Err TEXT',
    'POItems': 'POID TEXT,Code TEXT,IName TEXT,Unit TEXT,Qty REAL,Price REAL',
    # v6: مخزن الأكواد (بيزيد لما الطلبية تتسجل "تم الاستلام")
    'StoreStock': 'Code TEXT PRIMARY KEY,Qty REAL',
    # v9: جرد الشهر (رصيد أول المدة + وارد الطلبيات - المبيعات = المتوقع، والمقارنة بالعد الفعلي)
    'Stocktake': 'Month TEXT,PID INTEGER,Opening REAL,Actual REAL,PRIMARY KEY(Month,PID)',
    # v10: الريسبي (مكونات الصنف من الخامات) + تسويات الجرد + عدّ الجرد بالكود
    # v11: الدليفري (مناديب / شفتات / تحميل الأوردرات / سجل)
    # v12: نظام الموظفين + توحيد delivery + أعمدة OrderType/Action/State
    'Jobs': 'JobNo INTEGER PRIMARY KEY,JobName TEXT NOT NULL,Active INTEGER DEFAULT 1',
    'Employees': 'EmpNo INTEGER PRIMARY KEY AUTOINCREMENT,Name TEXT NOT NULL,JobNo INTEGER NOT NULL,Phone TEXT,Password TEXT NOT NULL,Active INTEGER DEFAULT 1,CreatedAt TEXT',
    'Permissions': 'JobNo INTEGER NOT NULL,Perm TEXT NOT NULL,Allowed INTEGER DEFAULT 0,PRIMARY KEY(JobNo,Perm)',
    'Deliveries': 'DID INTEGER PRIMARY KEY AUTOINCREMENT,DName TEXT,Phone TEXT,Active INTEGER DEFAULT 1',
    'DeliveryShifts': 'ShiftID INTEGER PRIMARY KEY AUTOINCREMENT,DID INTEGER,StartAt TEXT,StartCash REAL,EndAt TEXT,Status TEXT,Collected REAL,Expected REAL,Actual REAL,Diff REAL,Orders INTEGER,Fees REAL,ClosedBy TEXT,Note TEXT',
    'Dispatch': 'DispID INTEGER PRIMARY KEY AUTOINCREMENT,SaleID TEXT,DID INTEGER,ShiftID INTEGER,OutAt INTEGER,BackAt INTEGER,Status TEXT,ByUser TEXT,Refused INTEGER DEFAULT 0,DeliveredAt TEXT',
    'DeliveryLog': 'LID INTEGER PRIMARY KEY AUTOINCREMENT,DID INTEGER,Act TEXT,At TEXT,ByUser TEXT,Detail TEXT',
    # v13 ph4: سجل محاولات الدخول (للقفل بعد 5 محاولات غلط)
    'LoginLog': 'LID INTEGER PRIMARY KEY AUTOINCREMENT,UKey TEXT,UName TEXT,EmpNo INTEGER,At TEXT,AtMs INTEGER,OK INTEGER,IP TEXT,Note TEXT',
    'Recipe': 'PID INTEGER,Code TEXT,Qty REAL,PRIMARY KEY(PID,Code)',
    'StoreAdj': 'Month TEXT,Code TEXT,Kind TEXT,Day TEXT,Qty REAL,PRIMARY KEY(Month,Code,Kind)',
    'StockCount': 'Month TEXT,Code TEXT,Opening REAL,Actual REAL,PRIMARY KEY(Month,Code)',
    'StocktakeMeta': 'Month TEXT PRIMARY KEY,Status TEXT,ClosedBy TEXT,ClosedAt TEXT',
    'SaleLog': 'LID INTEGER PRIMARY KEY AUTOINCREMENT,SID TEXT,Act TEXT,ByUser TEXT,ByEmpNo INTEGER,Reason TEXT,At TEXT,Detail TEXT',
}
# v2: أعمدة جديدة في Sales (بتتضاف تلقائياً لقاعدة بيانات قديمة)
SALES_NEW = {'Void': 'INTEGER DEFAULT 0', 'VReason': 'TEXT', 'VBy': 'TEXT', 'VAt': 'TEXT',
             'EBy': 'TEXT', 'EAt': 'TEXT', 'ECount': 'INTEGER DEFAULT 0', 'Comment': 'TEXT', 'Returned': 'INTEGER DEFAULT 0', 'Branch': 'TEXT',
             # v12
             'OrderType': 'TEXT', 'OrderAction': 'TEXT', 'OrderState': 'TEXT', 'PayPlace': 'TEXT', 'EmpNo': 'INTEGER',
             'CashierNo': 'INTEGER', 'ManagerNo': 'INTEGER'}
# أعمدة جديدة في جداول أخرى
RETURNS_NEW = {'CashierNo': 'INTEGER', 'ManagerNo': 'INTEGER'}
PORDERS_NEW = {'CreatedByNo': 'INTEGER'}
DISPATCH_NEW = {'Refused': 'INTEGER DEFAULT 0', 'DeliveredAt': 'TEXT'}
# v13: ربط تسجيل الدخول والصلاحيات بجدول Employees / Permissions
EMP_NEW = {'UName': 'TEXT', 'PHash': 'TEXT'}
JOB_ROLE = {1: 'admin', 2: 'admin', 3: 'cashier', 4: 'cashier', 5: 'delivery'}   # JobNo -> role (المناديب ملهمش دخول)
ROLE_JOB = {'admin': 2, 'cashier': 3, 'delivery': 5}
ALL_PERMS = ['disc', 'del', 'stock', 'rep', 'close', 'printer', 'reprint', 'price', 'void', 'edit', 'return', 'po', 'dlv', 'dlvPay']


PBKDF2_ITERS = 150000


def hp_legacy(u, p):
    """الهاش القديم SHA-256 (للحسابات القديمة وكـ fallback لو pbkdf2 مش متاح)"""
    return hashlib.sha256(('istakoza|' + u.lower() + '|' + p).encode('utf-8')).hexdigest()


def hp(u, p):
    """هاش الباسورد: PBKDF2-HMAC-SHA256 بملح عشوائي. لو الدالة مش متاحة في البايثون بيرجع للـ SHA-256 القديم."""
    try:
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac('sha256', (u.lower() + '|' + p).encode('utf-8'), salt, PBKDF2_ITERS)
        return 'pbkdf2$sha256$%d$%s$%s' % (PBKDF2_ITERS, salt.hex(), h.hex())
    except (AttributeError, ValueError):
        return hp_legacy(u, p)


def vp(u, p, stored):
    """تحقق من الباسورد مقابل الهاش المخزّن (جديد PBKDF2 أو قديم SHA-256)"""
    if not stored:
        return False
    if stored.startswith('pbkdf2$'):
        try:
            _, alg, it, salt, hx = stored.split('$')
            h = hashlib.pbkdf2_hmac(alg, (u.lower() + '|' + p).encode('utf-8'), bytes.fromhex(salt), int(it))
            return secrets.compare_digest(h.hex(), hx)
        except Exception:
            return False
    return secrets.compare_digest(stored, hp_legacy(u, p))


def hp_is_legacy(stored):
    return bool(stored) and not stored.startswith('pbkdf2$')


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        for n, d in T.items():
            c.execute('CREATE TABLE IF NOT EXISTS %s (%s)' % (n, d))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Products)')}
        if 'hidden' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Hidden INTEGER DEFAULT 0')
        if 'code' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Code TEXT')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Sales)')}
        for col, typ in SALES_NEW.items():
            if col.lower() not in have:
                c.execute('ALTER TABLE Sales ADD COLUMN %s %s' % (col, typ))
        # v6: حالة الطلبية (جاري التنفيذ / تم الاستلام / ملغاة) + الكمية المستلمة فعلاً
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(POrders)')}
        for col, typ in (('Stage', "TEXT DEFAULT 'progress'"), ('RecvAt', 'TEXT'), ('RecvBy', 'TEXT')):
            if col.lower() not in have:
                c.execute('ALTER TABLE POrders ADD COLUMN %s %s' % (col, typ))
        for col, typ in PORDERS_NEW.items():
            if col.lower() not in have:
                c.execute('ALTER TABLE POrders ADD COLUMN %s %s' % (col, typ))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(POItems)')}
        if 'rqty' not in have:
            c.execute('ALTER TABLE POItems ADD COLUMN RQty REAL')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Products)')}
        if 'cost' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Cost REAL DEFAULT 0')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(StoreItems)')}
        if 'src' not in have:
            c.execute('ALTER TABLE StoreItems ADD COLUMN Src TEXT')
        # v12 Returns / Dispatch / SaleLog new cols
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Returns)')}
        for col, typ in RETURNS_NEW.items():
            if col.lower() not in have:
                c.execute('ALTER TABLE Returns ADD COLUMN %s %s' % (col, typ))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Dispatch)')}
        for col, typ in DISPATCH_NEW.items():
            if col.lower() not in have:
                try:
                    c.execute('ALTER TABLE Dispatch ADD COLUMN %s %s' % (col, typ))
                except Exception:
                    pass
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(SaleLog)')}
        if 'byempno' not in have:
            try:
                c.execute('ALTER TABLE SaleLog ADD COLUMN ByEmpNo INTEGER')
            except Exception:
                pass
        c.execute('CREATE INDEX IF NOT EXISTS ix_items_sid ON SaleItems(SID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_sales_day ON Sales(SDay)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_log_sid ON SaleLog(SID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_loginlog_k ON LoginLog(UKey,AtMs)')
        c.execute('DELETE FROM LoginLog WHERE AtMs<?', (int(time.time() * 1000) - 90 * 86400 * 1000,))
        # أيام اتقفلت قبل التحديث ده: نسجّلها في day_locks
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'migration' FROM Sales WHERE Closed=1", (now_s(),))
        if c.execute('SELECT COUNT(*) FROM Users').fetchone()[0] == 0:
            c.execute('INSERT INTO Users VALUES (?,?,?,?)', ('admin', hp('admin', 'admin'), 'admin', 'المدير'))
        # ===== v12 migration: Jobs seed + Users/Deliveries → Employees + Deliveries→Deliveries =====
        if c.execute('SELECT COUNT(*) FROM Jobs').fetchone()[0] == 0:
            c.executemany('INSERT INTO Jobs (JobNo,JobName,Active) VALUES (?,?,1)', [
                (1, 'IT'), (2, 'manager'), (3, 'cashier'), (4, 'waiter'), (5, 'delivery')])
        # migrate old Deliveries table if exists
        old_tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'Riders' in old_tables and c.execute('SELECT COUNT(*) FROM Deliveries').fetchone()[0] == 0:
            try:
                c.execute('INSERT INTO Deliveries (DID,DName,Phone,Active) SELECT RID,RName,Phone,Active FROM Riders')
            except Exception:
                pass
        if 'RiderShifts' in old_tables and c.execute('SELECT COUNT(*) FROM DeliveryShifts').fetchone()[0] == 0:
            try:
                c.execute('INSERT INTO DeliveryShifts (ShiftID,DID,StartAt,StartCash,EndAt,Status,Collected,Expected,Actual,Diff,Orders,Fees,ClosedBy,Note) SELECT ShiftID,RID,StartAt,StartCash,EndAt,Status,Collected,Expected,Actual,Diff,Orders,Fees,ClosedBy,Note FROM RiderShifts')
            except Exception:
                pass
        if 'RiderLog' in old_tables and c.execute('SELECT COUNT(*) FROM DeliveryLog').fetchone()[0] == 0:
            try:
                c.execute('INSERT INTO DeliveryLog (LID,DID,Act,At,ByUser,Detail) SELECT LID,RID,Act,At,ByUser,Detail FROM RiderLog')
            except Exception:
                pass
        # migrate Users → Employees
        if c.execute('SELECT COUNT(*) FROM Employees').fetchone()[0] == 0:
            for u, role, full in c.execute('SELECT UName,Role,FullName FROM Users'):
                jn = 2 if role == 'admin' else (3 if role == 'cashier' else 1)
                try:
                    c.execute('INSERT INTO Employees (Name,JobNo,Phone,Password,Active,CreatedAt) VALUES (?,?,?,?,1,?)',
                              (full or u, jn, '', u, now_s()))
                except Exception:
                    pass
            for r in c.execute('SELECT DName,Phone FROM Deliveries'):
                try:
                    c.execute('INSERT INTO Employees (Name,JobNo,Phone,Password,Active,CreatedAt) VALUES (?,?,?,?,1,?)',
                              (r[0], 5, r[1] or '', '1234', now_s()))
                except Exception:
                    pass

        # ===== v13: Employees/Permissions هما المصدر الوحيد للدخول والصلاحيات =====
        wire_employees(c)
        seed_permissions(c)

        # old sales defaults
        c.execute("UPDATE Sales SET OrderType='C', OrderAction='', OrderState='F', PayPlace='C' WHERE OrderType IS NULL OR OrderType=''")


def get_cfg(c):
    row = c.execute("SELECT V FROM Settings WHERE K='cfg'").fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


# ★★★ [EDIT-MAP] الصلاحيات الافتراضية للكاشير ★★★
PERM_DEF = {'dlv': 1, 'return': 1, 'reprint': 1, 'price': 1, 'del': 1, 'disc': 1}


def wire_employees(c):
    """يضيف UName/PHash لـ Employees وينقل Users القديم مرة واحدة (بنفس هاش الباسورد)"""
    have = {r[1].lower() for r in c.execute('PRAGMA table_info(Employees)')}
    for col, typ in EMP_NEW.items():
        if col.lower() not in have:
            c.execute('ALTER TABLE Employees ADD COLUMN %s %s' % (col, typ))
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_emp_uname ON Employees(UName)')
    if c.execute('PRAGMA user_version').fetchone()[0] < 13:   # مرة واحدة بس، عشان اللي يتحذف ما يرجعش
        for u, ph, role, full in c.execute('SELECT UName,PHash,Role,FullName FROM Users').fetchall():
            jn = ROLE_JOB.get(role, 3)
            if c.execute('SELECT 1 FROM Employees WHERE UName=?', (u,)).fetchone():
                continue
            r = c.execute('SELECT EmpNo FROM Employees WHERE UName IS NULL AND (Name=? OR Name=?) ORDER BY EmpNo LIMIT 1',
                          (full or u, u)).fetchone()
            if r:
                c.execute("UPDATE Employees SET UName=?,PHash=?,JobNo=?,Password='' WHERE EmpNo=?", (u, ph, jn, r[0]))
            else:
                c.execute("INSERT INTO Employees (Name,JobNo,Phone,Password,Active,CreatedAt,UName,PHash) VALUES (?,?,?,?,1,?,?,?)",
                          (full or u, jn, '', '', now_s(), u, ph))
        c.execute('PRAGMA user_version=13')
    # لازم يفضل فيه مدير واحد على الأقل يقدر يدخل
    if not c.execute('SELECT 1 FROM Employees WHERE JobNo IN (1,2) AND Active=1 AND UName IS NOT NULL AND PHash IS NOT NULL').fetchone():
        c.execute("DELETE FROM Employees WHERE UName='admin'")
        c.execute("INSERT INTO Employees (Name,JobNo,Phone,Password,Active,CreatedAt,UName,PHash) VALUES (?,?,?,?,1,?,?,?)",
                  ('المدير', 2, '', '', now_s(), 'admin', hp('admin', 'admin')))


def seed_permissions(c):
    """بيملا Permissions لأول مرة (INSERT OR IGNORE - مش بيمسح تعديلات المدير). الكاشير بياخد القيم الحالية من cfg.perm"""
    cfgp = get_cfg(c).get('perm') or {}
    for (jn,) in c.execute('SELECT JobNo FROM Jobs').fetchall():
        for k in ALL_PERMS:
            if jn in (1, 2):
                v = 1
            elif jn == 3:
                v = 1 if cfgp.get(k, PERM_DEF.get(k, 0)) else 0
            else:
                v = 0
            c.execute('INSERT OR IGNORE INTO Permissions (JobNo,Perm,Allowed) VALUES (?,?,?)', (jn, k, v))


def emp_perms(job):
    with db() as c:
        rows = dict(c.execute('SELECT Perm,Allowed FROM Permissions WHERE JobNo=?', (job,)).fetchall())
    return {k: int(rows.get(k, 0) or 0) for k in ALL_PERMS}


def can(user, perm):
    """المدير/IT يقدر على كل حاجة، والباقي بصلاحيات الوظيفة من جدول Permissions (لو مفيش صف = ممنوع)"""
    if user['role'] == 'admin':
        return True
    with db() as c:
        r = c.execute('SELECT Allowed FROM Permissions WHERE JobNo=? AND Perm=?', (user.get('job'), perm)).fetchone()
    return bool(r[0]) if r else False


SALE_COLS = ('SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
             'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned,Branch,'
             'OrderType,OrderAction,OrderState,PayPlace,EmpNo')


def row_to_sale(r, items):
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], h=r[4], type=r[5], by=r[6],
                sub=r[10], disc=r[11], tax=r[12], fee=r[13], total=r[14], items=items,
                cust=dict(name=r[7], phone=r[8], addr=r[9]) if r[5] in ('delivery', 'pickup') else None,
                void=int(r[16] or 0), vreason=r[17] or '', vby=r[18] or '', vat=r[19] or '',
                eby=r[20] or '', eat=r[21] or '', ecount=int(r[22] or 0), comment=r[23] or '', returned=int(r[24] or 0), branch=r[25] or '',
                orderType=r[26] or '', orderAction=r[27] or '', orderState=r[28] or '', payPlace=r[29] or '', empNo=r[30])


# ★★★ [EDIT-MAP] اللي بيتبعت للصفحة (أصناف، فواتير، ريسبي...) ★★★
def get_state():
    with db() as c:
        prods = [dict(id=r[0], name=r[1], cat=r[2], unit=r[3], price=r[4], icon=r[5], code=r[6], hidden=bool(r[7]), cost=r[8] or 0)
                 for r in c.execute('SELECT PID,PName,Cat,Unit,Price,Icon,Code,Hidden,Cost FROM Products ORDER BY PID')]
        items = {}
        for r in c.execute('SELECT SID,PID,IName,Cat,Unit,Qty,Price FROM SaleItems'):
            items.setdefault(r[0], []).append(dict(id=r[1], name=r[2], cat=r[3], unit=r[4], qty=r[5], price=r[6]))
        locked = {r[0] for r in c.execute('SELECT day FROM day_locks')}
        S, A = [], []
        for r in c.execute('SELECT %s FROM Sales ORDER BY SDay,SID' % SALE_COLS):
            sl = row_to_sale(r, items.get(r[0], []))
            sl['locked'] = r[2] in locked
            (A if r[15] else S).append(sl)
        stock = {str(r[0]): r[1] for r in c.execute('SELECT PID,Qty FROM Stock')}
        sync_fish(c)
        seed_prices(c)
        rc, cs, ad = mat_move(c, '0000-00-00', '9999-99-99', closed_only=True)
        mbase = {}
        for (code,) in c.execute('SELECT Code FROM StoreItems').fetchall():
            k = str(code)
            mbase[k] = round(rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0), 3)
        recipes = {}
        for pid, code, q in c.execute('SELECT PID,Code,Qty FROM Recipe ORDER BY rowid'):
            recipes.setdefault(str(pid), []).append(dict(code=str(code), qty=q))
        return dict(products=prods, sales=S, archive=A, stock=stock, cfg=get_cfg(c), ver=VERSION, returns=list_returns(c),
                    recipes=recipes, mbase=mbase)


# ★★★ [EDIT-MAP] حفظ الأصناف والأسعار والإعدادات (فحص الصلاحيات هنا) ★★★
def save_state(d, role):
    with LOCK, db() as c:
        if role == 'admin':
            c.execute('DELETE FROM Products')
            c.executemany('INSERT INTO Products (PID,PName,Cat,Unit,Price,Icon,Code,Hidden,Cost) VALUES (?,?,?,?,?,?,?,?,?)',
                          [(int(p['id']), p['name'], p.get('cat', ''), p.get('unit', 'pc'), float(p.get('price') or 0),
                            p.get('icon', ''), str(p.get('code') or ''), 1 if p.get('hidden') else 0, float(p.get('cost') or 0))
                           for p in d.get('products', [])])
            c.execute('DELETE FROM Stock')
            c.executemany('INSERT INTO Stock VALUES (?,?)', [(int(k), float(v)) for k, v in (d.get('stock') or {}).items()])
            c.execute('DELETE FROM Settings')
            c.execute('INSERT INTO Settings VALUES (?,?)', ('cfg', json.dumps(d.get('cfg') or {}, ensure_ascii=False)))
            # شاشة "صلاحيات الكاشير" بتكتب في cfg.perm → بننسخها لجدول Permissions (وظيفة الكاشير = 3)
            for k, v in ((d.get('cfg') or {}).get('perm') or {}).items():
                if k in ALL_PERMS:
                    c.execute('INSERT OR REPLACE INTO Permissions (JobNo,Perm,Allowed) VALUES (3,?,?)', (k, 1 if v else 0))
        elif role != 'none':
            for p in d.get('products', []):
                c.execute('UPDATE Products SET Price=? WHERE PID=?', (float(p.get('price') or 0), int(p['id'])))


def ins_sale(c, s, closed=0):
    cu = s.get('cust') or {}
    stype = s.get('type', 'cash')
    # OrderType: C=كاشير V=دليفري A=استلام فرع D=صالة
    otype = 'A' if stype == 'pickup' else (s.get('orderType') or ('V' if stype == 'delivery' else 'C'))
    if stype == 'pickup' and s.get('fee'):
        # استلام فرع: مفيش رسوم توصيل أبداً (حتى لو الصفحة بعتتها)
        s['total'] = round(float(s['total']) - float(s['fee']), 2)
        s['fee'] = 0
    c.execute('INSERT INTO Sales (SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
              'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned,Branch,OrderType,OrderAction,OrderState,PayPlace,EmpNo) '
              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (s['id'], s['no'], s['day'], s['time'], s.get('h', 0), stype, s.get('by', ''),
               cu.get('name', ''), cu.get('phone', ''), cu.get('addr', ''), s['sub'], s['disc'], s.get('tax', 0),
               s.get('fee', 0), s['total'], closed, 1 if s.get('void') else 0, s.get('vreason', ''), s.get('vby', ''),
               s.get('vat', ''), s.get('eby', ''), s.get('eat', ''), s.get('ecount', 0), s.get('comment', ''), s.get('returned', 0), s.get('branch', ''),
               otype, s.get('orderAction') or '', s.get('orderState') or '', otype, s.get('empNo')))
    put_items(c, s['id'], s['items'])


def put_items(c, sid, items):
    c.executemany('INSERT INTO SaleItems VALUES (?,?,?,?,?,?,?)',
                  [(sid, i['id'], i['name'], i.get('cat') or '', i['unit'], i['qty'], i['price']) for i in items])


# ★★★ [EDIT-MAP] حفظ الفاتورة: الترقيم + الوقت من السيرفر + خصم الخامات ★★★
def add_sale(d, user):
    # الترقيم بيتم هنا جوه السيرفر (داخل قفل) عشان جهازين مايطلعوش نفس رقم الفاتورة
    with LOCK, db() as c:
        ex = c.execute('SELECT SNo FROM Sales WHERE SID=?', (d['id'],)).fetchone()
        if ex:
            return {'ok': 1, 'no': ex[0], 'id': d['id'], 'dup': 1}
        d['no'] = (c.execute('SELECT MAX(SNo) FROM Sales WHERE Closed=0').fetchone()[0] or 0) + 1
        _n = datetime.now()
        d['day'], d['time'], d['h'] = _n.strftime('%Y-%m-%d'), _n.strftime('%H:%M:%S'), _n.hour
        d['by'] = user['u']
        d['empNo'] = user.get('emp')
        d['branch'] = str(get_cfg(c).get('branchCode') or '')
        ins_sale(c, d)
    return {'ok': 1, 'no': d['no'], 'id': d['id']}


# ------------------------------------------------------------------ void / edit (v2) + قفل الأيام (v3)
LOCK_MSG = '🚫 غير مسموح! هذا اليوم تم تقفيله رسمياً. استخدم شاشة المرتجع بتاريخ اليوم.'


class Forbidden(Exception):
    pass


def check_unlocked(c, day):
    if c.execute('SELECT 1 FROM day_locks WHERE day=?', (day,)).fetchone():
        raise Forbidden(LOCK_MSG)
def log(c, sid, act, user, reason, detail):
    c.execute('INSERT INTO SaleLog (SID,Act,ByUser,Reason,At,Detail) VALUES (?,?,?,?,?,?)',
              (sid, act, user['u'], reason, now_s(), json.dumps(detail, ensure_ascii=False)))


def snapshot(c, sid):
    r = c.execute('SELECT %s FROM Sales WHERE SID=?' % SALE_COLS, (sid,)).fetchone()
    if not r:
        raise Exception('الأوردر غير موجود')
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price FROM SaleItems WHERE SID=?', (sid,))]
    return row_to_sale(r, items), bool(r[15])


def stock_back(c, old_items, new_items):
    """أوردر من يوم اتقفل: المخزن اتخصم منه وقت القفل، فلازم نرجّع الفرق (المخزن للأصناف اللي ليها رصيد بس)"""
    delta = {}
    for i in old_items:
        delta[i['id']] = delta.get(i['id'], 0) + i['qty']
    for i in new_items:
        delta[i['id']] = delta.get(i['id'], 0) - i['qty']
    for pid, q in delta.items():
        if abs(q) > 1e-9:
            c.execute('UPDATE Stock SET Qty=Qty+? WHERE PID=?', (round(q, 3), pid))


# ★★★ [EDIT-MAP] إلغاء أوردر + قفل الأيام ★★★
def do_void(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب الإلغاء')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتلغي')
        if c.execute("SELECT 1 FROM Dispatch WHERE SaleID=? AND Status IN ('out','delivered')", (d['id'],)).fetchone():
            raise Exception('الأوردر متحمّل على مندوب - اسحبه الأول من شاشة الدليفري')
        if s['void']:
            raise Exception('الأوردر ملغي بالفعل')
        if s.get('orderState') == 'F':
            raise Exception('OrderState=F لأوردر ملغي = خطأ — الأوردر متسلّم بالفعل')
        c.execute("UPDATE Sales SET Void=1,VReason=?,VBy=?,VAt=?,OrderAction='C' WHERE SID=?", (reason, user['u'], now_s(), d['id']))
        if closed:
            stock_back(c, s['items'], [])
        log(c, d['id'], 'void', user, reason, {'before': s})


# ★★★ [EDIT-MAP] تعديل أوردر ★★★
def do_edit(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب التعديل')
    items = []
    for i in d.get('items') or []:
        q, p = float(i['qty']), float(i['price'])
        if q <= 0 or p < 0:
            raise Exception('كمية أو سعر غير صحيح')
        items.append(dict(id=int(i['id']), name=str(i['name']), cat=str(i.get('cat') or ''),
                          unit='kg' if i.get('unit') == 'kg' else 'pc', qty=round(q, 3), price=p))
    if not items:
        raise Exception('الأوردر لازم يفضل فيه صنف واحد على الأقل (للإلغاء استخدم Void)')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتعدل')
        if c.execute("SELECT 1 FROM Dispatch WHERE SaleID=? AND Status IN ('out','delivered')", (d['id'],)).fetchone():
            raise Exception('الأوردر متحمّل على مندوب - اسحبه الأول من شاشة الدليفري')
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعدل')
        cu = d.get('cust') or {}
        if s['type'] == 'delivery':
            cn, cp, ca = cu.get('name', ''), cu.get('phone', ''), cu.get('addr', '')
        elif s['type'] == 'pickup':
            cn, cp, ca = cu.get('name', ''), cu.get('phone', ''), ''
            if d.get('fee'):
                d['total'] = round(float(d['total']) - float(d['fee']), 2)
            d['fee'] = 0
        else:
            cn, cp, ca = '', '', ''
        c.execute('UPDATE Sales SET SSub=?,SDisc=?,STax=?,SFee=?,STotal=?,CName=?,CPhone=?,CAddr=?,EBy=?,EAt=?,ECount=ECount+1,Comment=? WHERE SID=?',
                  (float(d['sub']), float(d['disc']), float(d.get('tax', 0)), float(d.get('fee', 0)), float(d['total']),
                   cn, cp, ca, user['u'], now_s(), str(d.get('comment', s['comment']) or '').strip(), d['id']))
        c.execute('DELETE FROM SaleItems WHERE SID=?', (d['id'],))
        put_items(c, d['id'], items)
        if closed:
            stock_back(c, s['items'], items)
        after, _ = snapshot(c, d['id'])
        log(c, d['id'], 'edit', user, reason, {'before': s, 'after': after})


def get_log(sid):
    with db() as c:
        return [dict(act=r[0], by=r[1], reason=r[2], at=r[3])
                for r in c.execute('SELECT Act,ByUser,Reason,At FROM SaleLog WHERE SID=? ORDER BY LID', (sid,))]


# ------------------------------------------------------------------ misc
# ★★★ [EDIT-MAP] الطباعة على طابعة الشبكة ★★★
def do_print(d):
    """يبعت بيانات ESC/POS خام للطابعة على الشبكة (بورت 9100)"""
    ip = str(d.get('ip', '')).strip()
    port = int(d.get('port') or 9100)
    try:
        a = ipaddress.ip_address(ip)
    except Exception:
        raise Exception('عنوان IP غير صحيح')
    if not (a.is_private or a.is_loopback):
        raise Exception('الـ IP لازم يكون على الشبكة المحلية')
    try:
        data = base64.b64decode(d.get('data', ''))
    except Exception:
        raise Exception('بيانات الطباعة غير صالحة')
    try:
        with socket.create_connection((ip, port), timeout=5) as s:
            s.settimeout(10)
            s.sendall(data)
    except Exception as e:
        raise Exception('تعذر الاتصال بالطابعة %s:%d (%s)' % (ip, port, e))


def do_import(d):
    save_state(d, 'admin')
    with LOCK, db() as c:
        c.execute('DELETE FROM Sales')
        c.execute('DELETE FROM SaleItems')
        for closed, lst in ((1, d.get('archive', [])), (0, d.get('sales', []))):
            for k, s in enumerate(lst):
                s['id'] = s.get('id') or '%s-%s-%s-%d' % (s['day'], s['no'], closed, k)
                ins_sale(c, s, closed)
        c.execute('DELETE FROM day_locks')
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'import' FROM Sales WHERE Closed=1", (now_s(),))


def reset():
    with LOCK, db() as c:
        for t in ('Sales', 'SaleItems', 'Stock', 'SaleLog', 'day_locks', 'Returns', 'ReturnItems', 'Stocktake', 'StocktakeMeta', 'StoreAdj', 'StockCount'):
            c.execute('DELETE FROM ' + t)


LOGIN_MAX_FAILS = 5
LOGIN_LOCK_MIN = 10


class LoginLocked(Exception):
    def __init__(self, secs):
        self.secs = secs
        Exception.__init__(self, 'الحساب مقفول مؤقتاً بعد %d محاولات غلط - جرّب بعد %d دقيقة' % (LOGIN_MAX_FAILS, max(1, -(-secs // 60))))


def _login_lock_secs(c, key, now):
    """لو آخر 5 محاولات غلط في آخر 10 دقايق (من غير نجاح بينهم) => ثواني القفل المتبقية، وإلا 0"""
    win = now - LOGIN_LOCK_MIN * 60000
    last_ok = c.execute('SELECT IFNULL(MAX(AtMs),0) FROM LoginLog WHERE UKey=? AND OK=1', (key,)).fetchone()[0]
    rows = c.execute("SELECT AtMs FROM LoginLog WHERE UKey=? AND OK=0 AND IFNULL(Note,'')!='locked' AND AtMs>? AND AtMs>? ORDER BY AtMs DESC LIMIT ?",
                     (key, win, last_ok, LOGIN_MAX_FAILS)).fetchall()
    if len(rows) >= LOGIN_MAX_FAILS:
        left = (rows[0][0] + LOGIN_LOCK_MIN * 60000 - now) // 1000
        return max(0, left)
    return 0


def login(u, p, ip=''):
    u = (u or '').strip()
    key = u.lower()[:60]
    now = int(time.time() * 1000)
    with LOCK, db() as c:
        left = _login_lock_secs(c, key, now) if key else 0
        if left > 0:
            c.execute('INSERT INTO LoginLog (UKey,UName,At,AtMs,OK,IP,Note) VALUES (?,?,?,?,0,?,?)', (key, u[:60], now_s(), now, ip, 'locked'))
            raise LoginLocked(left)
        # الدخول بكود الموظف (EmpNo) أو باسم الدخول القديم (حسابات v12 زي admin)
        r = c.execute('SELECT UName,JobNo,Name,EmpNo,PHash FROM Employees WHERE lower(UName)=lower(?) AND Active=1', (u,)).fetchone()
        if not r and u.isdigit():
            r = c.execute('SELECT UName,JobNo,Name,EmpNo,PHash FROM Employees WHERE EmpNo=? AND Active=1', (int(u),)).fetchone()
        role = JOB_ROLE.get(r[1]) if r else None
        ok = bool(r) and vp(r[0], p or '', r[4]) and role not in (None, 'delivery')  # المناديب ملهمش دخول
        c.execute('INSERT INTO LoginLog (UKey,UName,EmpNo,At,AtMs,OK,IP,Note) VALUES (?,?,?,?,?,?,?,?)',
                  (key, (r[0] if r else u)[:60], r[3] if r else None, now_s(), now, 1 if ok else 0, ip,
                   '' if ok else ('غير موجود' if not r else 'باسورد غلط')))
        if not ok:
            return None
        if hp_is_legacy(r[4]):  # ترقية تلقائية للهاش القديم إلى PBKDF2 عند أول دخول ناجح
            c.execute('UPDATE Employees SET PHash=? WHERE EmpNo=?', (hp(r[0], p or ''), r[3]))
    return {'u': r[0], 'role': role, 'full': r[2], 'emp': r[3], 'job': r[1]}


def login_log(limit=200):
    with db() as c:
        return [dict(at=a, u=u or k, emp=e, ok=bool(o), ip=ip or '', note=n or '')
                for a, u, k, e, o, ip, n in c.execute('SELECT At,UName,UKey,EmpNo,OK,IP,Note FROM LoginLog ORDER BY LID DESC LIMIT ?', (int(limit),)).fetchall()]


def list_jobs():
    with db() as c:
        return [dict(job=j, name=n) for j, n in c.execute('SELECT JobNo,JobName FROM Jobs WHERE Active=1 ORDER BY JobNo').fetchall()]


def list_users():
    with db() as c:
        out = []
        jobs = {j: n for j, n in c.execute('SELECT JobNo,JobName FROM Jobs').fetchall()}
        for un, jn, nm, ph, en in c.execute('SELECT UName,JobNo,Name,Phone,EmpNo FROM Employees WHERE UName IS NOT NULL AND Active=1 ORDER BY JobNo,EmpNo').fetchall():
            role = JOB_ROLE.get(jn, 'cashier')
            if role == 'delivery' and not ph:
                d = c.execute('SELECT Phone FROM Deliveries WHERE DName=? LIMIT 1', (nm or un,)).fetchone()
                ph = d[0] if d else ''
            out.append(dict(u=un, role=role, full=nm or '', code=en, job=jn, jobName=jobs.get(jn, ''), phone=ph or ''))
        return out


NAME_RE = re.compile(r'[A-Za-z\u0600-\u06FF\u0750-\u077F ]+')


def gen_pw():
    return ''.join(secrets.choice('0123456789') for _ in range(6))


def save_user(d):
    """إضافة/تعديل/حذف موظف. المعرّف = EmpNo (كود الموظف، تلقائي ومايتغيّرش). يرجّع {'emp','pw'} (pw بس لو اتولّد/اتغيّر)."""
    emp = d.get('emp')
    emp = int(emp) if str(emp or '').isdigit() else None

    def admins_left(c):
        return c.execute('SELECT COUNT(*) FROM Employees WHERE JobNo IN (1,2) AND Active=1 AND UName IS NOT NULL').fetchone()[0]

    with LOCK, db() as c:
        ex = None
        if emp:
            ex = c.execute('SELECT EmpNo,JobNo,Name,UName,Phone FROM Employees WHERE EmpNo=?', (emp,)).fetchone()
        elif d.get('u'):
            ex = c.execute('SELECT EmpNo,JobNo,Name,UName,Phone FROM Employees WHERE UName=?', (str(d['u']).strip(),)).fetchone()
        if (emp or d.get('u')) and not ex:
            raise Exception('الموظف غير موجود')

        # ---- حذف
        if d.get('del'):
            if ex[1] in (1, 2) and admins_left(c) <= 1:
                raise Exception('لا يمكن حذف آخر مدير')
            if ex[1] == 5:
                rid = c.execute('SELECT DID FROM Deliveries WHERE DName=? LIMIT 1', (ex[2] or ex[3],)).fetchone()
                if rid:
                    if c.execute('SELECT 1 FROM DeliveryShifts WHERE DID=? LIMIT 1', (rid[0],)).fetchone():
                        c.execute('UPDATE Deliveries SET Active=0 WHERE DID=?', (rid[0],))
                    else:
                        c.execute('DELETE FROM Deliveries WHERE DID=?', (rid[0],))
            used = (c.execute('SELECT 1 FROM Sales WHERE EmpNo=? LIMIT 1', (ex[0],)).fetchone()
                    or c.execute('SELECT 1 FROM SaleLog WHERE ByEmpNo=? LIMIT 1', (ex[0],)).fetchone())
            if used:
                c.execute('UPDATE Employees SET Active=0 WHERE EmpNo=?', (ex[0],))
            else:
                c.execute('DELETE FROM Employees WHERE EmpNo=?', (ex[0],))
            return {'emp': ex[0]}

        # ---- القيم (في التعديل: اللي مش مبعوت يفضل زي ما هو)
        name = (d['name'] if 'name' in d else (ex[2] if ex else '')) or ''
        name = re.sub(r'\s+', ' ', str(name)).strip()
        if not name:
            raise Exception('اكتب اسم الموظف')
        if not NAME_RE.fullmatch(name):
            raise Exception('الاسم لازم حروف عربي أو إنجليزي فقط')
        jn = int(d['job']) if str(d.get('job') or '').isdigit() else (ex[1] if ex else None)
        if jn is None:
            raise Exception('اختار الوظيفة')
        if not c.execute('SELECT 1 FROM Jobs WHERE JobNo=? AND Active=1', (jn,)).fetchone() and not (ex and ex[1] == jn):
            raise Exception('الوظيفة غير موجودة')
        phone = str(d['phone']).strip() if 'phone' in d else (ex[4] if ex else '')
        phone = phone or ''
        if phone and not re.fullmatch(r'[0-9+ \-]{5,20}', phone):
            raise Exception('رقم التليفون غير صحيح')
        role = JOB_ROLE.get(jn, 'cashier')
        if role == 'delivery' and not phone:
            raise Exception('اكتب تليفون المندوب')

        pw_out = None
        if ex:
            if ex[1] in (1, 2) and jn not in (1, 2) and admins_left(c) <= 1:
                raise Exception('لا يمكن تغيير وظيفة آخر مدير')
            c.execute('UPDATE Employees SET JobNo=?, Name=?, Phone=?, Active=1 WHERE EmpNo=?', (jn, name, phone, ex[0]))
            if d.get('p') or d.get('regen'):
                pw_out = d.get('p') or gen_pw()
                c.execute('UPDATE Employees SET PHash=?, Password=? WHERE EmpNo=?', (hp(ex[3], pw_out), '', ex[0]))
            eno, old_name = ex[0], ex[2]
        else:
            pw_out = d.get('p') or gen_pw()
            cur = c.execute('INSERT INTO Employees (Name,JobNo,Phone,Password,Active,CreatedAt,UName,PHash) VALUES (?,?,?,?,1,?,?,NULL)',
                            (name, jn, phone, '', now_s(), 'tmp' + secrets.token_hex(6)))
            eno, old_name = cur.lastrowid, None
            un = str(eno)
            if c.execute('SELECT 1 FROM Employees WHERE UName=?', (un,)).fetchone():
                un = 'E' + un
            c.execute('UPDATE Employees SET UName=?, PHash=? WHERE EmpNo=?', (un, hp(un, pw_out), eno))

        # ---- مزامنة المندوب مع Deliveries (شاشة الدليفري)
        if role == 'delivery':
            row = c.execute('SELECT DID FROM Deliveries WHERE DName=? LIMIT 1', (old_name or name,)).fetchone()
            if not row:
                row = c.execute("SELECT DID FROM Deliveries WHERE Phone=? AND Phone!='' LIMIT 1", (phone,)).fetchone()
            if row:
                c.execute('UPDATE Deliveries SET DName=?, Phone=?, Active=1 WHERE DID=?', (name, phone, row[0]))
            else:
                c.execute('INSERT INTO Deliveries (DName, Phone, Active) VALUES (?,?,1)', (name, phone))
        return {'emp': eno, 'pw': pw_out}


# ------------------------------------------------------------------ مرتجع (v4) - 4 أقفال
# 1) بيختار فاتورة حقيقية من السيستم والأصناف بتيجي منها  2) كل صنف يرجع مرة واحدة بس (بالكمية)
# 3) باسورد مدير + سبب من قايمة مقفولة  4) فلوس المرتجع "مديونية مرتجع" مش من الدرج: المدير يصرفها من خزنة منفصلة
RET_REASONS_DEF = ['أوردر غلط', 'جودة سيئة', 'الزبون لغى', 'تأخير في التوصيل', 'صنف ناقص', 'أخرى']
FAILS = {}


def ret_reasons(cfg):
    r = [x.strip() for x in re.split(r'[,،]', str(cfg.get('retReasons') or '')) if x.strip()]
    return r or RET_REASONS_DEF


def find_manager(c, pw):
    for u, full, h in c.execute("SELECT UName,Name,PHash FROM Employees WHERE JobNo IN (1,2) AND Active=1 AND UName IS NOT NULL AND PHash IS NOT NULL").fetchall():
        if vp(u, pw, h):
            return u, (full or u)
    return None


def full_name(c, u):
    r = c.execute('SELECT Name FROM Employees WHERE UName=?', (u,)).fetchone()
    return r[0] if r and r[0] else u


def ret_dict(c, r):
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5], refund=i[6])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price,Refund FROM ReturnItems WHERE RID=?', (r[0],))]
    return dict(id=r[0], no=r[1], sid=r[2], sno=r[3], day=r[4], time=r[5], reason=r[6], by=r[7], mgr=r[8],
                total=r[9], status=r[10], paidBy=r[11] or '', paidAt=r[12] or '', log=r[13] or '', items=items)


RET_COLS = 'RID,RNo,SID,SNo,RDay,RTime,Reason,Cashier,Manager,Total,Status,PaidBy,PaidAt,Log'


def list_returns(c):
    return [ret_dict(c, r) for r in c.execute('SELECT %s FROM Returns ORDER BY RNo' % RET_COLS).fetchall()]


# ★★★ [EDIT-MAP] المرتجع بالأقفال الأربعة ★★★
def do_return(d, user):
    now = datetime.now()
    key = user['u']
    fl = [t for t in FAILS.get(key, []) if (now - t).total_seconds() < 600]
    if len(fl) >= 5:
        raise Forbidden('تم إيقاف المرتجع مؤقتاً بسبب باسورد مدير غلط كذا مرة - استنى 10 دقايق')
    with LOCK, db() as c:
        cfg = get_cfg(c)
        reason = str(d.get('reason', '')).strip()
        if reason not in ret_reasons(cfg):
            raise Exception('اختار سبب المرتجع من القايمة')
        mgr = find_manager(c, str(d.get('pw', '')))
        if not mgr:
            fl.append(now)
            FAILS[key] = fl
            c.commit()
            raise Forbidden('باسورد المدير غير صحيح')
        FAILS.pop(key, None)
        s, closed = snapshot(c, d['sid'])
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعمله مرتجع')
        orig, wsum = {}, {}
        for i in s['items']:
            orig[i['id']] = orig.get(i['id'], 0) + i['qty']
            wsum[i['id']] = wsum.get(i['id'], 0) + i['qty'] * i['price']
        meta = {i['id']: i for i in s['items']}
        done = {r[0]: r[1] for r in c.execute(
            'SELECT ri.PID,SUM(ri.Qty) FROM ReturnItems ri JOIN Returns r ON r.RID=ri.RID WHERE r.SID=? GROUP BY ri.PID', (s['id'],))}
        factor = (s['total'] - s['fee']) / s['sub'] if s['sub'] > 0 else 1.0
        lines, total = [], 0.0
        for it in d.get('items') or []:
            pid, q = int(it['id']), round(float(it['qty']), 3)
            if q <= 0:
                continue
            if pid not in orig:
                raise Forbidden('الصنف ده مش موجود في الفاتورة الأصلية')
            if done.get(pid, 0) + q > orig[pid] + 1e-9:
                raise Forbidden('الكمية دي رجعت قبل كده')
            price = wsum[pid] / orig[pid]
            refund = round(q * price * factor, 2)
            m = meta[pid]
            lines.append((pid, m['name'], m.get('cat') or '', m['unit'], q, round(price, 4), refund))
            total += refund
            done[pid] = done.get(pid, 0) + q
        if not lines:
            raise Exception('اختار كمية للمرتجع')
        rno = (c.execute('SELECT MAX(RNo) FROM Returns').fetchone()[0] or 0) + 1
        rid = secrets.token_hex(8)
        cname = full_name(c, user['u'])
        text = 'مرتجع فاتورة %s بواسطة كاشير %s وافق عليه مدير %s الساعة %s' % (s['no'], cname, mgr[1], now.strftime('%H:%M'))
        c.execute('INSERT INTO Returns (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)' % RET_COLS,
                  (rid, rno, s['id'], s['no'], now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), reason, cname, mgr[1],
                   round(total, 2), 'due', '', '', text))
        c.executemany('INSERT INTO ReturnItems VALUES (?,?,?,?,?,?,?,?)', [(rid,) + l for l in lines])
        full = all(done.get(pid, 0) >= orig[pid] - 1e-9 for pid in orig)
        c.execute('UPDATE Sales SET Returned=? WHERE SID=?', (2 if full else 1, s['id']))
        log(c, s['id'], 'return', user, text, {'rid': rid, 'total': round(total, 2), 'reason': reason})
        r = c.execute('SELECT %s FROM Returns WHERE RID=?' % RET_COLS, (rid,)).fetchone()
        return ret_dict(c, r)


def do_return_pay(d, user):
    with LOCK, db() as c:
        c.execute("UPDATE Returns SET Status='paid',PaidBy=?,PaidAt=? WHERE RID=? AND Status='due'",
                  (full_name(c, user['u']), now_s(), d['rid']))


# ------------------------------------------------------------------ طلبيات الشراء + الموردين + الأكواد المخزنية (v4)
def po_dict(c, r):
    items = [dict(code=i[0], name=i[1], unit=i[2], qty=i[3], price=i[4], rqty=i[5])
             for i in c.execute('SELECT Code,IName,Unit,Qty,Price,RQty FROM POItems WHERE POID=?', (r[0],))]
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], supplier=r[4], total=r[5], status=r[6], by=r[7],
                sentAt=r[8] or '', note=r[9] or '', err=r[10] or '', stage=r[11] or 'progress',
                recvAt=r[12] or '', recvBy=r[13] or '', items=items)


PO_BASE = 'POID,PONo,PODay,POTime,Supplier,Total,Status,CreatedBy,SentAt,Note,Err'
PO_COLS = PO_BASE + ',Stage,RecvAt,RecvBy'


FISH_CAT = 'أسماك'


# أسعار تقديرية (جنيه/كيلو) من أسواق مصر أكتوبر 2026: سوق العبور (الأسماك) وبوابة الأسعار (السلع) - عدّلها من 🏷️ الموردين والأكواد
# ★★★ [EDIT-MAP] ⭐ أسعار الأسماك التقديرية (أكتوبر 2026) - عدّلها هنا أو من شاشة الأكواد ★★★
FISH_RULES = [('كفتة', 0), ('فيليه', 150), ('قزاز', 300), ('جمبري', 580), ('بلطي', 80), ('مكريل', 120), ('ماكريل', 120), ('مكرونة', 150), ('وقار', 220),
              ('دينيس', 300), ('قشر بياض', 250), ('بربوني', 200), ('بريوني', 200), ('موسى', 330), ('قاروص', 250), ('لوت', 250), ('شعور', 200),
              ('ثعابين', 250), ('كابوريا', 180), ('سبيط', 300), ('كاليماري', 300)]
# ★★★ [EDIT-MAP] ⭐ الخامات الأساسية وأسعارها (ملح، زيت، أرز...) ★★★
STAPLES = [('5001', 'أرز', 'kg', 'بقالة', 35), ('5002', 'دقيق', 'kg', 'بقالة', 27), ('5003', 'زيت عباد الشمس', 'ltr', 'بقالة', 105),
           ('5004', 'زيت ذرة', 'ltr', 'بقالة', 120), ('5005', 'سكر', 'kg', 'بقالة', 35), ('5006', 'مكرونة', 'kg', 'بقالة', 26),
           ('5007', 'طماطم', 'kg', 'خضار', 20), ('5008', 'بصل', 'kg', 'خضار', 17), ('5009', 'ثوم', 'kg', 'خضار', 56),
           ('5010', 'ليمون', 'kg', 'خضار', 27), ('5011', 'بطاطس', 'kg', 'خضار', 20), ('5012', 'بيض', 'pc', 'بقالة', 4.2),
           ('5013', 'فراخ', 'kg', 'لحوم', 95), ('5014', 'كمون', 'kg', 'بهارات', 300), ('5015', 'فلفل أسود', 'kg', 'بهارات', 420),
           ('5016', 'شطة', 'kg', 'بهارات', 215), ('5017', 'كزبرة ناشفة', 'kg', 'بهارات', 140), ('5018', 'بابريكا', 'kg', 'بهارات', 170),
           ('5019', 'ملح', 'kg', 'بقالة', 0), ('5020', 'طحينة', 'kg', 'بقالة', 0)]


def fish_price(name):
    for k, v in FISH_RULES:
        if k in name:
            return v
    return 0


def seed_prices(c):
    c.execute('CREATE TABLE IF NOT EXISTS Flags (K TEXT PRIMARY KEY)')
    if not c.execute("SELECT 1 FROM Flags WHERE K='staples_v1'").fetchone():
        for code, name, unit, cat, price in STAPLES:
            if not c.execute('SELECT 1 FROM StoreItems WHERE Code=? OR IName=?', (code, name)).fetchone():
                c.execute("INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID,Src) VALUES (?,?,?,?,?,0,'')", (code, name, unit, cat, price))
        c.execute("INSERT INTO Flags VALUES ('staples_v1')")
    if not c.execute("SELECT 1 FROM Flags WHERE K='fish_prices_v1'").fetchone():
        for code, name in c.execute("SELECT Code,IName FROM StoreItems WHERE Src='menu' AND IFNULL(Price,0)=0").fetchall():
            if fish_price(name):
                c.execute('UPDATE StoreItems SET Price=? WHERE Code=?', (fish_price(name), code))
        c.execute("INSERT INTO Flags VALUES ('fish_prices_v1')")


# ★★★ [EDIT-MAP] مزامنة أصناف جروب الأسماك مع الأكواد المخزنية ★★★
def sync_fish(c):
    """v7: أصناف جروب الأسماك في المنيو بتظهر تلقائياً في أكواد الطلبيات بنفس كود الصنف (الاسم والوحدة بيتحدّثوا، والسعر/المورد بتحدده انت)"""
    cur = set()
    for code, name, unit in c.execute('SELECT Code,PName,Unit FROM Products WHERE Cat=? AND Code IS NOT NULL AND Code<>\'\'', (FISH_CAT,)).fetchall():
        code = str(code).strip()
        cur.add(code)
        if c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (code,)).fetchone():
            c.execute("UPDATE StoreItems SET IName=?,Unit=?,Cat=? WHERE Code=? AND Src='menu'", (name, unit or 'kg', FISH_CAT, code))
        else:
            c.execute('INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID,Src) VALUES (?,?,?,?,?,0,\'menu\')', (code, name, unit or 'kg', FISH_CAT, fish_price(name)))
    # صنف اتمسح أو اتغيّر كوده من المنيو: يتشال من الأكواد (الطلبيات القديمة محتفظة باسمها)
    if cur:  # لو جروب الأسماك اتسمّى تاني/اتمسح: بنوقف المزامنة ومنمسحش حاجة
        for (code,) in c.execute('SELECT Code FROM StoreItems WHERE Src=\'menu\'').fetchall():
            if code in cur:
                continue
            if c.execute('SELECT 1 FROM POItems WHERE Code=? UNION SELECT 1 FROM Recipe WHERE Code=? LIMIT 1', (code, code)).fetchone():
                c.execute("UPDATE StoreItems SET Src='' WHERE Code=?", (code,))  # عليه طلبيات/ريسبي: يفضل كخامة عادية
            else:
                c.execute('DELETE FROM StoreItems WHERE Code=?', (code,))


# ★★★ [EDIT-MAP] بيانات المخازن: الأكواد والموردين والطلبيات ★★★
def get_store():
    with db() as c:
        sync_fish(c)
        seed_prices(c)
        return dict(
            suppliers=[dict(id=r[0], name=r[1], phone=r[2] or '', note=r[3] or '')
                       for r in c.execute('SELECT SupID,SName,Phone,Note FROM Suppliers ORDER BY SName')],
            items=[dict(code=r[0], name=r[1], unit=r[2], cat=r[3] or '', price=r[4] or 0, supid=r[5] or 0)
                   for r in c.execute('SELECT Code,IName,Unit,Cat,Price,SupID FROM StoreItems ORDER BY Code')],
            templates=[dict(id=r[0], name=r[1], items=json.loads(r[2] or '[]'))
                       for r in c.execute('SELECT TID,TName,Items FROM PoTemplates ORDER BY TName')],
            orders=[po_dict(c, r) for r in c.execute('SELECT %s FROM POrders ORDER BY PONo DESC LIMIT 300' % PO_COLS)],
            stock={})


def store_extra(st):
    """رصيد كل خامة دلوقتي + آخر سعر شراء (من آخر طلبية)"""
    with db() as c:
        rc, cs, ad = mat_move(c, '0000-00-00', '9999-99-99')
        pr = last_prices(c)
    for i in st['items']:
        k = str(i['code'])
        i['bal'] = round(rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0), 3)
        i['last'] = pr.get(k, 0) or 0
    return st


# ★★★ [EDIT-MAP] إنشاء الطلبية ★★★
def do_po(d, user):
    sup = str(d.get('supplier', '')).strip()
    if not sup:
        raise Exception('اكتب اسم المورد')
    with LOCK, db() as c:
        sync_fish(c)
        ex = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        if ex:
            return po_dict(c, ex)
        lines, total = [], 0.0
        for it in d.get('items') or []:
            q = round(float(it['qty']), 3)
            row = c.execute('SELECT Code,IName,Unit,Price FROM StoreItems WHERE Code=?', (str(it['code']).strip(),)).fetchone()
            if not row:
                raise Exception('الكود %s غير موجود' % it['code'])
            if q <= 0:
                raise Exception('كمية غير صحيحة للكود %s' % it['code'])
            lines.append((d['id'], row[0], row[1], row[2], q, row[3] or 0))
            total += q * (row[3] or 0)
        if not lines:
            raise Exception('الطلبية فاضية')
        if not c.execute('SELECT 1 FROM Suppliers WHERE SName=?', (sup,)).fetchone():
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (sup, '', ''))
        n = c.execute("SELECT MAX(CAST(SUBSTR(PONo,4) AS INTEGER)) FROM POrders").fetchone()[0] or 0
        now = datetime.now()
        c.execute('INSERT INTO POrders (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?)' % PO_BASE,
                  (d['id'], 'PO-%04d' % (n + 1), now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), sup, round(total, 2),
                   'saved', user['u'], '', str(d.get('note', '')).strip(), ''))
        c.executemany('INSERT INTO POItems (POID,Code,IName,Unit,Qty,Price) VALUES (?,?,?,?,?,?)', lines)
        return po_dict(c, c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone())


# ------------------------------------------------------------------ v10: ريسبي + حركة الخامات + جرد الشهر
def last_prices(c):
    """سعر شراء كل خامة = سعرها في آخر طلبية (مش ملغاة)، ولو مفيش بياخد سعر الكود"""
    pr = {str(r[0]): (r[1] or 0) for r in c.execute('SELECT Code,Price FROM StoreItems')}
    for code, price in c.execute("SELECT i.Code,i.Price FROM POItems i JOIN POrders o ON o.POID=i.POID "
                                 "WHERE IFNULL(o.Stage,'progress')!='cancelled' AND IFNULL(i.Price,0)>0 ORDER BY o.PONo"):
        pr[str(code)] = price
    return pr


def usage_map(c):
    """pid -> [(كود الخامة, الكمية لكل وحدة بيع)]: من الريسبي. صنف الأسماك من غير ريسبي وكوده = كود خامة بياخد 1:1"""
    um = {}
    for pid, code, q in c.execute('SELECT PID,Code,Qty FROM Recipe'):
        um.setdefault(pid, []).append((str(code), q or 0))
    codes = {str(r[0]) for r in c.execute('SELECT Code FROM StoreItems')}
    for pid, cat, code in c.execute('SELECT PID,Cat,Code FROM Products'):
        if pid not in um and cat == FISH_CAT and code and str(code) in codes:
            um[pid] = [(str(code), 1.0)]
    return um


# ★★★ [EDIT-MAP] حساب تكلفة الصنف من الريسبي ★★★
def unit_cost(c, pid, um, pr):
    if pid in um:
        return round(sum(q * pr.get(code, 0) for code, q in um[pid]), 4)
    r = c.execute('SELECT Cost FROM Products WHERE PID=?', (pid,)).fetchone()
    return (r[0] or 0) if r else 0


# ★★★ [EDIT-MAP] حركة الخامات ★★★
def mat_move(c, lo, hi, closed_only=False, kinds=None):
    """حركة الخامات في الفترة [lo,hi): وارد (طلبيات تم الاستلام) / مستهلك (مبيعات × الريسبي) / تسويات الجرد"""
    recv, cons, adj = {}, {}, {}
    for code, q in c.execute("SELECT i.Code,SUM(IFNULL(i.RQty,0)) FROM POItems i JOIN POrders o ON o.POID=i.POID "
                             "WHERE o.Stage='received' AND substr(o.RecvAt,1,10)>=? AND substr(o.RecvAt,1,10)<? GROUP BY i.Code", (lo, hi)):
        recv[str(code)] = q or 0
    um = usage_map(c)
    sql = ('SELECT i.PID,SUM(i.Qty) FROM SaleItems i JOIN Sales s ON s.SID=i.SID WHERE IFNULL(s.Void,0)=0 AND s.SDay>=? AND s.SDay<?'
           + (' AND IFNULL(s.Closed,0)=1' if closed_only else '') + ' GROUP BY i.PID')
    for pid, q in c.execute(sql, (lo, hi)):
        for code, per in um.get(pid, []):
            cons[code] = cons.get(code, 0) + (q or 0) * per
    sql = 'SELECT Code,SUM(Qty) FROM StoreAdj WHERE Day>=? AND Day<?' + (' AND Kind IN (%s)' % ','.join('?' * len(kinds)) if kinds else '') + ' GROUP BY Code'
    for code, q in c.execute(sql, (lo, hi) + tuple(kinds or ())):
        adj[str(code)] = q or 0
    return recv, cons, adj


def _month(m):
    m = str(m or '')[:7]
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', m):
        raise Exception('شهر غير صحيح')
    return m


def _prev_month(m):
    y, mo = int(m[:4]), int(m[5:7])
    return '%04d-%02d' % ((y - 1, 12) if mo == 1 else (y, mo - 1))


def _st_calc(c, m):
    lo, hi = m + '-01', m + '-32'
    pr, pc, pa = mat_move(c, '0000-00-00', lo)
    rc, cs, _ = mat_move(c, lo, hi)
    open_adj = {str(r[0]): r[1] for r in c.execute("SELECT Code,Qty FROM StoreAdj WHERE Month=? AND Kind='open'", (m,))}
    prev = c.execute("SELECT 1 FROM StocktakeMeta WHERE Month=? AND Status='closed'", (_prev_month(m),)).fetchone()
    out = {}
    for (code,) in c.execute('SELECT Code FROM StoreItems').fetchall():
        k = str(code)
        prior = pr.get(k, 0) - pc.get(k, 0) + pa.get(k, 0)
        opening = prior + (0 if prev else open_adj.get(k, 0))
        out[k] = dict(prior=prior, opening=round(opening, 3), received=round(rc.get(k, 0), 3), used=round(cs.get(k, 0), 3),
                      expected=round(opening + rc.get(k, 0) - cs.get(k, 0), 3))
    return out, bool(prev)


# ★★★ [EDIT-MAP] جرد الشهر ★★★
def stocktake_get(month):
    m = _month(month)
    with db() as c:
        meta = c.execute('SELECT Status,ClosedBy,ClosedAt FROM StocktakeMeta WHERE Month=?', (m,)).fetchone()
        calc, fixed = _st_calc(c, m)
        pr = last_prices(c)
        mine = {str(r[0]): r[1] for r in c.execute('SELECT Code,Actual FROM StockCount WHERE Month=?', (m,))}
        rows = []
        for code, name, unit, cat in c.execute('SELECT Code,IName,Unit,Cat FROM StoreItems ORDER BY Cat,Code').fetchall():
            k = str(code)
            x = calc[k]
            rows.append(dict(code=k, name=name, unit=unit or 'kg', cat=cat or '', opening=x['opening'], fixed=fixed,
                             received=x['received'], used=x['used'], expected=x['expected'], actual=mine.get(k),
                             price=pr.get(k, 0), value=round(x['expected'] * pr.get(k, 0), 2)))
        return dict(month=m, status=(meta[0] if meta else 'open'), closedBy=(meta[1] if meta else '') or '',
                    closedAt=(meta[2] if meta else '') or '', rows=rows)


def stocktake_save(d):
    m = _month(d.get('month'))
    with LOCK, db() as c:
        st = c.execute('SELECT Status FROM StocktakeMeta WHERE Month=?', (m,)).fetchone()
        if st and st[0] == 'closed':
            raise Exception('جرد الشهر ده اتعتمد - مينفعش يتعدّل')
        calc, fixed = _st_calc(c, m)
        for r in d.get('rows') or []:
            k = str(r['code'])
            if k not in calc:
                continue
            old = c.execute('SELECT Opening,Actual FROM StockCount WHERE Month=? AND Code=?', (m, k)).fetchone()
            op, ac = (old[0], old[1]) if old else (None, None)
            if 'opening' in r and not fixed:
                if r['opening'] in (None, ''):
                    op = None
                    c.execute("DELETE FROM StoreAdj WHERE Month=? AND Code=? AND Kind='open'", (m, k))
                else:
                    op = round(float(r['opening']), 3)
                    c.execute("INSERT OR REPLACE INTO StoreAdj (Month,Code,Kind,Day,Qty) VALUES (?,?,'open',?,?)",
                              (m, k, m + '-01', round(op - calc[k]['prior'], 3)))
            if 'actual' in r:
                ac = None if r['actual'] in (None, '') else round(float(r['actual']), 3)
                if ac is not None and ac < 0:
                    raise Exception('كمية غير صحيحة')
            c.execute('INSERT OR REPLACE INTO StockCount (Month,Code,Opening,Actual) VALUES (?,?,?,?)', (m, k, op, ac))


def stocktake_close(d, user):
    """اعتماد الجرد: الفرق بين العدد الفعلي والمتوقع بيتسجل تسوية، فرصيد المخزن بيبقى = العدد الفعلي ورصيد أول الشهر الجاي كمان"""
    m = _month(d.get('month'))
    with LOCK, db() as c:
        if c.execute("SELECT 1 FROM StocktakeMeta WHERE Month=? AND Status='closed'", (m,)).fetchone():
            return
        calc, _ = _st_calc(c, m)
        for k, ac in c.execute('SELECT Code,Actual FROM StockCount WHERE Month=? AND Actual IS NOT NULL', (m,)).fetchall():
            if str(k) in calc:
                c.execute("INSERT OR REPLACE INTO StoreAdj (Month,Code,Kind,Day,Qty) VALUES (?,?,'count',?,?)",
                          (m, str(k), m + '-31', round(ac - calc[str(k)]['expected'], 3)))
        c.execute('INSERT OR REPLACE INTO StocktakeMeta (Month,Status,ClosedBy,ClosedAt) VALUES (?,?,?,?)',
                  (m, 'closed', full_name(c, user['u']), now_s()))


def stocktake_reopen(d):
    m = _month(d.get('month'))
    with LOCK, db() as c:
        if c.execute("SELECT 1 FROM StocktakeMeta WHERE Month>? AND Status='closed'", (m,)).fetchone():
            raise Exception('فيه جرد شهر بعده معتمد - افتح الأحدث الأول')
        c.execute("DELETE FROM StoreAdj WHERE Month=? AND Kind='count'", (m,))
        c.execute('DELETE FROM StocktakeMeta WHERE Month=?', (m,))


# ★★★ [EDIT-MAP] حفظ الريسبي ★★★
def recipe_save(d):
    pid = int(d['pid'])
    with LOCK, db() as c:
        codes = {str(r[0]) for r in c.execute('SELECT Code FROM StoreItems')}
        rows = {}
        for r in d.get('rows') or []:
            k, q = str(r['code']).strip(), round(float(r['qty']), 4)
            if k not in codes:
                raise Exception('الكود %s مش موجود في الموردين والأكواد' % k)
            if q <= 0:
                raise Exception('كمية غير صحيحة للكود %s' % k)
            rows[k] = round(rows.get(k, 0) + q, 4)
        c.execute('DELETE FROM Recipe WHERE PID=?', (pid,))
        c.executemany('INSERT INTO Recipe (PID,Code,Qty) VALUES (?,?,?)', [(pid, k, q) for k, q in rows.items()])
        if 'cost' in d:
            c.execute('UPDATE Products SET Cost=? WHERE PID=?', (float(d.get('cost') or 0), pid))


# ★★★ [EDIT-MAP] تقارير المخزن والأرباح ★★★
def report_stock(frm, to):
    """تقرير المخزن + تكلفة وربح الأصناف في فترة"""
    lo, hi = (frm or '0000-00-00'), ((to or '9999-99-99') + ' ')
    with db() as c:
        sync_fish(c)
        pr = last_prices(c)
        b_rc, b_cs, b_ad = mat_move(c, '0000-00-00', lo)
        rc, cs, ad = mat_move(c, lo, hi)
        buy = {str(r[0]): (r[1] or 0) for r in c.execute(
            "SELECT i.Code,SUM(IFNULL(i.RQty,0)*IFNULL(i.Price,0)) FROM POItems i JOIN POrders o ON o.POID=i.POID "
            "WHERE o.Stage='received' AND substr(o.RecvAt,1,10)>=? AND substr(o.RecvAt,1,10)<? GROUP BY i.Code", (lo, hi))}
        mats, sv = [], 0.0
        for code, name, unit in c.execute('SELECT Code,IName,Unit FROM StoreItems ORDER BY Cat,Code').fetchall():
            k = str(code)
            op = b_rc.get(k, 0) - b_cs.get(k, 0) + b_ad.get(k, 0)
            cl = op + rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0)
            val = cl * pr.get(k, 0)
            sv += val
            mats.append(dict(code=k, name=name, unit=unit, opening=round(op, 3), received=round(rc.get(k, 0), 3), used=round(cs.get(k, 0), 3),
                             adj=round(ad.get(k, 0), 3), closing=round(cl, 3), price=pr.get(k, 0), value=round(val, 2), bought=round(buy.get(k, 0), 2)))
        um = usage_map(c)
        prods, tr, tc = [], 0.0, 0.0
        for pid, name, cat, unit, q, rev in c.execute(
                'SELECT i.PID,i.IName,i.Cat,i.Unit,SUM(i.Qty),SUM(i.Qty*i.Price) FROM SaleItems i JOIN Sales s ON s.SID=i.SID '
                'WHERE IFNULL(s.Void,0)=0 AND s.SDay>=? AND s.SDay<? GROUP BY i.PID,i.IName ORDER BY 6 DESC', (lo, hi)).fetchall():
            uc = unit_cost(c, pid, um, pr)
            cost = (q or 0) * uc
            tr += rev or 0
            tc += cost
            prods.append(dict(id=pid, name=name, cat=cat or '', unit=unit, qty=round(q or 0, 3), revenue=round(rev or 0, 2), ucost=round(uc, 3),
                              cost=round(cost, 2), profit=round((rev or 0) - cost, 2), nocost=(uc == 0)))
        return dict(mats=mats, prods=prods, totals=dict(revenue=round(tr, 2), cost=round(tc, 2), profit=round(tr - tc, 2),
                                                        stockValue=round(sv, 2), bought=round(sum(buy.values()), 2)))


def do_po_stage(d, user):
    """تغيير حالة الطلبية: progress (جاري التنفيذ) / received (تم الاستلام: بيزوّد المخزن مرة واحدة) / cancelled (ملغاة)"""
    stage = d.get('stage')
    if stage not in ('progress', 'received', 'cancelled'):
        raise Exception('حالة غير صحيحة')
    with LOCK, db() as c:
        r = c.execute('SELECT Stage FROM POrders WHERE POID=?', (d['id'],)).fetchone()
        if not r:
            raise Exception('الطلبية غير موجودة')
        cur = r[0] or 'progress'
        if cur == stage:
            return
        if cur == 'received':
            raise Exception('الطلبية اتسجلت \"تم الاستلام\" واتزوّد المخزن - مينفعش يتغيّر')
        if stage == 'received':
            if cur == 'cancelled':
                raise Exception('الطلبية ملغاة - أعد فتحها الأول')
            got = {str(k): float(v) for k, v in (d.get('recv') or {}).items()}
            for code, name, qty in c.execute('SELECT Code,IName,Qty FROM POItems WHERE POID=?', (d['id'],)).fetchall():
                q = round(got.get(str(code), qty), 3)
                if q < 0:
                    raise Exception('كمية غير صحيحة للكود %s' % code)
                c.execute('UPDATE POItems SET RQty=? WHERE POID=? AND Code=?', (q, d['id'], code))
            c.execute('UPDATE POrders SET Stage=?,RecvAt=?,RecvBy=? WHERE POID=?', (stage, now_s(), full_name(c, user['u']), d['id']))
        else:
            c.execute('UPDATE POrders SET Stage=?,RecvAt=?,RecvBy=? WHERE POID=?', (stage, '', '', d['id']))


def po_mark(poid, status, err=''):
    with LOCK, db() as c:
        c.execute('UPDATE POrders SET Status=?,Err=?,SentAt=? WHERE POID=?',
                  (status, err[:200], now_s() if status == 'sent' else '', poid))


def do_po_send(d):
    """إرسال الطلبية بـ HTTP للعنوان اللي في الإعدادات (poIp/poPort/poPath). لو فشل: بتفضل 'في الانتظار' وتتبعت بعدين"""
    import urllib.request
    with db() as c:
        cfg = get_cfg(c)
        r = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        o = po_dict(c, r) if r else None
    if not o:
        raise Exception('الطلبية غير موجودة')
    ip = str(cfg.get('poIp') or '').strip()
    if cfg.get('poMode') != 'http' or not ip:
        raise Exception('إرسال الطلبيات HTTP غير مفعّل في الإعدادات')
    path = str(cfg.get('poPath') or '/')
    url = ip if ip.startswith('http') else 'http://%s:%d%s' % (ip, int(cfg.get('poPort') or 80), path if path.startswith('/') else '/' + path)
    body = json.dumps({'type': 'purchase_order', 'restaurant': cfg.get('name', ''), 'code': o['no'], 'date': o['day'],
                       'time': o['time'], 'supplier': o['supplier'], 'total': o['total'], 'note': o['note'],
                       'items': [dict(i, total=round(i['qty'] * i['price'], 2)) for i in o['items']]}, ensure_ascii=False).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json; charset=utf-8'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if not 200 <= resp.status < 300:
                raise Exception('HTTP %s' % resp.status)
        po_mark(d['id'], 'sent')
        return {'ok': 1, 'status': 'sent'}
    except Exception as e:
        po_mark(d['id'], 'pending', str(e))
        return {'ok': 1, 'status': 'pending', 'err': str(e)[:200]}


def save_supplier(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM Suppliers WHERE SupID=?', (int(d['id']),))
        elif d.get('id'):
            c.execute('UPDATE Suppliers SET SName=?,Phone=?,Note=? WHERE SupID=?', (d['name'], d.get('phone', ''), d.get('note', ''), int(d['id'])))
        else:
            if not str(d.get('name', '')).strip():
                raise Exception('اكتب اسم المورد')
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (d['name'].strip(), d.get('phone', ''), d.get('note', '')))


def save_store_item(d):
    code, old = str(d.get('code', '')).strip(), str(d.get('old_code') or d.get('code', '')).strip()
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM StoreItems WHERE Code=?', (old,))
            return
        if not code or not str(d.get('name', '')).strip():
            raise Exception('الكود والاسم مطلوبين')
        if old != code and c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (code,)).fetchone():
            raise Exception('الكود مستخدم لصنف تاني')
        vals = (code, d['name'].strip(), d.get('unit', 'kg'), d.get('cat', ''), float(d.get('price') or 0), int(d.get('supid') or 0))
        if c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (old,)).fetchone():
            c.execute('UPDATE StoreItems SET Code=?,IName=?,Unit=?,Cat=?,Price=?,SupID=? WHERE Code=?', vals + (old,))
        else:
            c.execute('INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID) VALUES (?,?,?,?,?,?)', vals)


def save_template(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM PoTemplates WHERE TID=?', (int(d['id']),))
            return
        name = str(d.get('name', '')).strip()
        if not name or not d.get('items'):
            raise Exception('اكتب اسم القالب وضيف أصناف')
        items = json.dumps([{'code': str(i['code']), 'qty': float(i['qty'])} for i in d['items']], ensure_ascii=False)
        if d.get('id'):
            c.execute('UPDATE PoTemplates SET TName=?,Items=? WHERE TID=?', (name, items, int(d['id'])))
        else:
            c.execute('INSERT INTO PoTemplates (TName,Items) VALUES (?,?)', (name, items))


# ------------------------------------------------------------------ [EDIT-MAP] ★ الدليفري: المناديب + الشفتات + تحميل الأوردرات (v11) ★
# الجداول: Deliveries (المناديب) | DeliveryShifts (شفت المندوب: العهدة والتحصيل والمحاسبة) | Dispatch (كل تحميل أوردر على مندوب) | DeliveryLog (سجل الأحداث)
def _ms():
    return int(time.time() * 1000)


def delivery_log(c, rid, act, user, detail=''):
    c.execute('INSERT INTO DeliveryLog (DID,Act,At,ByUser,Detail) VALUES (?,?,?,?,?)', (rid, act, now_s(), user['u'], str(detail)[:300]))


def dv_state():
    with db() as c:
        riders = [dict(id=r[0], name=r[1], phone=r[2] or '', active=bool(r[3]))
                  for r in c.execute('SELECT DID,DName,Phone,Active FROM Deliveries ORDER BY DName')]
        shifts = {str(r[1]): dict(id=r[0], rid=r[1], start=r[2], cash=r[3] or 0)
                  for r in c.execute("SELECT ShiftID,DID,StartAt,StartCash FROM DeliveryShifts WHERE Status='open'")}
        disp = [dict(id=r[0], sid=r[1], rid=r[2], shift=r[3], outAt=r[4], backAt=r[5], status=r[6])
                for r in c.execute("SELECT DispID,SaleID,DID,ShiftID,OutAt,BackAt,Status FROM Dispatch WHERE Status='out' OR "
                                   "(Status='delivered' AND ShiftID IN (SELECT ShiftID FROM DeliveryShifts WHERE Status='open'))")]
        done = [r[0] for r in c.execute("SELECT DISTINCT SaleID FROM Dispatch WHERE Status IN ('out','delivered')")]
    return dict(riders=riders, shifts=shifts, dispatch=disp, done=done, now=_ms())


def delivery_save(d):
    with LOCK, db() as c:
        if d.get('del'):
            rid = int(d['id'])
            if c.execute('SELECT 1 FROM DeliveryShifts WHERE DID=? LIMIT 1', (rid,)).fetchone():
                c.execute('UPDATE Deliveries SET Active=0 WHERE DID=?', (rid,))  # له تاريخ: بنوقفه بس
            else:
                c.execute('DELETE FROM Deliveries WHERE DID=?', (rid,))
        elif d.get('id'):
            c.execute('UPDATE Deliveries SET DName=?,Phone=?,Active=? WHERE DID=?', (str(d['name']).strip(), d.get('phone', ''), 1 if d.get('active', True) else 0, int(d['id'])))
        else:
            if not str(d.get('name', '')).strip():
                raise Exception('اكتب اسم المندوب')
            c.execute('INSERT INTO Deliveries (DName,Phone,Active) VALUES (?,?,1)', (d['name'].strip(), d.get('phone', '')))


def delivery_checkin(d, user):
    rid, cash = int(d['rid']), round(float(d.get('cash') or 0), 2)
    if cash < 0:
        raise Exception('عهدة غير صحيحة')
    with LOCK, db() as c:
        r = c.execute('SELECT Active FROM Deliveries WHERE DID=?', (rid,)).fetchone()
        if not r or not r[0]:
            raise Exception('المندوب غير موجود أو موقوف')
        if c.execute("SELECT 1 FROM DeliveryShifts WHERE DID=? AND Status='open'", (rid,)).fetchone():
            raise Exception('المندوب محضّر بالفعل (شفته مفتوح)')
        c.execute("INSERT INTO DeliveryShifts (DID,StartAt,StartCash,Status) VALUES (?,?,?,'open')", (rid, now_s(), cash))
        delivery_log(c, rid, 'checkin', user, 'عهدة %s' % cash)


def delivery_dispatch(d, user):
    rid, sids = int(d['rid']), list(d.get('sids') or [])
    if not sids:
        raise Exception('اختار أوردر واحد على الأقل')
    with LOCK, db() as c:
        sh = c.execute("SELECT ShiftID FROM DeliveryShifts WHERE DID=? AND Status='open'", (rid,)).fetchone()
        if not sh:
            raise Exception('المندوب مش محضّر - حضّره الأول')
        t = _ms()  # المندوب يقدر ياخد أكتر من أوردر مع بعض
        for sid in sids:
            s = c.execute('SELECT SType,Void,OrderType FROM Sales WHERE SID=?', (sid,)).fetchone()
            if s and (s[2] == 'A' or s[0] == 'pickup'):
                raise Exception('أوردر استلام فرع - بيتسلّم من شاشة «استلام فرع» مش على مندوب')
            if not s or s[0] != 'delivery' or s[1]:
                raise Exception('أوردر مش دليفري أو ملغي')
            if c.execute("SELECT 1 FROM Dispatch WHERE SaleID=? AND Status IN ('out','delivered')", (sid,)).fetchone():
                raise Exception('أوردر متحمّل قبل كده')
            c.execute("INSERT INTO Dispatch (SaleID,DID,ShiftID,OutAt,Status,ByUser) VALUES (?,?,?,?,'out',?)", (sid, rid, sh[0], t, user['u']))
        delivery_log(c, rid, 'dispatch', user, '%d أوردر' % len(sids))
    return {'ok': 1, 'outAt': t}


def delivery_return(d, user):
    rid = int(d['rid'])
    with LOCK, db() as c:
        n = c.execute("UPDATE Dispatch SET Status='delivered',BackAt=? WHERE DID=? AND Status='out'", (_ms(), rid)).rowcount
        if not n:
            raise Exception('المندوب مفيش معاه أوردرات بره')
        delivery_log(c, rid, 'return', user, '%d أوردر اتسلّم' % n)


def delivery_recall(d, user):
    rid = int(d['rid'])
    with LOCK, db() as c:
        n = c.execute("UPDATE Dispatch SET Status='pulled',BackAt=? WHERE DID=? AND Status='out'", (_ms(), rid)).rowcount
        if not n:
            raise Exception('المندوب مفيش معاه أوردرات بره')
        delivery_log(c, rid, 'recall', user, '%d أوردر رجعوا للمعلّقة' % n)


def dispatch_pull(d, user):
    """سحب أوردر واحد بس من المندوب (بالـ DispID) وإرجاعه للمعلّقة"""
    did = d.get('dispid', d.get('did'))
    if did is None:
        raise Exception('رقم الأوردر ناقص')
    did = int(did)
    with LOCK, db() as c:
        r = c.execute("SELECT d.DID FROM Dispatch d JOIN DeliveryShifts s ON s.ShiftID=d.ShiftID WHERE d.DispID=? AND d.Status IN ('out','delivered') AND s.Status='open'", (did,)).fetchone()
        if not r:
            raise Exception('الأوردر مش معاه مندوب (أو شفت المندوب اتقفل)')
        c.execute("UPDATE Dispatch SET Status='pulled',BackAt=? WHERE DispID=?", (_ms(), did))
        delivery_log(c, r[0], 'pull', user, 'DispID %s' % did)


def _dispatch_one(d, user, status, act, refused=0):
    """تسليم/رفض أوردر واحد من اللي مع المندوب (بالـ DispID)"""
    did = d.get('dispid', d.get('did'))
    if did is None:
        raise Exception('رقم الأوردر ناقص')
    did = int(did)
    with LOCK, db() as c:
        r = c.execute("SELECT d.DID FROM Dispatch d JOIN DeliveryShifts s ON s.ShiftID=d.ShiftID WHERE d.DispID=? AND d.Status='out' AND s.Status='open'", (did,)).fetchone()
        if not r:
            raise Exception('الأوردر مش بره مع المندوب (اتسلّم أو اتسحب قبل كده)')
        c.execute("UPDATE Dispatch SET Status=?,BackAt=?,Refused=?,DeliveredAt=? WHERE DispID=?",
                  (status, _ms(), refused, now_s() if status == 'delivered' else None, did))
        delivery_log(c, r[0], act, user, 'DispID %s' % did)


def dispatch_deliver(d, user):
    _dispatch_one(d, user, 'delivered', 'deliver1')


def dispatch_refuse(d, user):
    # الزبون رفض: الأوردر يرجع للمعلّقة (Status=pulled) وبيتعلّم Refused=1 ومش بيدخل في حساب المندوب
    _dispatch_one(d, user, 'pulled', 'refuse', 1)


def mark_delivered(d, user):
    """كاشير: تم التسليم - OrderState=F + EmpNo + DeliveredAt + PayPlace"""
    sid = d.get('sid') or d.get('id')
    if not sid:
        raise Exception('رقم الأوردر مطلوب')
    with LOCK, db() as c:
        r = c.execute("SELECT Void,OrderState,OrderType,SType FROM Sales WHERE SID=?", (sid,)).fetchone()
        if not r:
            raise Exception('الأوردر غير موجود')
        if r[0]:
            raise Exception('OrderState=F لأوردر ملغي = خطأ')
        if r[1] == 'F':
            raise Exception('الأوردر متسلّم قبل كده')
        # resolve EmpNo from Employees by username/fullname match, else null
        empno = user.get('emp')
        otype = r[2] or ('V' if r[3] == 'delivery' else 'C')
        c.execute("UPDATE Sales SET OrderState='F', EmpNo=?, PayPlace=?, OrderAction=COALESCE(NULLIF(OrderAction,''),'') WHERE SID=?",
                  (empno, otype, sid))
        # if delivery dispatch exists, mark delivered
        c.execute("UPDATE Dispatch SET Status='delivered', BackAt=?, DeliveredAt=? WHERE SaleID=? AND Status='out'",
                  (int(time.time()*1000), now_s(), sid))
        log(c, sid, 'deliver', user, 'تم التسليم', {'empNo': empno, 'orderType': otype})
    return {'ok': 1, 'sid': sid, 'empNo': empno}



def delivery_settle(d, user):
    rid, actual = int(d['rid']), round(float(d.get('actual') or 0), 2)
    with LOCK, db() as c:
        sh = c.execute("SELECT ShiftID,StartCash FROM DeliveryShifts WHERE DID=? AND Status='open'", (rid,)).fetchone()
        if not sh:
            raise Exception('المندوب ده مفيش له شفت مفتوح')
        if c.execute("SELECT 1 FROM Dispatch WHERE DID=? AND Status='out'", (rid,)).fetchone():
            raise Exception('المندوب لسه معاه أوردرات بره - اضغط رجع أو إعادة المندوب الأول')
        n, coll, fees = c.execute("SELECT COUNT(*),IFNULL(SUM(s.STotal),0),IFNULL(SUM(s.SFee),0) FROM Dispatch d JOIN Sales s ON s.SID=d.SaleID "
                                  "WHERE d.ShiftID=? AND d.Status='delivered'", (sh[0],)).fetchone()
        coll, fees = round(coll, 2), round(fees, 2)
        exp = round((sh[1] or 0) + coll, 2)
        diff = round(actual - exp, 2)
        c.execute("UPDATE DeliveryShifts SET Status='closed',EndAt=?,Collected=?,Expected=?,Actual=?,Diff=?,Orders=?,Fees=?,ClosedBy=?,Note=? WHERE ShiftID=?",
                  (now_s(), coll, exp, actual, diff, n, fees, user['u'], str(d.get('note', ''))[:200], sh[0]))
        delivery_log(c, rid, 'settle', user, 'متوقع %s فعلي %s فرق %s' % (exp, actual, diff))
        return dict(id=sh[0], rid=rid, orders=n, collected=coll, fees=fees, cash=sh[1] or 0, expected=exp, actual=actual, diff=diff)


def shifts_list(frm, to):
    with db() as c:
        return [dict(id=r[0], rid=r[1], rider=r[2], start=r[3], end=r[4] or '', cash=r[5] or 0, orders=r[6] or 0, collected=r[7] or 0, fees=r[8] or 0,
                     expected=r[9] or 0, actual=r[10] or 0, diff=r[11] or 0, by=r[12] or '', note=r[13] or '', status=r[14])
                for r in c.execute("SELECT s.ShiftID,s.DID,r.DName,s.StartAt,s.EndAt,s.StartCash,s.Orders,s.Collected,s.Fees,s.Expected,s.Actual,s.Diff,s.ClosedBy,s.Note,s.Status "
                                   "FROM DeliveryShifts s JOIN Deliveries r ON r.DID=s.DID WHERE substr(s.StartAt,1,10)>=? AND substr(s.StartAt,1,10)<=? ORDER BY s.ShiftID DESC", (frm, to))]


def dv_guard_close():
    with db() as c:
        if c.execute("SELECT 1 FROM DeliveryShifts WHERE Status='open' LIMIT 1").fetchone():
            raise Exception('فيه مناديب لسه شفتهم مفتوح - حاسبهم واقفل شفتهم الأول من شاشة الدليفري')


# ------------------------------------------------------------------ backups (v3) - يدوي بس، من غير مسح أوتوماتيكي
BK_DIR = os.path.join(os.path.dirname(DB), 'backups')
BK_RE = re.compile(r'^pos_backup_(?:auto_)?(\d{8})_(\d{6})\.db$')
BK_AUTO_HOUR, BK_AUTO_KEEP = 3, 30  # ★ [EDIT-MAP] ساعة النسخ التلقائي وعدد النسخ المحفوظة


# ★★★ [EDIT-MAP] النسخ الاحتياطي ★★★
def make_backup(auto=False):
    os.makedirs(BK_DIR, exist_ok=True)
    name = ('pos_backup_auto_' if auto else 'pos_backup_') + datetime.now().strftime('%Y%m%d_%H%M%S') + '.db'
    dst = os.path.join(BK_DIR, name)
    src, out = sqlite3.connect(DB, timeout=30), sqlite3.connect(dst)
    try:
        src.backup(out)  # نسخة متسقة حتى لو في بيع شغال
    finally:
        out.close()
        src.close()
    return {'name': name, 'size': os.path.getsize(dst)}


def prune_auto_backups(keep=None):
    # بيمسح النسخ التلقائية القديمة بس (pos_backup_auto_*) ويسيب آخر N - النسخ اليدوي عمره ما بيتمسح من هنا
    keep = keep or BK_AUTO_KEEP
    files = sorted((n for n in os.listdir(BK_DIR) if n.startswith('pos_backup_auto_') and BK_RE.match(n)), reverse=True) if os.path.isdir(BK_DIR) else []
    for n in files[keep:]:
        try:
            os.remove(os.path.join(BK_DIR, n))
        except OSError:
            pass


def auto_backup_loop():
    while True:
        try:
            now = datetime.now()
            if now.hour >= BK_AUTO_HOUR:
                today = now.strftime('%Y%m%d')
                have = os.path.isdir(BK_DIR) and any(n.startswith('pos_backup_auto_' + today) for n in os.listdir(BK_DIR))
                if not have:  # لو الجهاز كان مقفول الساعة 3 بيتاخد أول ما يشتغل
                    make_backup(auto=True)
                    prune_auto_backups()
                    print('Backup تلقائي اتاخد', today)
        except Exception as e:
            print('Backup تلقائي فشل:', e)
        time.sleep(300)


def bk_time(name):
    m = BK_RE.match(name)
    return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S') if m else None


def list_backups():
    if not os.path.isdir(BK_DIR):
        return []
    out = []
    for n in os.listdir(BK_DIR):
        t = bk_time(n)
        if t:
            out.append({'name': n, 'size': os.path.getsize(os.path.join(BK_DIR, n)), 'at': t.strftime('%Y-%m-%d %H:%M:%S')})
    return sorted(out, key=lambda x: x['name'], reverse=True)


def clean_backups(days):
    # بيمسح بس النسخ الأقدم من المدة اللي اتحددت (قرار يدوي)
    days = int(days)
    if days < 1:
        raise Exception('مدة غير صحيحة')
    cut, n = datetime.now() - timedelta(days=days), 0
    for f in list_backups():
        if bk_time(f['name']) < cut:
            os.remove(os.path.join(BK_DIR, f['name']))
            n += 1
    return n


# ------------------------------------------------------------------ الفرع / الشبكة (v5)
def local_ips():
    # بيجيب عناوين IP بتاعة الجهاز على الشبكة عشان تتحط في إعدادات الفرع وتتفتح من الأجهزة التانية
    ips = set()
    try:
        for r in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(r[4][0])
    except OSError:
        pass
    try:
        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.connect(('10.255.255.255', 1))
        ips.add(so.getsockname()[0])
        so.close()
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith('127.') and not i.startswith('169.254.'))


def site_info():
    with db() as c:
        cfg = get_cfg(c)
    return {'name': cfg.get('name', ''), 'branchName': cfg.get('branchName', ''), 'branchCode': cfg.get('branchCode', ''), 'ver': VERSION}


# ------------------------------------------------------------------ http
# ★★★ [EDIT-MAP] 📡 كل مسارات الـ API (do_GET / do_POST) ★★★
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj, ct='application/json'):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ct + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def user(self):
        return TOK.get(self.headers.get('X-Token', ''))

    def events(self):
        # بث لحظي (Server-Sent Events): كل جهاز بيسمع لأي أوردر/تعديل بيحصل من جهاز تاني
        q = parse_qs(urlparse(self.path).query)
        if not TOK.get((q.get('t') or [''])[0]):
            return self.out(401, {'error': 'login'})
        sub = queue.Queue(maxsize=500)
        with SUBS_LOCK:
            SUBS.add(sub)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            while True:
                try:
                    ev = sub.get(timeout=15)
                    self.wfile.write(('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with SUBS_LOCK:
                SUBS.discard(sub)

    def download(self):
        q = parse_qs(urlparse(self.path).query)
        u = TOK.get((q.get('t') or [''])[0])
        name = (q.get('name') or [''])[0]
        if not u or u['role'] != 'admin' or not BK_RE.match(name) or not os.path.isfile(os.path.join(BK_DIR, name)):
            return self.out(403, {'error': 'forbidden'})
        b = open(os.path.join(BK_DIR, name), 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Disposition', 'attachment; filename="%s"' % name)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            try:
                return self.out(200, open(HTML, 'rb').read(), 'text/html')
            except OSError:
                return self.out(500, {'error': 'pos_istakoza.html not found'})
        if p == '/api/site':
            return self.out(200, site_info())
        if p == '/api/events':
            return self.events()
        if p == '/api/backup/download':
            return self.download()
        u = self.user()
        if not u:
            return self.out(401, {'error': 'login'})
        try:
            if p == '/api/state':
                return self.out(200, dict(get_state(), me=dict(u, perm=emp_perms(u.get('job')))))
            if p == '/api/users' and u['role'] == 'admin':
                return self.out(200, list_users())
            if p == '/api/loginlog' and u['role'] == 'admin':
                return self.out(200, login_log())
            if p == '/api/jobs' and u['role'] == 'admin':
                return self.out(200, list_jobs())
            if p == '/api/netinfo' and u['role'] == 'admin':
                return self.out(200, {'ips': local_ips(), 'port': PORT, 'host': socket.gethostname(), 'db': DB, 'html': HTML, 'ver': VERSION})
            if p == '/api/stocktake' and can(u, 'stock'):
                q = parse_qs(urlparse(self.path).query)
                return self.out(200, stocktake_get((q.get('month') or [''])[0]))
            if p == '/api/report/stock' and can(u, 'rep'):
                q = parse_qs(urlparse(self.path).query)
                return self.out(200, report_stock((q.get('from') or [''])[0], (q.get('to') or [''])[0]))
            if p in ('/api/delivery', '/api/rider') and can(u, 'dlv'):
                return self.out(200, dv_state())
            if p in ('/api/delivery/shifts', '/api/rider/shifts') and can(u, 'dlvPay'):
                q = parse_qs(urlparse(self.path).query)
                return self.out(200, shifts_list((q.get('from') or ['0000-00-00'])[0], (q.get('to') or ['9999-99-99'])[0]))
            if p == '/api/store' and can(u, 'po'):
                return self.out(200, store_extra(get_store()))
            if p == '/api/backups' and u['role'] == 'admin':
                return self.out(200, {'dir': BK_DIR, 'files': list_backups()})
            if p == '/api/log':
                return self.out(200, get_log((parse_qs(urlparse(self.path).query).get('sid') or [''])[0]))
            self.out(403, {'error': 'forbidden'})
        except Exception as e:
            self.out(500, {'error': str(e)})

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            d = {}
        p = self.path
        try:
            if p == '/api/login':
                try:
                    u = login(d.get('u', ''), d.get('p', ''), self.client_address[0])
                except LoginLocked as e:
                    return self.out(429, {'error': str(e), 'locked': e.secs})
                if not u:
                    return self.out(400, {'error': 'اسم المستخدم أو كلمة السر غير صحيحة'})
                t = secrets.token_hex(16)
                TOK[t] = u
                return self.out(200, dict(u, perm=emp_perms(u.get('job')), token=t, mode=MODE))
            u = self.user()
            if not u:
                return self.out(401, {'error': 'login'})
            adm = u['role'] == 'admin'
            cid = self.headers.get('X-Cid', '')
            if p == '/api/logout':
                TOK.pop(self.headers.get('X-Token'), None)
            elif p == '/api/state':
                save_state(d, u['role'] if adm or can(u, 'price') else 'none')
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/sale':
                r = add_sale(d, u)
                if not r.get('dup'):
                    broadcast({'t': 'sale', 'sale': d, 'who': u['full'] or u['u'], 'cid': cid})
                return self.out(200, r)
            elif p in ('/api/delivery', '/api/rider') and adm:
                delivery_save(d)
                broadcast({'t': 'reload', 'cid': cid})
            elif p in ('/api/delivery/checkin', '/api/rider/checkin') and can(u, 'dlv'):
                delivery_checkin(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p in ('/api/delivery/dispatch', '/api/rider/dispatch') and can(u, 'dlv'):
                r = delivery_dispatch(d, u)
                broadcast({'t': 'reload', 'cid': cid})
                return self.out(200, r)
            elif p == '/api/sale/deliver' and can(u, 'dlv'):
                r = mark_delivered(d, u)
                broadcast({'t': 'deliver', 'sid': d.get('sid') or d.get('id'), 'cid': cid})
                return self.out(200, r)
            elif p in ('/api/delivery/return', '/api/rider/return') and can(u, 'dlv'):
                delivery_return(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p in ('/api/delivery/recall', '/api/rider/recall') and can(u, 'dlv'):
                delivery_recall(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/dispatch/deliver' and can(u, 'dlv'):
                dispatch_deliver(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/dispatch/refuse' and can(u, 'dlv'):
                dispatch_refuse(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/dispatch/pull' and can(u, 'dlv'):
                dispatch_pull(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p in ('/api/delivery/settle', '/api/rider/settle') and can(u, 'dlvPay'):
                r = delivery_settle(d, u)
                broadcast({'t': 'reload', 'cid': cid})
                return self.out(200, {'ok': 1, 'shift': r})
            elif p == '/api/close' and can(u, 'close'):
                dv_guard_close()
                with LOCK, db() as c:
                    for i in d['ids']:
                        r = c.execute('SELECT SDay FROM Sales WHERE SID=?', (i,)).fetchone()
                        c.execute('UPDATE Sales SET Closed=1 WHERE SID=?', (i,))
                        if r:  # تسجيل اليوم في day_locks
                            c.execute('INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) VALUES (?,?,?)', (r[0], now_s(), u['u']))
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/void' and can(u, 'void'):
                do_void(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/edit' and can(u, 'edit'):
                do_edit(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/return' and can(u, 'return'):
                r = do_return(d, u)
                broadcast({'t': 'reload', 'cid': cid})
                return self.out(200, {'ok': 1, 'ret': r})
            elif adm and p == '/api/return/pay':
                do_return_pay(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/stocktake/save' and can(u, 'stock'):
                stocktake_save(d)
            elif p == '/api/stocktake/close' and can(u, 'stock'):
                stocktake_close(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/recipe':
                recipe_save(d)
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/stocktake/reopen':
                stocktake_reopen(d)
            elif p == '/api/po' and can(u, 'po'):
                return self.out(200, {'ok': 1, 'order': do_po(d, u)})
            elif p == '/api/po/send' and can(u, 'po'):
                return self.out(200, do_po_send(d))
            elif p == '/api/po/stage' and can(u, 'po'):
                do_po_stage(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/po/mark' and can(u, 'po'):
                po_mark(d['id'], 'sent' if d.get('status') == 'sent' else 'pending', str(d.get('err', '')))
            elif adm and p == '/api/po/del':
                with LOCK, db() as c:
                    c.execute('DELETE FROM POItems WHERE POID=?', (d['id'],))
                    c.execute('DELETE FROM POrders WHERE POID=?', (d['id'],))
            elif adm and p == '/api/store/supplier':
                save_supplier(d)
            elif adm and p == '/api/store/item':
                save_store_item(d)
            elif p == '/api/store/template' and can(u, 'po'):
                save_template(d)
            elif p == '/api/print':
                do_print(d)
            elif adm and p == '/api/users':
                return self.out(200, dict(save_user(d), ok=1))
            elif adm and p == '/api/backup':
                return self.out(200, dict(make_backup(), ok=1))
            elif adm and p == '/api/backup/clean':
                return self.out(200, {'ok': 1, 'deleted': clean_backups(d.get('days', 30))})
            elif adm and p == '/api/reset':
                reset()
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/import':
                do_import(d)
                broadcast({'t': 'reload', 'cid': cid})
            else:
                return self.out(403, {'error': 'forbidden'})
            self.out(200, {'ok': 1})
        except Forbidden as e:
            self.out(403, {'error': str(e)})
        except Exception as e:
            print('POST ERROR:', repr(e))
            self.out(400, {'error': str(e)})


def open_pos(url):
    # بيفتح البرنامج كنافذة تطبيق بطباعة صامتة (--kiosk-printing) لو Chrome/Edge موجود، وإلا المتصفح العادي
    if os.name == 'nt':
        pf = [os.environ.get(k, '') for k in ('ProgramFiles', 'ProgramFiles(x86)', 'LocalAppData')]
        cands = [os.path.join(pf[0], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[2], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                 os.path.join(pf[0], 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
        prof = os.path.join(os.environ.get('LOCALAPPDATA', APP_DIR), 'IstakozaPOS_Browser')
        for b in cands:
            if os.path.isfile(b):
                try:
                    subprocess.Popen([b, '--kiosk-printing', '--user-data-dir=' + prof, '--app=' + url])
                    return
                except OSError:
                    pass
    webbrowser.open(url)


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


if __name__ == '__main__':
    if os.name == 'nt':
        os.system('chcp 65001 >nul')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    url = 'http://localhost:%d' % PORT
    if port_busy():
        print('البرنامج شغال بالفعل (البورت %d مستخدم) - هفتحه في المتصفح' % PORT)
        if '--no-browser' not in sys.argv:
            open_pos(url)
        sys.exit(0)
    init()
    print('السيرفر شغال: %s   (من جهاز تاني: http://IP-الجهاز:%d)' % (url, PORT))
    print('مكان البيانات:', DB)
    print('الصفحة:', HTML)
    print('الدخول الأول: admin / admin  - غيّر كلمة السر من الإعدادات')
    print('متقفلش الشاشة السودا دي طول ما بتشتغل.')
    if '--no-browser' not in sys.argv:
        threading.Timer(1.2, lambda: open_pos(url)).start()
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    Srv(('0.0.0.0', PORT), H).serve_forever()
