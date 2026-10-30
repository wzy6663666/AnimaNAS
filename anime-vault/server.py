#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANIME VAULT — 番剧硬盘档案馆
本地零依赖服务：硬盘快照 / 容量分析 / 番剧识别 / 健康监测
仅使用 Python 标准库（sqlite3 + http.server）
"""
import ctypes
import difflib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
COVERS = os.path.join(DATA, 'covers')
DB_PATH = os.path.join(DATA, 'vault.db')
STATIC = os.path.join(BASE, 'static')
PORT = int(os.environ.get('ANIME_VAULT_PORT', '8777'))
UA = 'AnimeVault/1.0 (local anime drive indexer)'

os.makedirs(COVERS, exist_ok=True)

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  letter TEXT, label TEXT, volume_label TEXT,
  volume_guid TEXT UNIQUE, disk_serial TEXT, model TEXT, bus TEXT,
  total_bytes INTEGER DEFAULT 0, free_bytes INTEGER DEFAULT 0,
  last_scan TEXT, last_health TEXT, health_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS shows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  drive_id INTEGER, path TEXT, folder_name TEXT,
  title TEXT, title_auto TEXT, title_cn TEXT,
  fansub TEXT, season INTEGER, episodes TEXT,
  source TEXT, codec TEXT, resolution TEXT, bitdepth TEXT,
  audio TEXT, subtitle TEXT, container TEXT, year INTEGER,
  size INTEGER DEFAULT 0, file_count INTEGER DEFAULT 0, video_count INTEGER DEFAULT 0,
  cover TEXT, cover_src TEXT,
  bgm_id INTEGER, bgm_score REAL, bgm_date TEXT, bgm_name TEXT, bgm_rank INTEGER,
  bgm_tried INTEGER DEFAULT 0, manual INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  drive_id INTEGER, show_id INTEGER,
  path TEXT, parent_path TEXT, name TEXT,
  is_dir INTEGER, size INTEGER DEFAULT 0,
  file_count INTEGER DEFAULT 0, dir_count INTEGER DEFAULT 0,
  depth INTEGER DEFAULT 0, ext TEXT, mtime INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_drive ON entries(drive_id);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(drive_id, parent_path);
CREATE INDEX IF NOT EXISTS idx_entries_show ON entries(show_id);
CREATE INDEX IF NOT EXISTS idx_entries_name ON entries(name);
CREATE INDEX IF NOT EXISTS idx_shows_drive ON shows(drive_id);
CREATE TABLE IF NOT EXISTS roots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  drive_id INTEGER, path TEXT DEFAULT '', label TEXT,
  last_scan TEXT, show_count INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0,
  created_at TEXT, UNIQUE(drive_id, path)
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    for tbl in ('shows', 'entries'):
        cols = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % tbl)}
        if 'root_id' not in cols:
            conn.execute('ALTER TABLE %s ADD COLUMN root_id INTEGER' % tbl)
    for (did,) in conn.execute('SELECT id FROM drives').fetchall():
        r = conn.execute("SELECT id FROM roots WHERE drive_id=? AND path=''", (did,)).fetchone()
        if r:
            rid = r[0]
        else:
            rid = conn.execute('INSERT INTO roots (drive_id,path,label,created_at) VALUES (?,?,?,?)',
                               (did, '', '整个硬盘', now_str())).lastrowid
        conn.execute('UPDATE shows SET root_id=? WHERE drive_id=? AND root_id IS NULL', (rid, did))
        conn.execute('UPDATE entries SET root_id=? WHERE drive_id=? AND root_id IS NULL', (rid, did))
    conn.commit()
    conn.close()


def rows(sql, args=()):
    conn = db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


# ---------------------------------------------------------------- volume / disk

def ps_json(script, timeout=25):
    """执行 PowerShell 并解析 JSON 输出"""
    full = ("$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$ErrorActionPreference='SilentlyContinue';" + script)
    try:
        p = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', full],
                           capture_output=True, timeout=timeout)
        txt = p.stdout.decode('utf-8', errors='replace').strip()
        if not txt:
            return None
        return json.loads(txt)
    except Exception:
        return None


def list_volumes():
    """枚举当前接上的所有数据盘"""
    data = ps_json("Get-Volume | Where-Object {$_.DriveLetter} | Select-Object "
                   "DriveLetter,FileSystemLabel,FileSystem,DriveType,Size,SizeRemaining,UniqueId "
                   "| ConvertTo-Json -Compress")
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for v in data:
        letter = (v.get('DriveLetter') or '').strip()
        if not letter:
            continue
        size = v.get('Size') or 0
        if not size or size < 5 * 1024 ** 3:
            continue
        out.append({
            'letter': letter,
            'volume_label': v.get('FileSystemLabel') or '',
            'fs': v.get('FileSystem') or '',
            'drive_type': v.get('DriveType') or '',
            'total_bytes': size,
            'free_bytes': v.get('SizeRemaining') or 0,
            # 以 ctypes 读取的挂载点 GUID 为准，和 drive_payload 的在线判断保持一致
            'guid': volume_guid_of(letter) or (v.get('UniqueId') or ''),
        })
    return sorted(out, key=lambda x: x['letter'])


def get_health(letter):
    """读取 SMART / 可靠性计数，估算寿命"""
    script = r"""
$p = Get-Partition -DriveLetter %s | Get-Disk
$pd = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $p.Number }
$r = $pd | Get-StorageReliabilityCounter
[PSCustomObject]@{
  disk = $p.FriendlyName; serial = "$($p.SerialNumber)".Trim(); bus = "$($p.BusType)";
  health = "$($p.HealthStatus)"; op = "$($p.OperationalStatus)"; size = $p.Size;
  model = "$($pd.Model)"; media = "$($pd.MediaType)";
  hours = $r.PowerOnHours; temp = $r.Temperature; wear = $r.Wear;
  readErr = $r.ReadErrorsTotal; writeErr = $r.WriteErrorsTotal;
  startStop = $r.StartStopCycleCount; powerOn = $r.PowerOnHours;
  uncorrected = $r.ReadErrorsUncorrected; pending = $r.LoadUnloadCycleCount
} | ConvertTo-Json -Compress
""" % letter
    raw = ps_json(script)
    if not raw:
        return {'available': False, 'reason': '无法读取该盘的 SMART 数据（USB 桥接盒或驱动不支持）'}
    if isinstance(raw, list):
        raw = raw[0]
    h = {
        'available': True,
        'disk': (raw.get('disk') or '').strip(),
        'model': (raw.get('model') or '').strip(),
        'serial': (raw.get('serial') or '').strip(),
        'bus': (raw.get('bus') or '').strip(),
        'media': (raw.get('media') or '').strip(),
        'health_status': raw.get('health') or '',
        'operational': raw.get('op') or '',
        'power_on_hours': raw.get('hours') or 0,
        'temperature': raw.get('temp'),
        'wear': raw.get('wear') or 0,
        'read_errors': raw.get('readErr'),
        'write_errors': raw.get('writeErr'),
        'start_stop': raw.get('startStop'),
        'load_unload': raw.get('pending'),
    }
    h['score'], h['grade'], h['notes'] = health_score(h)
    h['checked_at'] = now_str()
    return h


def health_score(h):
    """基于通电时长/温度/错误计数/厂商健康状态的经验估算（非精确寿命）"""
    score = 100.0
    notes = []
    hours = h.get('power_on_hours') or 0
    if hours:
        # 机械盘设计寿命按 5 年连续通电 ≈ 43800h
        used = min(1.0, hours / 43800.0)
        score -= used * 42
        notes.append('累计通电 %.0f 小时（约 %.1f 年）' % (hours, hours / 8760.0))
    t = h.get('temperature')
    if isinstance(t, (int, float)) and t:
        if t > 45:
            score -= min(12, (t - 45) * 1.5)
            notes.append('温度偏高 %d°C' % t)
        elif t < 15:
            notes.append('温度偏低 %d°C' % t)
    err = (h.get('read_errors') or 0) + (h.get('write_errors') or 0)
    if err:
        score -= min(15, math.log2(1 + err) * 2.5)
        notes.append('累计读写重试 %d 次' % err)
    wear = h.get('wear') or 0
    if wear:
        score -= min(20, wear * 0.2)
    st = (h.get('health_status') or '').lower()
    if 'warning' in st:
        score -= 22
        notes.append('厂商健康状态：警告')
    elif 'unhealthy' in st or 'critical' in st:
        score -= 55
        notes.append('厂商健康状态：异常')
    score = max(3, min(100, round(score)))
    if score >= 85:
        grade = '良好'
    elif score >= 70:
        grade = '正常'
    elif score >= 55:
        grade = '注意'
    else:
        grade = '预警'
    if hours:
        remain = max(0, 43800 - hours)
        notes.append('按每天通电 8 小时估算，剩余约 %.1f 年' % (remain / 8 / 365.0))
    return score, grade, notes


# ---------------------------------------------------------------- name parsing

BRACKET_RE = re.compile(r'\[([^\[\]]{1,80})\]')
KNOWN_GROUPS = [
    'lolihouse', 'nekomoe', 'kissaten', 'ani', 'sakurato', 'ktxp', 'hysub', 'dbd-raws', 'dbd',
    'dmg', 'vcb-studio', 'vcb', 'sumisora', 'comicat', 'romanticat', 'coolcomic', 'haruhana',
    'sweetsub', 'uha-wings', 'airota', '7³acg', '7acg', '343-labs', 'kitaujisub', 'jsum',
    'ohys-raws', 'reinforce', 'moozzi2', 'beatles-raws', 'snow-raws', 'lilith-raws', 'tucapt',
    'loli', 'sakura', 'mabors', 'xrip', 'philosophy-raws', 'ai-raws', 'horriblesubs', 'erai-raws',
    'subsplease', 'nekomo', 'yui', 'suzu', 'aoe', 'mkv', 'raws', 'studio', 'acg', 'labs',
]
SPEC_TOKENS = [
    '1080p', '1080', '720p', '720', '2160p', '2160', '4k', 'uhd', 'hevc', 'avc', 'x265', 'x264',
    'h264', 'h.264', 'h265', 'h.265', 'av1', 'bdrip', 'bdremux', 'bd', 'webrip', 'web-dl', 'webdl',
    'web', 'tvrip', 'tv', 'dvdrip', 'dvd', 'aac', 'flac', 'ac3', 'eac3', 'dts', 'opus', 'mp3',
    '10bit', '10-bit', '8bit', 'ma10p', 'mkv', 'mp4', '简体', '繁体', '简繁', '内封', '外挂',
    '内嵌', '简日', '繁日', '双语', '字幕', 'chs', 'cht', 'jpsc', 'gb', 'big5', 'fin', 'tv全集',
    '全集', '合集', 'sp', 'ncop', 'nced', 'hevc-10bit', 'avc aac', 'fansub banner',
    # 流媒体平台标记
    'baha', 'bilibili', 'b-global', 'netflix', 'amzn', 'amazon', 'cr', 'crunchyroll', 'abema',
    'iqiyi', 'gaga', 'disney', 'hulu', 'u-next', 'hidive', 'adn', 'vrv', 'sentai',
    'chi_jpn', 'chi', 'jpn', 'japanese', 'sc', 'tc',
]
CJK_SPEC_WORDS = ('内封', '外挂', '内嵌', '简繁', '简体', '繁体', '简日', '繁日', '双语',
                  '字幕', '全集', '合集', '特典', '映像')
DATE_RE = re.compile(r'^(19|20)\d{4,6}$')
EP_RANGE_RE = re.compile(r'(?<![\d.])(\d{1,3})\s*[-~]\s*(\d{1,3})(?![\d.])')


def norm_token(t):
    return t.strip().lower().replace('_', ' ')


def is_spec_token(t):
    tl = norm_token(t)
    if DATE_RE.match(tl.replace(' ', '')):
        return True
    if EP_RANGE_RE.fullmatch(tl.strip()) or re.fullmatch(r'\d{1,3}\s*[-~]\s*\d{1,3}.*', tl):
        return True
    if tl.startswith('[') or tl in ('', '-'):
        return True
    if re.fullmatch(r'\d{3,4}\s*[x×*]\s*\d{3,4}', tl):
        return True
    if any(w in t for w in CJK_SPEC_WORDS):
        return True
    flat = re.sub(r'[\s_]+', '', tl)
    if any(flat == re.sub(r'[\s_]+', '', x) for x in SPEC_TOKENS):
        return True
    # 纯规格串，如 "1080p hevc-10bit aac assx2"
    words = re.split(r'[\[\]\s/&,]+', tl)
    hits = sum(1 for w in words if w and (w in SPEC_TOKENS or any(w.startswith(s) for s in
                ('1080', '720', '2160', 'hevc', 'avc', 'x26', 'h26', 'bdrip', 'webrip', 'web-dl',
                 'tvrip', 'baha', 'bilibili', 'cr', 'ass', 'srt', 'flac', 'aac', 'ac3', 'eac3',
                 'dts', 'opus', 'ma10p', '10bit', '8bit', 'mkv', 'mp4'))))
    if hits and hits >= max(1, len([w for w in words if w]) // 2):
        return True
    return False


def is_group_token(t):
    tl = norm_token(t)
    if any(g == tl or g in tl for g in KNOWN_GROUPS):
        return True
    if '&' in t and not any(ch.isdigit() for ch in t):
        return True
    return False


def parse_specs(text):
    """从任意名称里抽取视频规格"""
    t = text.replace('＆', '&').replace('（', '(').replace('）', ')')
    tl = t.lower()
    out = {}

    if re.search(r'bd(rip|remux|box)?\b|blu-?ray|bdmv', tl):
        out['source'] = 'BD'
    elif re.search(r'web-?dl|webrip|web-?rip', tl):
        out['source'] = 'WEB-DL' if 'web-dl' in tl or 'webdl' in tl else 'WEBRip'
    elif re.search(r'tvrip|\btv\b', tl):
        out['source'] = 'TVRip'
    elif 'dvd' in tl:
        out['source'] = 'DVD'

    if re.search(r'hevc|x265|h\.?265|ma10p', tl):
        out['codec'] = 'HEVC'
    elif re.search(r'avc|x264|h\.?264', tl):
        out['codec'] = 'H.264'
    elif 'av1' in tl:
        out['codec'] = 'AV1'

    m = re.search(r'(2160|1080|720|480)\s*[pP]', t)
    if m:
        out['resolution'] = m.group(1) + 'p'
    elif re.search(r'\b4k\b|uhd', tl):
        out['resolution'] = '2160p'
    else:
        md = re.search(r'(3840|1920|1280)\s*[x×*]\s*(2160|1080|720)', tl)
        if md:
            out['resolution'] = {'2160': '2160p', '1080': '1080p', '720': '720p'}[md.group(2)]

    if re.search(r'10-?bit|ma10p', tl):
        out['bitdepth'] = '10bit'
    elif re.search(r'8-?bit', tl):
        out['bitdepth'] = '8bit'

    audios = []
    for pat, name in ((r'flac', 'FLAC'), (r'eac3', 'EAC3'), (r'ac3', 'AC3'), (r'dts', 'DTS'),
                      (r'opus', 'Opus'), (r'aac', 'AAC'), (r'\bmp3\b', 'MP3')):
        if re.search(pat, tl):
            audios.append(name)
    if audios:
        out['audio'] = '/'.join(dict.fromkeys(audios))

    if '简繁内封' in t:
        out['subtitle'] = '简繁内封'
    elif '简繁外挂' in t or '简繁日双语外挂' in t or ('简繁' in t and '外挂' in t):
        out['subtitle'] = '简繁外挂'
    elif '简繁日' in t and '内嵌' in t:
        out['subtitle'] = '简繁日内嵌'
    elif '简体内嵌' in t or '简日双语内嵌' in t or ('简' in t and '内嵌' in t):
        out['subtitle'] = '简体内嵌'
    elif '繁体内嵌' in t:
        out['subtitle'] = '繁体内嵌'
    elif re.search(r'ass\s*[x×]\s*[2-9]|srt\s*[x×]\s*[2-9]', tl):
        out['subtitle'] = '简繁内封'
    elif re.search(r'\bass\b|\bsrt\b', tl) and not re.search(r'ass\s*[x×]\s*0', tl):
        out['subtitle'] = '内封'
    elif re.search(r'chs\s*&\s*cht|chs_cht', tl):
        out['subtitle'] = '简繁'
    elif 'jpsc' in tl:
        out['subtitle'] = '简日'
    elif re.search(r'chi_jpn|chi&jpn|sc_jpn', tl):
        out['subtitle'] = '简日'
    elif re.search(r'\bchi\b', tl) and not re.search(r'cht|tc\b', tl):
        out['subtitle'] = '简体'
    elif re.search(r'chs_jpn|chs&jpn|gb_?(&|＆)_?jp', tl):
        out['subtitle'] = '简日'
    elif re.search(r'gb\s*[&＆]\s*jp', tl):
        out['subtitle'] = '简日'
    elif re.search(r'\bcht\b', tl) or 'big5' in tl or '繁体' in t:
        out['subtitle'] = '繁体'
    elif re.search(r'\bchs\b', tl) or re.search(r'\bgb\b', tl) or '简体' in t or '简中' in t:
        out['subtitle'] = '简体'

    if re.search(r'\bmkv\b', tl):
        out['container'] = 'MKV'
    elif re.search(r'\bmp4\b', tl):
        out['container'] = 'MP4'

    ms = re.search(r'\bS(\d{1,2})\b', t) or re.search(r'第([一二三四五六七八九十\d]{1,3})季', t) \
        or re.search(r'season\s*(\d{1,2})', tl)
    if ms:
        s = ms.group(1)
        cn = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        try:
            out['season'] = int(cn.get(s, s))
        except Exception:
            pass

    me = re.search(r'\[(\d{1,3}\s*[-~]\s*\d{1,3})[^\]]*\]', t)
    if me:
        out['episodes'] = re.sub(r'\s*[-~]\s*', '-', me.group(1))
    else:
        me2 = re.search(r'(\d{1,3})\s*[-~]\s*(\d{1,3})\s*(?:TV)?(?:全集|Fin)?', t)
        if me2 and abs(int(me2.group(2)) - int(me2.group(1))) < 200:
            out['episodes'] = '%s-%s' % (me2.group(1), me2.group(2))

    for g in ('完结', 'Fin', '合集', '全集'):
        if g in t:
            out['complete'] = True
            break
    return out


def parse_folder_name(folder):
    """解析顶层文件夹名 → 标题 / 汉化组 / 规格"""
    s = folder
    tokens = [(m.start(), m.end(), m.group(1).strip()) for m in BRACKET_RE.finditer(s)]
    groups, keep_title_parts = [], []
    for _, _, tok in tokens:
        if is_group_token(tok) and not is_spec_token(tok):
            groups.append(tok)
        elif is_spec_token(tok):
            continue
        else:
            keep_title_parts.append(tok)

    # 情况：[BDrip] NEW GAME! S01 [7³ACG] —— 组名在末尾
    leftover = BRACKET_RE.sub(' ', s)
    leftover = re.sub(r'\s+', ' ', leftover).strip(' -_·')
    leftover_clean = re.sub(r'\bS\d{1,2}\b', ' ', leftover)
    leftover_clean = re.sub(r'第[一二三四五六七八九十\d]{1,3}季', ' ', leftover_clean)
    leftover_clean = re.sub(r'\s+', ' ', leftover_clean).strip(' -_·')

    # 括号外的文字通常才是真正的片名；只有它为空时才用括号里的内容
    bracket_title = ' '.join(keep_title_parts).strip()
    title = ''
    if len(re.sub(r'[\s\-_·~]', '', leftover_clean)) >= 2:
        title = leftover_clean
        if bracket_title and re.search(r'[\u4e00-\u9fff]{2,}', bracket_title) \
                and not re.search(r'[\u4e00-\u9fff]', leftover_clean):
            title = bracket_title
    else:
        title = bracket_title or leftover_clean or folder
    title = re.sub(r'\s+', ' ', title).strip(' -_·')

    # 汉化组：优先已知组名（多组标 & 时取最具体的那个），其次首个括号
    fansub = ''
    if groups:
        cands = [g for g in groups if any(k == norm_token(g) or k in norm_token(g) for k in KNOWN_GROUPS)]
        pool = cands or groups
        pool = sorted(pool, key=lambda g: (g.count('&') + g.count('＆'), len(g)), reverse=True)
        fansub = pool[0]
    if not fansub:
        for _, _, tok in tokens:
            if not is_spec_token(tok):
                fansub = tok
                break
    fansub = re.sub(r'\s+', ' ', fansub).strip()

    specs = parse_specs(s)
    # 清理标题里残留的规格/季数标记
    title = re.sub(r'\bS\d{1,2}\b', ' ', title)
    title = re.sub(r'(?i)season\s*\d+', ' ', title)
    title = re.sub(r'第[一二三四五六七八九十\d]{1,3}季', ' ', title)
    title = re.sub(r'(简繁|简体|繁体|简日|繁日|双语)?(内封|外挂|内嵌)', ' ', title)
    title = re.sub(r'(?i)\b(fin|tv全集|全集|合集|sp|ncop|nced)\b', ' ', title)
    title = re.sub(r'(?i)\b(flac|aac|ac3|eac3|dts|opus|ass|srt|sub)\s*[x×]\s*\d+\b', ' ', title)
    title = re.sub(r'[\[\(（【]\s*[\]\)）】]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -_·~～,，。')
    # 如果标题被清空，退回原名
    if not re.sub(r'[\s\-_·~～]', '', title):
        title = folder
    # 中文名（如果标题里有 CJK 就当作中文名）
    title_cn = title if re.search(r'[\u4e00-\u9fff]', title) else ''
    return {'title': title, 'fansub': fansub, 'title_cn': title_cn, 'specs': specs}


# ---------------------------------------------------------------- scanning

SKIP_DIRS = {'$recycle.bin', 'system volume information', '.trash', '.trashes', 'recycler',
             '#recycle', 'found.000', '.git', 'node_modules', '@eadir', '.ds_store'}
VIDEO_EXT = {'.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.rmvb', '.wmv', '.flv', '.mov', '.webm'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}

SCAN = {'active': False, 'drive_id': None, 'letter': '', 'phase': 'idle', 'path': '',
        'files': 0, 'dirs': 0, 'bytes': 0, 'started': 0, 'message': '', 'elapsed': 0}
MATCH = {'active': False, 'drive_id': None, 'total': 0, 'done': 0, 'matched': 0,
         'failed': 0, 'message': '', 'current': '', 'mode': 'all'}
LOCK = threading.Lock()


def human_path(p):
    return p if len(p) < 74 else p[:28] + ' … ' + p[-42:]


def scan_thread(drive_id, root_id, scan_dir, prefix='', letter=''):
    """扫描某个目录（磁盘根或指定子文件夹），写入快照"""
    global SCAN
    t0 = time.time()
    all_rows = []
    state = {'files': 0, 'dirs': 0, 'bytes': 0}
    last_push = [0.0]

    def push(path, force=False):
        now = time.time()
        if not force and now - last_push[0] < 0.12:
            return
        last_push[0] = now
        SCAN.update(path=human_path(path), files=state['files'], dirs=state['dirs'],
                    bytes=state['bytes'], elapsed=round(now - t0, 1))

    def walk(abs_dir, rel_dir, depth):
        total, nfiles, ndirs = 0, 0, 0
        try:
            with os.scandir(abs_dir) as it:
                items = list(it)
        except OSError:
            return 0, 0, 0
        for e in items:
            name = e.name
            rel = (rel_dir + '/' + name) if rel_dir else name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if e.is_dir(follow_symlinks=False):
                if name.lower() in SKIP_DIRS:
                    continue
                sz, nf, nd = walk(e.path, rel, depth + 1)
                all_rows.append((drive_id, root_id, rel, rel_dir, name, 1, sz, nf, nd, depth,
                                 '', int(st.st_mtime)))
                total += sz
                nfiles += nf
                ndirs += nd + 1
                state['dirs'] += 1
                push(e.path)
            else:
                sz = st.st_size
                ext = os.path.splitext(name)[1].lower()
                all_rows.append((drive_id, root_id, rel, rel_dir, name, 0, sz, 1, 0, depth,
                                 ext, int(st.st_mtime)))
                total += sz
                nfiles += 1
                state['files'] += 1
                state['bytes'] += sz
                if state['files'] % 120 == 0:
                    push(e.path)
        return total, nfiles, ndirs

    try:
        SCAN.update(phase='walking', message='正在遍历目录…', started=t0)
        walk(scan_dir, prefix, 0)

        # 扫子文件夹时，补上祖先目录，保证「目录」页能一层层点进来
        parts = [x for x in prefix.split('/') if x]
        for i in range(len(parts)):
            vpath = '/'.join(parts[:i + 1])
            vparent = '/'.join(parts[:i])
            all_rows.append((drive_id, root_id, vpath, vparent, parts[i], 1,
                             state['bytes'], state['files'], 0, -1, '', int(time.time())))

        SCAN.update(phase='indexing', message='正在写入快照…', files=state['files'],
                    dirs=state['dirs'], bytes=state['bytes'])
        build_shows(drive_id, root_id, all_rows, (letter + ':/') if letter else '',
                    state['bytes'])
        total, used, free = shutil.disk_usage(letter + ':/')
        conn = db()
        conn.execute('UPDATE drives SET last_scan=?, total_bytes=?, free_bytes=? WHERE id=?',
                     (now_str(), total, free, drive_id))
        conn.execute('UPDATE roots SET last_scan=?, show_count=(SELECT COUNT(*) FROM shows '
                     'WHERE root_id=?), total_bytes=? WHERE id=?',
                     (now_str(), root_id, state['bytes'], root_id))
        conn.commit()
        conn.close()
        SCAN.update(phase='done', active=False, message='扫描完成', path='',
                    elapsed=round(time.time() - t0, 1))
        threading.Thread(target=refresh_health_quiet, args=(drive_id, letter), daemon=True).start()
    except Exception as ex:  # noqa
        SCAN.update(phase='error', active=False, message='扫描失败：%s' % ex)
        print('[scan error]', ex)


def rescan_worker(drive_id, roots, letter):
    """依次重扫一块盘下的所有扫描目录"""
    for i, r in enumerate(roots):
        if i:
            SCAN.update(active=True, phase='starting', files=0, dirs=0, bytes=0, path='')
        SCAN.update(root_id=r['id'], scan_path=r['path'], message='(%d/%d) %s' % (
            i + 1, len(roots), r['path'] or '整个硬盘'), queue_idx=i, started=time.time())
        scan_thread(drive_id, r['id'], letter + ':/' + r['path'], r['path'], letter)
        if SCAN.get('phase') == 'error':
            break
    SCAN.update(active=False, phase='done', message='全部扫描目录已更新', path='')


def refresh_health_quiet(drive_id, letter):
    try:
        h = get_health(letter)
        if h.get('available'):
            c = db()
            c.execute('UPDATE drives SET health_json=?, last_health=? WHERE id=?',
                      (json.dumps(h, ensure_ascii=False), now_str(), drive_id))
            c.commit()
            c.close()
    except Exception:
        pass


def build_shows(drive_id, root_id, all_rows, drive_root, scanned_bytes=0):
    """按扫描目录下的第一层文件夹聚合，解析番剧元数据，识别本地海报"""
    conn = db()
    prev = {}
    for r in conn.execute('SELECT folder_name,title,title_auto,title_cn,fansub,cover,cover_src,'
                          'bgm_id,bgm_score,bgm_date,bgm_name,manual,path FROM shows WHERE root_id=?',
                          (root_id,)):
        prev[r['path']] = dict(r)
    conn.execute('DELETE FROM entries WHERE root_id=?', (root_id,))
    conn.execute('DELETE FROM shows WHERE root_id=?', (root_id,))
    conn.executemany('INSERT INTO entries (drive_id,root_id,path,parent_path,name,is_dir,size,'
                     'file_count,dir_count,depth,ext,mtime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     all_rows)

    tops = {}
    for r in all_rows:
        if r[9] == 0 and r[5] == 1:      # depth 0 的目录 = 番剧文件夹
            tops[r[2]] = {'size': 0, 'files': 0, 'videos': 0, 'images': []}
    for r in all_rows:
        rel, is_dir, size, ext = r[2], r[5], r[6], r[10]
        if is_dir:
            continue
        top = rel.rsplit('/', 1)[0] if '/' in rel else ''
        if top in tops:
            tops[top]['size'] += size
            tops[top]['files'] += 1
            if ext in VIDEO_EXT:
                tops[top]['videos'] += 1
            if ext in IMG_EXT:
                tops[top]['images'].append((rel, size, r[4]))

    show_ids = {}
    for top, info in sorted(tops.items(), key=lambda kv: -kv[1]['size']):
        folder = top.split('/')[-1]
        meta = parse_folder_name(folder)
        specs = dict(meta['specs'])
        votes = {}
        vids = [r for r in all_rows if r[5] == 0 and r[10] in VIDEO_EXT
                and r[2].startswith(top + '/')]
        for r in vids[:40]:
            sp = parse_specs(r[4])
            for k, v in sp.items():
                if k in ('episodes', 'season', 'complete'):
                    continue
                votes.setdefault(k, {})
                votes[k][v] = votes[k].get(v, 0) + 1
        for k, vv in votes.items():
            if not specs.get(k):
                specs[k] = max(vv.items(), key=lambda kv: kv[1])[0]
        BD_GROUPS = ('vcb-studio', 'dbd-raws', '7³acg', '343-labs', 'philosophy-raws',
                     'snow-raws', 'moozzi2', 'beatles-raws', 'reinforce', 'kitaujisub')
        if not specs.get('source') and meta['fansub'] and any(
                g in meta['fansub'].lower() for g in BD_GROUPS):
            specs['source'] = 'BD'
        if not specs.get('fansub'):
            for r in vids[:10]:
                sp = parse_folder_name(r[4])
                if sp['fansub']:
                    meta['fansub'] = sp['fansub']
                    break
        episodes = specs.get('episodes') or ('%d 话' % info['videos'] if info['videos'] else '')

        cur = conn.execute(
            'INSERT INTO shows (drive_id,root_id,path,folder_name,title,title_auto,title_cn,fansub,'
            'season,episodes,source,codec,resolution,bitdepth,audio,subtitle,container,size,'
            'file_count,video_count,cover,cover_src,manual) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
            (drive_id, root_id, top, folder, meta['title'], meta['title'], meta['title_cn'],
             meta['fansub'], specs.get('season'), episodes, specs.get('source'),
             specs.get('codec'), specs.get('resolution'), specs.get('bitdepth'),
             specs.get('audio'), specs.get('subtitle'), specs.get('container'), info['size'],
             info['files'], info['videos'], None, None))
        sid = cur.lastrowid
        show_ids[top] = sid

        poster = pick_poster(info['images'], drive_root)
        if poster:
            src = os.path.join(drive_root, poster[0].replace('/', os.sep))
            dst_name = 'd%d_s%d%s' % (drive_id, sid, os.path.splitext(src)[1].lower())
            dst = os.path.join(COVERS, dst_name)
            try:
                shutil.copyfile(src, dst)
                conn.execute('UPDATE shows SET cover=?, cover_src=? WHERE id=?',
                             ('/covers/' + dst_name, 'local', sid))
            except Exception:
                pass

    # 回填 show_id：向上找最近的番剧目录
    upd = []
    for r in conn.execute('SELECT id,path FROM entries WHERE root_id=?', (root_id,)):
        p = r['path']
        while p:
            if p in show_ids:
                upd.append((show_ids[p], r['id']))
                break
            p = p.rsplit('/', 1)[0] if '/' in p else ''
    conn.executemany('UPDATE entries SET show_id=? WHERE id=?', upd)

    # 恢复上次的封面与手动修改
    for path, p in prev.items():
        sid = show_ids.get(path)
        if not sid:
            continue
        if p.get('manual'):
            conn.execute('UPDATE shows SET title=?, title_cn=?, fansub=?, cover=?, cover_src=?, '
                         'bgm_id=?, bgm_score=?, bgm_date=?, bgm_name=?, manual=1 WHERE id=?',
                         (p['title'], p['title_cn'], p['fansub'], p['cover'], p['cover_src'],
                          p['bgm_id'], p['bgm_score'], p['bgm_date'], p['bgm_name'], sid))
        elif p.get('cover') and p.get('cover_src') in ('bangumi', 'anilist', 'bgm', 'manual'):
            conn.execute('UPDATE shows SET cover=?, cover_src=?, bgm_id=?, bgm_score=?, '
                         'bgm_date=?, bgm_name=? WHERE id=?',
                         (p['cover'], p['cover_src'], p['bgm_id'], p['bgm_score'],
                          p['bgm_date'], p['bgm_name'], sid))
    conn.commit()
    conn.close()


def img_size(path):
    """不依赖第三方库读取 JPEG/PNG/WebP 尺寸"""
    try:
        with open(path, 'rb') as f:
            head = f.read(32)
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')
            if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                if head[12:16] == b'VP8X':
                    w = int.from_bytes(head[24:27], 'little') + 1
                    h = int.from_bytes(head[27:30], 'little') + 1
                    return w, h
                if head[12:16] == b'VP8 ':
                    w = int.from_bytes(head[26:28], 'little') & 0x3FFF
                    h = int.from_bytes(head[28:30], 'little') & 0x3FFF
                    return w, h
            if head[:2] == b'\xff\xd8':
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b'\xff':
                        b = f.read(1)
                    if not b:
                        break
                    marker = f.read(1)
                    if not marker:
                        break
                    m = marker[0]
                    if m in (0xd8, 0x01) or 0xd0 <= m <= 0xd7:
                        continue
                    ln = int.from_bytes(f.read(2), 'big')
                    if 0xc0 <= m <= 0xcf and m not in (0xc4, 0xc8, 0xcc):
                        f.read(1)
                        h = int.from_bytes(f.read(2), 'big')
                        w = int.from_bytes(f.read(2), 'big')
                        return w, h
                    f.seek(ln - 2, 1)
    except Exception:
        pass
    return 0, 0


BAD_IMG_HINT = ('banner', 'label', 'cd_', 'logo', 'icon', 'back', 'obi', 'clear',
                'shikishi', 'key_anime', 'fansub')
# 这些目录/文件名一律不当封面（光盘扫描件、小册子、菜单图）
HARD_BAD = ('scans', 'scan', '/booklet', 'booklet', '/disc', 'menus', 'menu_', 'cds', 'sp&s', 'extras')
GOOD_IMG_HINT = ('poster', 'cover', 'folder', 'package', 'jacket', 'main', 'kv')


def pick_poster(images, root):
    """挑选最像竖版海报的本地图片；宁可没有，也不要拿横版 banner / 光盘扫描图凑数"""
    scored = []
    for rel, size, name in images:
        n = (name + ' ' + rel).lower()   # 目录名也算，Scans/ 里的图要排除
        if size < 40 * 1024:
            continue
        if any(k in n for k in HARD_BAD):
            continue
        full = os.path.join(root, rel.replace('/', os.sep))
        w, h = img_size(full)
        if not w or not h:
            continue
        bad = any(k in n for k in BAD_IMG_HINT)
        good = any(k in n for k in GOOD_IMG_HINT)
        portrait = h >= w * 1.28
        ratio_score = h / float(w) if w else 0
        score = 0.0
        if good:
            score += 3
        if portrait:
            score += 2
        if bad:
            score -= 4
        if not portrait and not good:
            score -= 3.5
        if w < 300 or h < 300:
            score -= 2
        score += min(1.0, ratio_score / 1.6)
        scored.append((score, w * h, rel, w, h))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], -x[1]))
    best = scored[0]
    if best[0] < 0.5:          # 全是横版/扫描图，交给联网封面
        return None
    return (best[2], best[0], best[3], best[4])



# ---------------------------------------------------------------- bangumi

def http_json(url, data=None, timeout=12):
    req = urllib.request.Request(url, data=data,
                                 headers={'User-Agent': UA, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))


def bgm_search(keyword):
    body = json.dumps({'keyword': keyword, 'filter': {'type': [2]}}).encode()
    js = http_json('https://api.bgm.tv/v0/search/subjects?limit=6', body)
    return js.get('data') or []


def clean_for_search(t):
    t = re.sub(r'\[[^\]]*\]', ' ', t)
    t = re.sub(r'\bS\d{1,2}\b|Season\s*\d+|第[一二三四五六七八九十\d]{1,3}季', ' ', t)
    t = re.sub(r'[\(\（][^\)\）]*[\)\）]', ' ', t)
    t = re.sub(r'[~～_·]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip(' -!！?？.')


ANILIST_URL = 'https://graphql.anilist.co'
BGM_DOWN = {'until': 0.0}


def anilist_search(q, per=6):
    query = ('query($s:String,$n:Int){Page(perPage:$n){media(search:$s,type:ANIME,sort:SEARCH_MATCH){'
             'id title{romaji native english} coverImage{extraLarge large} averageScore '
             'startDate{year} siteUrl}}}')
    body = json.dumps({'query': query, 'variables': {'s': q, 'n': per}}).encode()
    js = http_json(ANILIST_URL, body, timeout=10)
    return ((js or {}).get('data') or {}).get('Page', {}).get('media') or []


def ascii_run(t):
    found = re.findall(r"[A-Za-z][A-Za-z0-9!'&:.\- ]{2,}", t or '')
    found = [x.strip() for x in found if len(x.strip()) >= 4]
    return max(found, key=len) if found else ''


def romaji_candidates(show, limit=4):
    """从标题与视频文件名里猜罗马音/英文片名（AniList 对中文检索支持很差）"""
    out = []
    t = show.get('title') or ''
    if t and re.fullmatch(r'[\x20-\x7e]+', t):
        out.append(t)
    else:
        a = ascii_run(t)
        if a:
            out.append(a)
    try:
        vids = rows("SELECT name FROM entries WHERE show_id=? AND is_dir=0 AND ext IN "
                    "('.mkv','.mp4','.avi','.ts','.m2ts','.rmvb','.mov') ORDER BY name LIMIT 4",
                    (show['id'],))
    except Exception:
        vids = []
    for v in vids:
        n = os.path.splitext(v['name'])[0]
        n = BRACKET_RE.sub(' ', n)
        n = re.sub(r'\([^)]*\)', ' ', n)
        n = re.sub(r'(?i)\bS\d{1,2}E\d{1,3}\b', ' ', n)
        n = re.sub(r'(?i)\b(ep?|episode)\s*\d{1,3}\b', ' ', n)
        n = re.sub(r'\s*[-–—]\s*\d{1,3}(v\d)?\b.*$', ' ', n)
        n = re.sub(r'\s+', ' ', n).strip(' -_.')
        if len(n) >= 4 and re.search(r'[A-Za-z]{3}', n):
            out.append(n)
    # 中文名也试一次：AniList 收录了不少中文别名，命中结果唯一时可信度高
    if re.search(r'[\u4e00-\u9fff]{3,}', t):
        out.append(t)
    seen, uniq = set(), []
    for x in out:
        k = re.sub(r'\W', '', x.lower())
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


def _ratio(a, b):
    return difflib.SequenceMatcher(None, (a or '').lower(), (b or '').lower()).ratio()


def pick_bgm(hits, q, min_score=0.45):
    best, best_score = None, 0.0
    for h in hits:
        img = h.get('images') or {}
        if not (img.get('large') or img.get('common') or img.get('medium')):
            continue
        ratio = max(_ratio(q, h.get('name_cn') or ''), _ratio(q, h.get('name') or ''))
        if ratio > best_score:
            best, best_score = h, ratio
    if not best or best_score < min_score:
        return None
    img = best.get('images') or {}
    return {
        'id': best.get('id'),
        'name': best.get('name_cn') or best.get('name'),
        'native': best.get('name') or '',
        'score': best_score,
        'rating': (best.get('rating') or {}).get('score'),
        'year': (best.get('date') or '')[:4],
        'images': [u for u in (img.get('large'), img.get('common'), img.get('medium')) if u],
        'site': 'https://bgm.tv/subject/%s' % best.get('id'),
    }


def pick_anilist(hits, q, min_score=0.42):
    if re.search(r'[\u4e00-\u9fff]', q) and 0 < len(hits) <= 2:
        h = hits[0]
        ci = h.get('coverImage') or {}
        if ci.get('extraLarge') or ci.get('large'):
            ti = h.get('title') or {}
            return {
                'id': h.get('id'),
                'name': ti.get('native') or ti.get('romaji') or '',
                'native': ti.get('romaji') or '',
                'score': 0.5,
                'rating': round((h.get('averageScore') or 0) / 10.0, 1) if h.get('averageScore') else None,
                'year': str((h.get('startDate') or {}).get('year') or ''),
                'images': [u for u in (ci.get('extraLarge'), ci.get('large')) if u],
                'site': h.get('siteUrl') or '',
            }
    best, best_score = None, 0.0
    for h in hits:
        ci = h.get('coverImage') or {}
        if not (ci.get('extraLarge') or ci.get('large')):
            continue
        titles = [(h.get('title') or {}).get(k) or '' for k in ('romaji', 'native', 'english')]
        ratio = max([_ratio(q, x) for x in titles if x] or [0])
        if ratio > best_score:
            best, best_score = h, ratio
    if not best or best_score < min_score:
        return None
    ti = best.get('title') or {}
    ci = best.get('coverImage') or {}
    return {
        'id': best.get('id'),
        'name': ti.get('native') or ti.get('romaji') or '',
        'native': ti.get('romaji') or '',
        'score': best_score,
        'rating': round((best.get('averageScore') or 0) / 10.0, 1) if best.get('averageScore') else None,
        'year': str((best.get('startDate') or {}).get('year') or ''),
        'images': [u for u in (ci.get('extraLarge'), ci.get('large')) if u],
        'site': best.get('siteUrl') or '',
    }


def match_one(show, min_score=0.45):
    """先查 Bangumi（中文检索友好），不行再查 AniList（罗马音 + 高清竖版封面）"""
    q = clean_for_search(show['title_cn'] or show['title'] or show['folder_name'])
    result = None

    if time.time() > BGM_DOWN['until'] and q:
        try:
            hits = []
            for attempt in (q, q[:6], re.sub(r'[^\w\u4e00-\u9fff ]', ' ', q).strip()):
                if not attempt:
                    continue
                hits = bgm_search(attempt)
                if hits:
                    break
            result = pick_bgm(hits, q, min_score)
        except Exception:
            BGM_DOWN['until'] = time.time() + 600   # 10 分钟内不再尝试 Bangumi

    if not result:
        for cq in romaji_candidates(show):
            try:
                hits = anilist_search(cq)
            except Exception:
                hits = []
            result = pick_anilist(hits, cq)
            if result:
                break

    if not result:
        return {'ok': False, 'reason': '图源均未匹配到结果'}

    cover = None
    for url in result['images']:
        cover = save_cover(show['drive_id'], show['id'], url)
        if cover:
            break
    if not cover:
        return {'ok': False, 'reason': '匹配到了条目但封面下载失败'}

    c = db()
    c.execute('UPDATE shows SET bgm_id=?, bgm_score=?, bgm_date=?, bgm_name=?, bgm_tried=1, '
              'bgm_rank=?, cover=?, cover_src=? WHERE id=?',
              (result['id'], result.get('rating'), result.get('year') or None,
               result.get('name'), result['score'], cover,
               'bangumi' if 'bgm.tv' in (result.get('site') or '') else 'anilist',
               show['id']))
    c.commit()
    c.close()
    return {'ok': True, 'name': result.get('name'), 'score': result['score'],
            'cover': cover, 'site': result.get('site')}



def match_worker(drive_id, scope='all'):
    """scope=all 匹配全部未处理条目；scope=covers 只重做缺失/本地封面"""
    MATCH.update(active=True, drive_id=drive_id, done=0, matched=0, failed=0,
                 message='正在连接图源…', current='', mode=scope)
    conn = db()
    if scope == 'covers':
        sql = ("SELECT * FROM shows WHERE drive_id=? AND manual=0 AND "
               "(cover IS NULL OR cover_src='local')")
    else:
        sql = ("SELECT * FROM shows WHERE drive_id=? AND (bgm_tried=0 OR cover IS NULL)")
    items = [dict(r) for r in conn.execute(sql, (drive_id,))]
    MATCH['total'] = len(items)
    conn.close()
    consec_fail = 0
    for i, s in enumerate(items):
        if not MATCH['active']:
            break
        MATCH.update(done=i, current=s['title'],
                     message='正在匹配 %d/%d' % (i + 1, len(items)))
        try:
            res = match_one(s)
            consec_fail = 0 if res.get('ok') else consec_fail
        except Exception:
            consec_fail += 1
            if consec_fail >= 4:
                MATCH.update(active=False, message='图源均连接失败，请检查网络')
                return
            continue
        if res.get('ok'):
            MATCH['matched'] += 1
        else:
            MATCH['failed'] += 1
            c = db()
            c.execute('UPDATE shows SET bgm_tried=1 WHERE id=?', (s['id'],))
            c.commit()
            c.close()
        time.sleep(0.7)
    MATCH.update(active=False, done=MATCH['total'], message='封面匹配完成')



def gather_candidates(show):
    """汇总多个图源的候选封面，供用户手动挑选"""
    out = []
    q = clean_for_search(show['title_cn'] or show['title'] or show['folder_name'])
    if time.time() > BGM_DOWN['until'] and q:
        try:
            hits = bgm_search(q) or []
            for h in hits[:8]:
                img = h.get('images') or {}
                url = img.get('large') or img.get('common') or img.get('medium')
                if not url:
                    continue
                out.append({
                    'source': 'Bangumi',
                    'name': h.get('name_cn') or h.get('name'),
                    'native': h.get('name') if h.get('name_cn') else '',
                    'year': (h.get('date') or '')[:4],
                    'rating': (h.get('rating') or {}).get('score'),
                    'image': url,
                    'site': 'https://bgm.tv/subject/%s' % h.get('id'),
                })
        except Exception:
            BGM_DOWN['until'] = time.time() + 600
    for cq in romaji_candidates(show, 2):
        try:
            hits = anilist_search(cq, 6) or []
        except Exception:
            hits = []
        for h in hits:
            ci = h.get('coverImage') or {}
            url = ci.get('extraLarge') or ci.get('large')
            if not url:
                continue
            ti = h.get('title') or {}
            out.append({
                'source': 'AniList',
                'name': ti.get('native') or ti.get('romaji'),
                'native': ti.get('romaji') or '',
                'year': str((h.get('startDate') or {}).get('year') or ''),
                'rating': round((h.get('averageScore') or 0) / 10.0, 1) if h.get('averageScore') else None,
                'image': url,
                'site': h.get('siteUrl') or '',
            })
        if out:
            break
    seen, uniq = set(), []
    for c in out:
        k = c['image']
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def local_images(show):
    dr = one('SELECT * FROM drives WHERE id=?', (show['drive_id'],))
    if not dr:
        return []
    root = dr['letter'] + ':/'
    online = os.path.exists(root)
    items = rows("SELECT path,name,size FROM entries WHERE show_id=? AND is_dir=0 AND "
                 "ext IN ('.jpg','.jpeg','.png','.webp') AND size>40000 "
                 "ORDER BY size DESC LIMIT 40", (show['id'],))
    out = []
    for it in items:
        w = h = 0
        if online:
            w, h = img_size(os.path.join(root, it['path'].replace('/', os.sep)))
            if not w:
                continue
        out.append({'path': it['path'], 'name': it['name'], 'size': it['size'],
                    'w': w, 'h': h,
                    'url': '/api/localimg?drive=%d&path=%s' % (dr['id'],
                                                               urllib.parse.quote(it['path']))})
    out.sort(key=lambda x: -((x['h'] or 0) / float(x['w'] or 1) if x['w'] else 0))
    return out[:24]


def save_cover(drive_id, show_id, url):
    if url.startswith('//'):
        url = 'https:' + url
    ext = '.jpg'
    m = re.search(r'\.(jpg|jpeg|png|webp)', url.lower())
    if m:
        ext = '.' + m.group(1).replace('jpeg', 'jpg')
    name = 'b%d_s%d%s' % (drive_id, show_id, ext)
    dst = os.path.join(COVERS, name)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://bgm.tv/'})
        with urllib.request.urlopen(req, timeout=15) as r:
            blob = r.read()
        if len(blob) < 1200:
            return None
        with open(dst, 'wb') as f:
            f.write(blob)
        return '/covers/' + name
    except Exception:
        return None


# ---------------------------------------------------------------- api helpers

def volume_guid_of(letter):
    """当前该盘符对应的卷 GUID，用于判断是不是同一块盘"""
    try:
        buf = ctypes.create_unicode_buffer(64)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(letter + ':\\', buf, 64):
            return buf.value
    except Exception:
        pass
    return ''


def drive_payload(d):
    letter = d.get('letter') or ''
    online = bool(letter) and os.path.exists(letter + ':/')
    if online and d.get('volume_guid'):
        cur = volume_guid_of(letter)
        if cur and cur.lower() != (d['volume_guid'] or '').lower():
            online = False  # 同一盘符换成了别的盘，旧快照按离线处理
    health = None
    if d.get('health_json'):
        try:
            health = json.loads(d['health_json'])
        except Exception:
            health = None
    agg = one('SELECT COUNT(*) c, COALESCE(SUM(size),0) s, COALESCE(SUM(file_count),0) f '
              'FROM shows WHERE drive_id=?', (d['id'],)) or {}
    return {
        'id': d['id'], 'letter': letter, 'label': d.get('label') or d.get('volume_label') or letter + ':',
        'volume_label': d.get('volume_label') or '', 'guid': d.get('volume_guid') or '',
        'model': d.get('model') or '', 'bus': d.get('bus') or '',
        'total_bytes': d.get('total_bytes') or 0, 'free_bytes': d.get('free_bytes') or 0,
        'last_scan': d.get('last_scan') or '', 'online': online,
        'health': health, 'show_count': agg.get('c', 0), 'show_bytes': agg.get('s', 0),
        'file_count': agg.get('f', 0),
    }


def show_payload(s, drive_total):
    pct = round(s['size'] * 100.0 / drive_total, 2) if drive_total else 0
    return {
        'id': s['id'], 'drive_id': s['drive_id'], 'path': s['path'], 'folder_name': s['folder_name'],
        'title': s['title'] or s['folder_name'], 'title_auto': s['title_auto'],
        'title_cn': s['title_cn'], 'fansub': s['fansub'], 'season': s['season'],
        'episodes': s['episodes'], 'source': s['source'], 'codec': s['codec'],
        'resolution': s['resolution'], 'bitdepth': s['bitdepth'], 'audio': s['audio'],
        'subtitle': s['subtitle'], 'container': s['container'], 'size': s['size'],
        'file_count': s['file_count'], 'video_count': s['video_count'],
        'cover': s['cover'], 'cover_src': s['cover_src'], 'bgm_id': s['bgm_id'],
        'bgm_score': s['bgm_score'], 'bgm_date': s['bgm_date'], 'bgm_name': s['bgm_name'],
        'manual': s['manual'], 'pct': pct,
    }


# ---------------------------------------------------------------- http handler

class Handler(BaseHTTPRequestHandler):
    server_version = 'AnimeVault/1.0'

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # -- utils
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def send_file(self, path, ctype=None, cache=True):
        if not os.path.isfile(path):
            self.send_json({'error': 'not found'}, 404)
            return
        ctype = ctype or {
            '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8', '.svg': 'image/svg+xml',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.webp': 'image/webp', '.ico': 'image/x-icon', '.json': 'application/json',
        }.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'public, max-age=86400' if cache else 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/' or path == '/index.html':
            return self.send_file(os.path.join(STATIC, 'index.html'), cache=False)
        if path.startswith('/static/'):
            return self.send_file(os.path.join(STATIC, path[len('/static/'):]))
        if path.startswith('/covers/'):
            return self.send_file(os.path.join(COVERS, os.path.basename(path)))
        if path == '/api/state':
            ds = rows('SELECT * FROM drives ORDER BY created_at DESC')
            return self.send_json({'drives': [drive_payload(d) for d in ds],
                                   'scan': SCAN, 'match': MATCH})
        if path == '/api/volumes':
            vols = list_volumes()
            known = {d['volume_guid']: d['id'] for d in rows('SELECT id,volume_guid FROM drives')}
            for v in vols:
                v['known_drive_id'] = known.get(v['guid'])
            return self.send_json({'volumes': vols})
        if path == '/api/overview':
            ds = rows('SELECT * FROM drives ORDER BY created_at DESC')
            drives = [drive_payload(d) for d in ds]
            dmap = {x['id']: x for x in drives}
            shows = rows('SELECT * FROM shows ORDER BY size DESC')
            all_bytes = sum(s['size'] or 0 for s in shows)
            for s_ in shows:
                dd = dmap.get(s_['drive_id']) or {}
                s_['drive_label'] = dd.get('label') or ''
                s_['drive_letter'] = dd.get('letter') or ''
                s_['drive_online'] = dd.get('online')
                s_['pct'] = round((s_['size'] or 0) * 100.0 / all_bytes, 2) if all_bytes else 0
            def group(key):
                m = {}
                for s_ in shows:
                    k = s_.get(key) or '未知'
                    m[k] = m.get(k, 0) + (s_['size'] or 0)
                return sorted(m.items(), key=lambda kv: -kv[1])
            return self.send_json({
                'drives': drives,
                'totals': {
                    'drives': len(drives),
                    'online': sum(1 for x in drives if x['online']),
                    'shows': len(shows),
                    'files': sum(x['file_count'] or 0 for x in shows),
                    'bytes': all_bytes,
                    'capacity': sum(x['total_bytes'] or 0 for x in drives),
                    'free': sum(x['free_bytes'] or 0 for x in drives),
                },
                'shows': shows,
                'top': shows[:18],
                'spec': {
                    'source': group('source'), 'codec': group('codec'),
                    'resolution': group('resolution'), 'subtitle': group('subtitle'),
                },
                'scan': SCAN, 'match': MATCH,
            })

        if path == '/api/drive':
            d = one('SELECT * FROM drives WHERE id=?', (qs.get('id', ['0'])[0],))
            if not d:
                return self.send_json({'error': 'no drive'}, 404)
            shows = rows('SELECT * FROM shows WHERE drive_id=? ORDER BY size DESC', (d['id'],))
            roots = rows('SELECT r.id, r.path, r.label, r.last_scan, r.created_at, '
                         '(SELECT COUNT(*) FROM shows s WHERE s.root_id=r.id) AS show_count, '
                         '(SELECT COALESCE(SUM(size),0) FROM shows s WHERE s.root_id=r.id) AS bytes '
                         'FROM roots r WHERE r.drive_id=? ORDER BY r.path', (d['id'],))
            for r_ in roots:
                r_['total_bytes'] = r_['bytes']
            dp = drive_payload(d)
            total = dp['show_bytes'] or dp['total_bytes']
            payload = [show_payload(s, total) for s in shows]
            ext = rows('SELECT ext, SUM(size) s, COUNT(*) c FROM entries WHERE drive_id=? AND is_dir=0 '
                       'GROUP BY ext ORDER BY s DESC LIMIT 12', (d['id'],))
            return self.send_json({'drive': dp, 'shows': payload, 'ext': ext,
                                   'roots': roots, 'scan': SCAN, 'match': MATCH})
        if path == '/api/show':
            s = one('SELECT * FROM shows WHERE id=?', (qs.get('id', ['0'])[0],))
            if not s:
                return self.send_json({'error': 'no show'}, 404)
            d = one('SELECT * FROM drives WHERE id=?', (s['drive_id'],))
            dp = drive_payload(d)
            total = dp['show_bytes'] or 1
            files = rows('SELECT name,path,size,ext,is_dir FROM entries WHERE show_id=? AND is_dir=0 '
                         'ORDER BY size DESC LIMIT 600', (s['id'],))
            dirs = rows('SELECT name,path,size,file_count FROM entries WHERE drive_id=? AND '
                        'parent_path=? AND is_dir=1 ORDER BY size DESC', (s['drive_id'], s['path']))
            return self.send_json({'show': show_payload(s, total), 'drive': dp,
                                   'files': [dict(f, pct=round(f['size'] * 100.0 / (s['size'] or 1), 2))
                                             for f in files],
                                   'dirs': dirs})
        if path == '/api/show/candidates':
            sid = int(qs.get('show_id', ['0'])[0])
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s:
                return self.send_json({'error': 'no show'}, 404)
            try:
                cands = gather_candidates(s)
            except Exception:
                cands = []
            return self.send_json({'candidates': cands, 'local': local_images(s),
                                   'romaji': romaji_candidates(s)})
        if path == '/api/localimg':
            did = int(qs.get('drive', ['0'])[0])
            rel = qs.get('path', [''])[0]
            d = one('SELECT * FROM drives WHERE id=?', (did,))
            if not d:
                return self.send_json({'error': 'no drive'}, 404)
            root = (d['letter'] + ':/').lower()
            full = os.path.normpath(os.path.join(d['letter'] + ':/', rel.replace('/', os.sep)))
            if not full.lower().startswith(root) or \
                    os.path.splitext(full)[1].lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                return self.send_json({'error': 'bad path'}, 400)
            return self.send_file(full, cache=True)
        if path == '/api/img':
            u = qs.get('u', [''])[0]
            if not u.startswith('http'):
                return self.send_json({'error': 'bad url'}, 400)
            try:
                req = urllib.request.Request(u, headers={'User-Agent': UA,
                                                         'Referer': 'https://bgm.tv/'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    blob = r.read()
                    ctype = r.headers.get('Content-Type', 'image/jpeg')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(blob)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(blob)
            except Exception:
                self.send_json({'error': 'fetch failed'}, 502)
            return
        if path == '/api/entries':
            did = int(qs.get('drive', ['0'])[0])
            sub = qs.get('path', [''])[0]
            items = rows('SELECT name,path,is_dir,size,file_count,dir_count,ext FROM entries '
                         'WHERE drive_id=? AND parent_path=? ORDER BY is_dir DESC, size DESC',
                         (did, sub))
            parent = one('SELECT * FROM entries WHERE drive_id=? AND path=?', (did, sub))
            return self.send_json({'path': sub, 'items': items,
                                   'crumb': [c for c in sub.split('/') if c]})
        if path == '/api/search':
            q = (qs.get('q', [''])[0] or '').strip()
            did = qs.get('drive', ['0'])[0]
            if not q:
                return self.send_json({'items': [], 'total': 0})
            like = '%' + q.replace('%', '').replace('_', '') + '%'
            sql = ('SELECT e.name,e.path,e.is_dir,e.size,e.ext,e.drive_id,e.show_id,'
                   's.title show_title,s.cover,s.fansub FROM entries e LEFT JOIN shows s ON s.id=e.show_id '
                   'WHERE e.name LIKE ? COLLATE NOCASE')
            args = [like]
            if did and did != '0':
                sql += ' AND e.drive_id=?'
                args.append(int(did))
            sql += ' ORDER BY e.is_dir DESC, e.size DESC LIMIT 400'
            items = rows(sql, args)
            total = one('SELECT COUNT(*) c FROM entries WHERE name LIKE ? COLLATE NOCASE',
                        (like,))['c']
            drives = {d['id']: d for d in rows('SELECT id,label,letter FROM drives')}
            for it in items:
                dd = drives.get(it['drive_id']) or {}
                it['drive_label'] = dd.get('label') or dd.get('letter')
            return self.send_json({'items': items, 'total': total})
        if path == '/api/health':
            letter = (qs.get('letter', [''])[0] or '?').upper()[:1]
            did = int(qs.get('drive', ['0'])[0] or 0)
            h = get_health(letter)
            if did and h.get('available'):
                c = db()
                c.execute('UPDATE drives SET health_json=?, last_health=? WHERE id=?',
                          (json.dumps(h, ensure_ascii=False), now_str(), did))
                c.commit()
                c.close()
            return self.send_json({'health': h})
        if path == '/api/tasks':
            return self.send_json({'scan': SCAN, 'match': MATCH})
        return self.send_json({'error': 'unknown endpoint'}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self.body_json()

        if path == '/api/scan':
            letter = (body.get('letter') or '').upper()[:1]
            label = (body.get('label') or '').strip()
            sub = (body.get('path') or '').strip().replace('\\', '/').strip('/')
            if SCAN.get('active'):
                return self.send_json({'error': '已有扫描任务在进行'}, 409)
            vols = list_volumes()
            vol = next((v for v in vols if v['letter'] == letter), None)
            if not vol:
                return self.send_json({'error': '未找到 %s: 盘' % letter}, 400)
            scan_dir = letter + ':/' + sub
            if not os.path.isdir(scan_dir):
                return self.send_json({'error': '文件夹不存在：%s' % scan_dir}, 400)
            c = db()
            d = c.execute('SELECT * FROM drives WHERE volume_guid=?', (vol['guid'],)).fetchone()
            if d:
                did = d['id']
                c.execute('UPDATE drives SET letter=?, volume_label=?, total_bytes=?, free_bytes=?,'
                          ' label=COALESCE(NULLIF(?,\'\'),label) WHERE id=?',
                          (letter, vol['volume_label'], vol['total_bytes'], vol['free_bytes'],
                           label, did))
            else:
                did = c.execute('INSERT INTO drives (letter,label,volume_label,volume_guid,total_bytes,'
                                'free_bytes,created_at) VALUES (?,?,?,?,?,?,?)',
                                (letter, label or (vol['volume_label'] or (letter + ': 盘')),
                                 vol['volume_label'], vol['guid'], vol['total_bytes'],
                                 vol['free_bytes'], now_str())).lastrowid
            rrow = c.execute('SELECT id FROM roots WHERE drive_id=? AND path=?', (did, sub)).fetchone()
            if rrow:
                rid = rrow['id']
            else:
                rid = c.execute('INSERT INTO roots (drive_id,path,label,created_at) VALUES (?,?,?,?)',
                                (did, sub, sub or '整个硬盘', now_str())).lastrowid
            c.commit()
            c.close()
            SCAN.update(active=True, drive_id=did, root_id=rid, letter=letter, scan_path=sub,
                        phase='starting', path='', files=0, dirs=0, bytes=0,
                        started=time.time(), message='准备扫描…')
            threading.Thread(target=scan_thread,
                             args=(did, rid, scan_dir, sub, letter), daemon=True).start()
            return self.send_json({'ok': True, 'drive_id': did, 'root_id': rid})

        if path == '/api/drive/rescan':
            did = int(body.get('drive_id') or 0)
            if SCAN.get('active'):
                return self.send_json({'error': '已有扫描任务在进行'}, 409)
            d = one('SELECT * FROM drives WHERE id=?', (did,))
            if not d:
                return self.send_json({'error': 'no drive'}, 404)
            if not os.path.exists(d['letter'] + ':/'):
                return self.send_json({'error': '该硬盘当前未连接'}, 400)
            rs = [dict(r) for r in rows('SELECT * FROM roots WHERE drive_id=? ORDER BY path', (did,))]
            if not rs:
                return self.send_json({'error': '没有扫描目录'}, 400)
            SCAN.update(active=True, drive_id=did, letter=d['letter'], phase='starting',
                        path='', files=0, dirs=0, bytes=0, started=time.time(),
                        message='准备扫描…', queue=len(rs), queue_idx=0)
            threading.Thread(target=rescan_worker, args=(did, rs, d['letter']), daemon=True).start()
            return self.send_json({'ok': True, 'roots': len(rs)})

        if path == '/api/dirs':
            letter = (body.get('letter') or '').upper()[:1]
            sub = (body.get('path') or '').strip().replace('\\', '/').strip('/')
            base = letter + ':/' + sub
            if not os.path.isdir(base):
                return self.send_json({'error': '路径不存在'}, 400)
            items = []
            try:
                for e in os.scandir(base):
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if e.name.lower() in SKIP_DIRS:
                        continue
                    n = 0
                    try:
                        with os.scandir(e.path) as it:
                            n = sum(1 for x in it)
                    except OSError:
                        pass
                    items.append({'name': e.name,
                                  'path': (sub + '/' + e.name) if sub else e.name,
                                  'kids': n})
            except OSError as ex:
                return self.send_json({'error': str(ex)}, 400)
            items.sort(key=lambda x: x['name'].lower())
            return self.send_json({'path': sub, 'items': items,
                                   'crumb': [c for c in sub.split('/') if c],
                                   'letter': letter})

        if path == '/api/health/refresh':
            did = int(body.get('drive_id') or 0)
            d = one('SELECT * FROM drives WHERE id=?', (did,))
            if not d:
                return self.send_json({'error': 'no drive'}, 404)
            h = get_health(d['letter'])
            if h.get('available'):
                c = db()
                c.execute('UPDATE drives SET health_json=?, last_health=? WHERE id=?',
                          (json.dumps(h, ensure_ascii=False), now_str(), did))
                c.commit()
                c.close()
            return self.send_json({'health': h})

        if path == '/api/drive/label':
            did = int(body.get('drive_id') or 0)
            c = db()
            c.execute('UPDATE drives SET label=? WHERE id=?', ((body.get('label') or '').strip(), did))
            c.commit()
            c.close()
            return self.send_json({'ok': True})

        if path == '/api/drive/delete':
            did = int(body.get('drive_id') or 0)
            c = db()
            c.execute('DELETE FROM entries WHERE drive_id=?', (did,))
            c.execute('DELETE FROM shows WHERE drive_id=?', (did,))
            c.execute('DELETE FROM roots WHERE drive_id=?', (did,))
            c.execute('DELETE FROM drives WHERE id=?', (did,))
            c.commit()
            c.close()
            return self.send_json({'ok': True})

        if path == '/api/show/update':
            sid = int(body.get('show_id') or 0)
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s:
                return self.send_json({'error': 'no show'}, 404)
            fields, args = [], []
            for k in ('title', 'title_cn', 'fansub'):
                if k in body:
                    fields.append('%s=?' % k)
                    args.append(body[k])
            if 'cover_url' in body and body['cover_url']:
                url = body['cover_url']
                if url.startswith('/covers/') or url.startswith('http'):
                    fields.append('cover=?')
                    args.append(url)
                    fields.append('cover_src=?')
                    args.append('manual')
            if body.get('reset_cover'):
                fields.append('cover=NULL, cover_src=NULL')
            if fields:
                fields.append('manual=1')
                args.append(sid)
                c = db()
                c.execute('UPDATE shows SET %s WHERE id=?' % ','.join(fields), args)
                c.commit()
                c.close()
            return self.send_json({'ok': True})

        if path == '/api/show/cover':
            # 上传 base64 图片作为封面
            sid = int(body.get('show_id') or 0)
            b64 = body.get('data') or ''
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s or ',' not in b64:
                return self.send_json({'error': 'bad request'}, 400)
            import base64
            head, payload = b64.split(',', 1)
            ext = '.png' if 'png' in head else ('.webp' if 'webp' in head else '.jpg')
            name = 'm%d_s%d%s' % (s['drive_id'], sid, ext)
            with open(os.path.join(COVERS, name), 'wb') as f:
                f.write(base64.b64decode(payload))
            c = db()
            c.execute('UPDATE shows SET cover=?, cover_src=\'manual\', manual=1 WHERE id=?',
                      ('/covers/' + name, sid))
            c.commit()
            c.close()
            return self.send_json({'ok': True, 'cover': '/covers/' + name})

        if path == '/api/cover/fetch':
            # 从 URL 下载封面
            sid = int(body.get('show_id') or 0)
            url = (body.get('url') or '').strip()
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s or not url.startswith('http'):
                return self.send_json({'error': 'bad url'}, 400)
            local = save_cover(s['drive_id'], sid, url)
            if local:
                c = db()
                c.execute('UPDATE shows SET cover=?, cover_src=\'manual\', manual=1 WHERE id=?',
                          (local, sid))
                c.commit()
                c.close()
                return self.send_json({'ok': True, 'cover': local})
            return self.send_json({'error': '下载失败（网络或图片地址无效）'}, 400)

        if path == '/api/show/pick':
            sid = int(body.get('show_id') or 0)
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s:
                return self.send_json({'error': 'no show'}, 404)
            url = (body.get('url') or '').strip()
            rel = (body.get('path') or '').strip()
            local = None
            if url.startswith('http'):
                local = save_cover(s['drive_id'], sid, url)
                if not local:
                    return self.send_json({'error': '图片下载失败'}, 502)
            elif rel:
                d = one('SELECT * FROM drives WHERE id=?', (s['drive_id'],))
                root = d['letter'] + ':/'
                if not os.path.exists(root):
                    return self.send_json({'error': '该硬盘当前未连接'}, 400)
                full = os.path.normpath(os.path.join(root, rel.replace('/', os.sep)))
                if not full.lower().startswith(root.lower()) or not os.path.isfile(full):
                    return self.send_json({'error': '图片不存在'}, 400)
                ext = os.path.splitext(full)[1].lower() or '.jpg'
                name = 'p%d_s%d%s' % (s['drive_id'], sid, ext)
                try:
                    shutil.copyfile(full, os.path.join(COVERS, name))
                except Exception as ex:
                    return self.send_json({'error': str(ex)}, 500)
                local = '/covers/' + name
            if not local:
                return self.send_json({'error': '缺少图片地址'}, 400)
            c = db()
            c.execute('UPDATE shows SET cover=?, cover_src=?, manual=1 WHERE id=?',
                      (local, 'manual', sid))
            c.commit()
            c.close()
            return self.send_json({'ok': True, 'cover': local})

        if path == '/api/show/rematch':
            sid = int(body.get('show_id') or 0)
            s = one('SELECT * FROM shows WHERE id=?', (sid,))
            if not s:
                return self.send_json({'error': 'no show'}, 404)
            try:
                res = match_one(s)
            except Exception as ex:
                return self.send_json({'error': '联网失败：%s' % ex}, 502)
            if not res.get('ok'):
                return self.send_json({'error': res.get('reason') or '未找到匹配'})
            return self.send_json(res)

        if path == '/api/match':
            did = int(body.get('drive_id') or 0)
            if MATCH.get('active'):
                return self.send_json({'error': '匹配任务进行中'}, 409)
            scope = body.get('scope') or 'all'
            if body.get('force'):
                c = db()
                c.execute('UPDATE shows SET bgm_tried=0 WHERE drive_id=? AND manual=0', (did,))
                c.commit()
                c.close()
            threading.Thread(target=match_worker, args=(did, scope), daemon=True).start()
            return self.send_json({'ok': True})

        if path == '/api/open':
            p = (body.get('path') or '').strip()
            d = one('SELECT * FROM drives WHERE id=?', (int(body.get('drive_id') or 0),))
            if not d:
                return self.send_json({'error': 'no drive'}, 400)
            root = d['letter'] + ':/'
            if not os.path.exists(root):
                return self.send_json({'error': '该硬盘当前未连接'}, 400)
            target = os.path.normpath(os.path.join(root, p.replace('/', os.sep)))
            if not os.path.exists(target):
                target = root
            try:
                os.startfile(target)
                return self.send_json({'ok': True})
            except Exception as ex:
                return self.send_json({'error': str(ex)}, 500)

        return self.send_json({'error': 'unknown endpoint'}, 404)


class VaultServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会让两个进程同时绑定同一端口，必须关掉
    allow_reuse_address = False
    daemon_threads = True


def cleanup_covers():
    """删除重扫后遗留的孤立封面文件"""
    try:
        used = {os.path.basename(r['cover']) for r in
                rows("SELECT cover FROM shows WHERE cover IS NOT NULL AND cover LIKE '/covers/%'")}
        now = time.time()
        n = 0
        for fn in os.listdir(COVERS):
            fp = os.path.join(COVERS, fn)
            if fn in used or not os.path.isfile(fp):
                continue
            if now - os.path.getmtime(fp) < 3600:   # 刚生成的先留着
                continue
            try:
                os.remove(fp)
                n += 1
            except Exception:
                pass
        if n:
            print('[cleanup] 清理孤立封面 %d 个' % n)
    except Exception:
        pass


def main():
    init_db()
    threading.Thread(target=cleanup_covers, daemon=True).start()
    port = PORT
    srv = None
    for p in range(port, port + 12):
        try:
            srv = VaultServer(('127.0.0.1', p), Handler)
            port = p
            break
        except OSError:
            continue
    if not srv:
        print('端口被占用，无法启动')
        sys.exit(1)
    print('=' * 56)
    print('  ANIME VAULT  番剧硬盘档案馆')
    print('  http://127.0.0.1:%d' % port)
    print('  数据目录: %s' % DATA)
    print('=' * 56)
    try:
        import webbrowser
        if os.environ.get('ANIME_VAULT_NOBROWSER') != '1':
            threading.Timer(1.0, lambda: webbrowser.open('http://127.0.0.1:%d' % port)).start()
    except Exception:
        pass
    srv.serve_forever()


if __name__ == '__main__':
    main()
