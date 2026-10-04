# -*- coding: utf-8 -*-
"""
SE Localization Tool — GUI для локализации модов Space Engineers.
Требует: pip install customtkinter
"""
import os, re, csv, sys, json, shutil, threading, queue, urllib.request, urllib.parse
import unicodedata
import tkinter as tk
from datetime import datetime

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox, Menu, simpledialog
except ImportError:
    print("=== Нужна библиотека ===")
    print("Открой PowerShell и запусти:")
    print("   pip install customtkinter")
    input("\nEnter для выхода...")
    sys.exit(1)

# ===================== ЛОГИКА =====================

TAGS_SBC = ['DisplayName', 'Description', 'Tooltip', 'PublicName']
TAG_RE = re.compile(r'<(' + '|'.join(TAGS_SBC) + r')(\s[^>]*)?>([^<]*)</\1>', re.IGNORECASE)
KEY_RE = re.compile(r'^(DisplayName|Description|Tooltip|PublicName)_', re.IGNORECASE)
GRID_RE = re.compile(r'^(Large|Small|Static) Grid \d+$', re.IGNORECASE)
PATH_RE = re.compile(r'\.(dds|png|mwm|xml|resx|sbc)\b', re.IGNORECASE)
PREFIX_RE = re.compile(r'^(TXVE|AS2|MES|Symm|NWU|MES-Prefab|GE_|PE_|ASA_)[-_]?', re.IGNORECASE)
SNAKE_RE = re.compile(r'^[a-z][a-z0-9_]*$')
DATA_RESX_RE = re.compile(
    r'<data\s+name="([^"]+)"[^>]*>\s*<value[^>]*>(.*?)</value>\s*</data>', re.DOTALL)
SKIP_RESX_KEYS = {'Name1', 'Icon1', 'Bitmap1', 'Color1'}
CYR = re.compile(r'[а-яА-ЯёЁ]')

_TSV_LOCK = threading.Lock()
_TSV_HEADER = ['mod_id', 'path', 'line', 'col',
               'kind', 'key', 'original', 'translated']


def _sanitize_field(s):
    if s is None:
        return ''
    return (s.replace('\r\n', ' ').replace('\r', ' ')
             .replace('\n', ' ').replace('\t', ' '))


def _normalize_key_part(s):
    if s is None:
        return ''
    s = str(s).replace('\ufeff', '')
    try:
        s = unicodedata.normalize('NFC', s)
    except Exception:
        pass
    s = s.replace('\u00a0', ' ').replace('\u2009', ' ').replace('\u202f', ' ')
    s = ' '.join(s.split())
    return s


def sbc_skip(s):
    if not s: return True
    if KEY_RE.match(s): return True
    if s.startswith('{') or s.startswith('['): return True
    if 'ERROR:' in s or 'WARNING:' in s: return True
    if GRID_RE.match(s): return True
    if PATH_RE.search(s): return True
    if 'dummy' in s.lower(): return True
    if s.lower() in ('test', 'ff', 'f'): return True
    if PREFIX_RE.match(s): return True
    if SNAKE_RE.match(s): return True
    return False


def line_col(text, pos):
    return text.count('\n', 0, pos) + 1, pos - text.rfind('\n', 0, pos)


def read_file(full):
    try:
        raw = open(full, 'rb').read()
    except OSError:
        return None
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _extract_tag_parts(s):
    m = re.match(r'<(\w+)(\s[^>]*)?>([^<]*)</\1>$', s.strip(), re.DOTALL)
    if not m:
        return None
    return m.group(1), (m.group(2) or ''), m.group(3)


def _normalize_inner(s):
    return ' '.join(s.replace('"', '').split())


def _inner_text(trans):
    t = trans.strip()
    parts = _extract_tag_parts(t)
    if parts:
        return parts[2]
    return t


def _strip_translated_wrapper_global(orig, translated):
    t = translated.strip() if translated else ''
    if not t or not t.startswith('<'):
        return translated
    orig_parts = _extract_tag_parts(orig.strip() if orig else '')
    if not orig_parts:
        return translated
    orig_tag = orig_parts[0]
    pat = re.compile(
        r'<' + re.escape(orig_tag) + r'(\s[^>]*)?>(.*?)</' + re.escape(orig_tag) + r'>',
        re.IGNORECASE | re.DOTALL)
    m = pat.match(t)
    if not m:
        return translated
    inner = m.group(2)
    if '<' in inner or '>' in inner:
        return translated
    return inner


def scan_mod(mod_path, log_cb=None):
    rows = []
    for dirpath, _, files in os.walk(mod_path):
        for fn in files:
            low, full = fn.lower(), os.path.join(dirpath, fn)
            rel = os.path.relpath(full, mod_path)
            if low.endswith('.sbc'):
                text = read_file(full)
                if not text: continue
                for m in TAG_RE.finditer(text):
                    c = m.group(3).strip()
                    if sbc_skip(c): continue
                    ln, col = line_col(text, m.start())
                    rows.append(('sbc', rel, ln, col, '', m.group(0)))
            elif low.endswith('.resx'):
                if low.startswith('mytexts.') and low != 'mytexts.resx':
                    continue
                text = read_file(full)
                if not text: continue
                for m in DATA_RESX_RE.finditer(text):
                    k, v = m.group(1), m.group(2).replace('\t', '    ')
                    if not v.strip() or k in SKIP_RESX_KEYS: continue
                    if any(x in v for x in ('resheader', 'xmlns:xsd', 'xsd:schema')): continue
                    ln, col = line_col(text, m.start(2))
                    rows.append(('resx', rel, ln, col, k, v))
    return rows


def _is_valid_tsv(path, log_cb=None):
    try:
        with _TSV_LOCK:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                r = csv.reader(f, delimiter='\t')
                first = next(r, None)
                if not first:
                    return False
                first = [c.replace('\ufeff', '').strip() for c in first[:8]]
                if first != _TSV_HEADER:
                    return False
                for n, row in enumerate(r, 2):
                    if not row:
                        continue
                    if len(row) != 8:
                        if log_cb:
                            log_cb('  TSV-проверка: строка {} содержит {} колонок вместо 8'.format(
                                n, len(row)))
                        return False
                    if not row[0] or not row[1] or not row[2].isdigit() or not row[3].isdigit():
                        return False
                    if row[4].lower() not in ('sbc', 'resx'):
                        return False
                return True
    except (OSError, UnicodeError, csv.Error):
        return False


def _write_tsv(rows, out):
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                       lineterminator='\n')
        w.writerow(_TSV_HEADER)
        for row in rows:
            w.writerow([_sanitize_field(c) for c in row])


def _parse_plain_translation_text(text, log_cb=None):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    while text.startswith('\ufeff'):
        text = text[1:]
    rec_start = re.compile(
        r'^(\S+)\s+([^\t\n]+?\.(?:sbc|resx))\s+(\d+)\s+(\d+)\s+',
        re.IGNORECASE | re.MULTILINE)
    matches = list(rec_start.finditer(text))
    records = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        records.append(text[m.start():end].strip())
    if log_cb:
        log_cb('  Обычный текст: найдено записей {}'.format(len(records)))
    rows = []
    bad = 0
    for rec in records:
        m = rec_start.match(rec)
        if not m:
            bad += 1
            continue
        mod_id, path, ln, col = m.groups()
        rest = rec[m.end():].strip()
        kind = 'resx' if path.lower().endswith('.resx') else 'sbc'
        key = ''
        orig = ''
        trans = ''
        kind_prefix = re.match(r'^(sbc|resx)\s+', rest, re.IGNORECASE)
        if kind_prefix:
            rest = rest[kind_prefix.end():].strip()
        if kind == 'resx':
            km = re.match(r'([^\s]+)\s+(.*)$', rest, re.DOTALL)
            if not km:
                bad += 1
                continue
            key, body = km.group(1), km.group(2).strip()
            cm = CYR.search(body)
            if cm:
                orig = body[:cm.start()].rstrip()
                trans = body[cm.start():].rstrip()
            else:
                orig = body
                trans = ''
        else:
            bm = re.search(r'(</\w+>)\s*(<\w+>)', rest, re.DOTALL)
            if bm:
                orig = rest[:bm.end(1)].rstrip()
                trans = rest[bm.start(2):].rstrip()
            else:
                orig = rest
                trans = ''
        if orig.startswith('"') and orig.endswith('"') and len(orig) >= 2:
            orig = orig[1:-1]
        if trans.startswith('"') and trans.endswith('"') and len(trans) >= 2:
            trans = trans[1:-1]
        orig = _sanitize_field(orig)
        trans = _sanitize_field(trans)
        trans = _strip_translated_wrapper_global(orig, trans)
        rows.append([mod_id, path, ln, col, kind, key, orig.strip(), trans.strip()])
    return rows, bad


def fix_tsv(inp, out, log_cb=None):
    if not os.path.isfile(inp):
        raise FileNotFoundError('Файл не найден: {}'.format(inp))
    if _is_valid_tsv(inp, log_cb=log_cb):
        if log_cb:
            log_cb('  Формат: корректный TSV — повторный разбор не требуется')
        rows = []
        repaired = 0
        with open(inp, 'r', encoding='utf-8-sig', newline='') as f:
            r = csv.reader(f, delimiter='\t')
            next(r, None)
            for row in r:
                if not row:
                    continue
                row = [_sanitize_field(c.replace('\ufeff', '')) for c in row[:8]]
                orig_val = row[7]
                row[7] = _strip_translated_wrapper_global(row[6], row[7])
                if row[7] != orig_val:
                    repaired += 1
                rows.append(row)
        _write_tsv(rows, out)
        filled = sum(1 for row in rows if row[7].strip())
        if log_cb:
            msg = '  TSV принят: {} строк, {} заполнено'.format(len(rows), filled)
            if repaired:
                msg += ', восстановлено {}'.format(repaired)
            log_cb(msg)
        return len(rows), 0
    try:
        with open(inp, 'r', encoding='utf-8-sig', newline='') as f:
            text = f.read()
    except UnicodeDecodeError as e:
        raise ValueError('Файл не является UTF-8 текстом/TSV: {}'.format(e))
    rows, bad = _parse_plain_translation_text(text, log_cb=log_cb)
    if not rows:
        raise ValueError(
            'Не удалось распознать файл как TSV или поддерживаемый обычный текстовый формат.\n'
            'Ожидается TSV с заголовком mod_id/path/line/col/kind/key/original/translated '
            'или текстовый экспорт с первыми полями: mod_id path line col.')
    _write_tsv(rows, out)
    filled = sum(1 for row in rows if row[7].strip())
    if log_cb:
        log_cb('  Текст преобразован в TSV: {} строк, {} заполнено, {} битых'.format(
            len(rows), filled, bad))
    return len(rows), bad


def collect_by_file(tsv_path, only_mod=None):
    by_file = {}
    with _TSV_LOCK:
        with open(tsv_path, 'r', encoding='utf-8', newline='') as f:
            r = csv.reader(f, delimiter='\t')
            next(r, None)
            for row in r:
                if len(row) < 8: continue
                mod_id, path, ln, col, kind, key, orig, trans = [
                    _sanitize_field(c) for c in row[:8]]
                if only_mod and mod_id != only_mod:
                    continue
                if not trans.strip() or orig == trans: continue
                try:
                    li, ci = int(ln), int(col)
                except ValueError:
                    continue
                by_file.setdefault((mod_id, path), []).append(
                    (li, ci, kind, key, orig, trans))
    return by_file


def inject_sbc_position(text, ln, col, orig, trans):
    """
    Позиционная замена SBC.
    Возвращает (text, status):
      'applied' — вставили,
      'already' — уже на месте,
      'missing' — не совпало.
    СОХРАНЯЕТ XML-теги: если orig — тег, а trans без тега, оборачивает в тот же тег.
    """
    lines = text.splitlines(True)
    if ln - 1 >= len(lines):
        return text, 'missing'
    line = lines[ln - 1]
    off = col - 1
    if off < 0 or off > len(line):
        return text, 'missing'

    o = orig.strip()
    t = trans.strip()

    # 1) Полное совпадение с orig на позиции.
    if line[off:off + len(orig)] == orig:
        parts = _extract_tag_parts(o)
        if parts and not t.startswith('<'):
            tag, attrs, _inner = parts
            new_val = '<' + tag + attrs + '>' + t + '</' + tag + '>'
        else:
            new_val = trans
        new_line = line[:off] + new_val + line[off + len(orig):]
        lines[ln - 1] = new_line
        return ''.join(lines), 'applied'

    # 2) На позиции уже стоит наш перевод.
    if line[off:off + len(t)] == t:
        return text, 'already'

    parts = _extract_tag_parts(o)
    if parts:
        tag, attrs, inner_orig = parts
        expected_open = '<' + tag + attrs + '>'
        expected_close = '</' + tag + '>'
        if line[off:off + len(expected_open)] == expected_open:
            inner_start = off + len(expected_open)
            t_inner = _inner_text(trans)
            if line[inner_start:inner_start + len(t_inner)] == t_inner:
                close_pos = inner_start + len(t_inner)
                if line[close_pos:close_pos + len(expected_close)] == expected_close:
                    return text, 'already'

    return text, 'missing'


def inject_sbc_content(text, orig, trans):
    o = orig.strip()
    t = trans.strip()
    parts = _extract_tag_parts(o)
    if not parts:
        o_norm = o.replace('\r\n', '\n')
        if o_norm and o_norm in text:
            return text.replace(o_norm, t, 1), True
        return text, False
    tag, attrs, inner_orig = parts
    norm_orig = _normalize_inner(inner_orig)
    t_parts = _extract_tag_parts(t)
    if t_parts:
        new_tag = t
    else:
        new_tag = '<' + tag + attrs + '>' + t + '</' + tag + '>'
    pat = re.compile(r'<' + tag + r'(\s[^>]*)?>(.*?)</' + tag + r'>', re.DOTALL)
    for mm in pat.finditer(text):
        if _normalize_inner(mm.group(2)) == norm_orig:
            return text[:mm.start()] + new_tag + text[mm.end():], True
    return text, False


def inject_resx(text, key, orig, trans):
    p1 = re.compile(r'(<data\s+name="' + re.escape(key) + r'"[^>]*>\s*<value[^>]*>)' + re.escape(orig) + r'(</value>)', re.DOTALL)
    if p1.search(text):
        return p1.sub(lambda m: m.group(1) + trans + m.group(2), text, count=1), True
    p2 = re.compile(r'(<data\s+name="' + re.escape(key) + r'"[^>]*>\s*<value[^>]*>)(.*?)(</value>)', re.DOTALL)
    if p2.search(text):
        return p2.sub(lambda m: m.group(1) + trans + m.group(3), text, count=1), True
    return text, False


def inject_all(mods_root, tsv, log_cb=None, only_mod=None):
    by_file = collect_by_file(tsv, only_mod=only_mod)
    ok = skip = already = files_changed = 0
    total = len(by_file)
    for idx, ((mod_id, rel), items) in enumerate(by_file.items(), 1):
        full = os.path.join(mods_root, mod_id, rel)
        if not os.path.isfile(full):
            if log_cb: log_cb('  MISSING: {}'.format(rel))
            continue
        raw = open(full, 'rb').read()
        bom = b''
        if raw.startswith(b'\xef\xbb\xbf'):
            bom, raw = raw[:3], raw[3:]
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        modified = False
        pos = sorted([x for x in items if x[2] == 'sbc'],
                     key=lambda x: (x[0], x[1]), reverse=True)
        resx = [x for x in items if x[2] == 'resx']
        for ln, col, k, key, orig, trans in pos:
            text2, status = inject_sbc_position(text, ln, col, orig, trans)
            if status == 'applied':
                text = text2
                ok += 1
                modified = True
                continue
            if status == 'already':
                already += 1
                continue
            text2, done = inject_sbc_content(text, orig, trans)
            if done:
                text = text2
                ok += 1
                modified = True
            else:
                skip += 1
        for ln, col, k, key, orig, trans in resx:
            text, done = inject_resx(text, key, orig, trans)
            if done:
                ok += 1
                modified = True
            else:
                skip += 1
        if modified:
            open(full, 'wb').write(bom + text.encode('utf-8'))
            files_changed += 1
        if log_cb and idx % 10 == 0:
            log_cb('  ...{}/{}'.format(idx, total))
    return ok, skip, already, files_changed


def backup_all(mods_root, tsv, log_cb=None, only_mod=None):
    by_file = collect_by_file(tsv, only_mod=only_mod)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    done = 0
    for (mod_id, rel) in by_file:
        full = os.path.join(mods_root, mod_id, rel)
        if os.path.isfile(full):
            shutil.copy2(full, full + '.bak_' + stamp)
            done += 1
    return done


def diagnose_row(text, kind, key, orig, trans, ln=None, col=None):
    if not trans.strip():
        return 'empty', None
    if kind == 'resx':
        pattern = re.compile(
            r'<data\s+name="' + re.escape(key) +
            r'"[^>]*>\s*<value[^>]*>(.*?)</value>', re.DOTALL)
        m = pattern.search(text)
        if not m:
            return 'failed', None
        current = m.group(1)
        if current == trans:
            return 'applied', current
        if current == orig:
            return 'failed', current
        return 'already', current
    o = orig.strip()
    t = trans.strip()
    parts = _extract_tag_parts(o)
    if not parts:
        if t and t in text:
            return 'applied', t
        if orig in text:
            return 'failed', orig
        return 'already', None
    tag, attrs, inner_orig = parts
    if ln is not None and col is not None:
        lines = text.splitlines(True)
        if 1 <= ln <= len(lines):
            line = lines[ln - 1]
            off = col - 1
            if 0 <= off <= len(line):
                if line[off:off + len(orig)] == orig:
                    return 'failed', inner_orig
                if line[off:off + len(t)] == t:
                    return 'applied', t
                expected_open = '<' + tag + attrs + '>'
                expected_close = '</' + tag + '>'
                if line[off:off + len(expected_open)] == expected_open:
                    inner_start = off + len(expected_open)
                    close_idx = line.find(expected_close, inner_start)
                    if close_idx >= 0:
                        current_inner = line[inner_start:close_idx]
                        t_inner = _inner_text(trans)
                        if current_inner == t_inner:
                            return 'applied', current_inner
                        if current_inner == inner_orig:
                            return 'failed', current_inner
                        return 'already', current_inner
    norm_orig = _normalize_inner(inner_orig)
    t_inner = _inner_text(trans)
    norm_trans = _normalize_inner(t_inner)
    pat = re.compile(r'<' + tag + r'(\s[^>]*)?>(.*?)</' + tag + r'>', re.DOTALL)
    first_other = None
    for mm in pat.finditer(text):
        inner_norm = _normalize_inner(mm.group(2))
        if inner_norm == norm_trans:
            return 'applied', mm.group(2)
        if inner_norm == norm_orig:
            return 'failed', mm.group(2)
        if first_other is None:
            first_other = mm.group(2)
    return 'already', first_other


def diag_all(mods_root, tsv, log_cb=None, only_mod=None):
    by_file = collect_by_file(tsv, only_mod=only_mod)
    results = {}
    current_values = {}
    for (mod_id, rel), items in by_file.items():
        full = os.path.join(mods_root, mod_id, rel)
        text = None
        if os.path.isfile(full):
            text = read_file(full) or ''
        for ln, col, kind, key, orig, trans in items:
            row_id = (mod_id, rel, str(ln), str(col), kind, key, orig)
            if text is None:
                results[row_id] = 'failed'
                continue
            st, cv = diagnose_row(text, kind, key, orig, trans, ln, col)
            results[row_id] = st
            if cv is not None:
                current_values[row_id] = cv
    return results, current_values


def fetch_mod_names(mod_ids):
    data = urllib.parse.urlencode({'itemcount': len(mod_ids),
        **{'publishedfileids[{}]'.format(i): m for i, m in enumerate(mod_ids)}}).encode()
    req = urllib.request.Request(
        'https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/',
        data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read().decode())
    return {it['publishedfileid']: it.get('title', '')
            for it in resp.get('response', {}).get('publishedfiledetails', [])}


# ===================== GUI =====================

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'se_gui_config.json')


class App(ctk.CTk):
    _RENDER_CHUNK = 100

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode('dark')
        ctk.set_default_color_theme('dark-blue')
        ctk.set_widget_scaling(1.25)
        ctk.set_window_scaling(1.15)

        self.title('SE Localization Tool')
        self.geometry('1600x950')
        self.minsize(1300, 800)

        self.cfg = self._load_cfg()
        self.mods_root = self.cfg.get('mods_root', r'F:\Steam\steamapps\workshop\content\244850')
        self.loc_dir = self.cfg.get('loc_dir', r'F:\Steam\steamapps\workshop\loc')
        self.mod_names = self.cfg.get('mod_names', {})
        self.mod_widgets = {}
        self.q = queue.Queue()
        self.task_running = False
        self.translation_rows = []
        self.translation_widgets = []
        self.current_mod_id = None

        self.translation_filter = 'all'
        self.diagnostic_results = {}
        self.current_file_values = {}
        self.problem_indices = []
        self.current_problem_pos = -1
        self._save_after_id = None
        self._tsv_undo_backup = self.cfg.get('tsv_undo_backup')
        self._translation_dirty = False

        self._scanned_mods = set()
        self._scanning_mods = set()

        self._last_export_rows = self.cfg.get('last_export_rows', [])

        self._visible_rows = []
        self._render_pos = 0
        self._render_gen = 0
        self._render_cancel = False

        # Пагинация
        self._page_size = 100
        self._current_page = 0
        self._total_pages = 1
        self._page_buttons = {}

        os.makedirs(self.loc_dir, exist_ok=True)

        self._build_ui()
        self.after(80, self._poll_queue)
        self.after(200, self._initial_load)

    # ---------- CONFIG ----------
    def _load_cfg(self):
        if os.path.isfile(CONFIG_PATH):
            try:
                return json.load(open(CONFIG_PATH, 'r', encoding='utf-8'))
            except Exception:
                pass
        return {}

    def _save_cfg(self):
        self.cfg['mods_root'] = self.mods_root
        self.cfg['loc_dir'] = self.loc_dir
        self.cfg['mod_names'] = self.mod_names
        self.cfg['last_export_rows'] = self._last_export_rows[-5000:]
        self.cfg['tsv_undo_backup'] = self._tsv_undo_backup
        try:
            json.dump(self.cfg, open(CONFIG_PATH, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- TEXT WIDGET HELPERS ----------
    def _inner_text(self, widget):
        return getattr(widget, '_textbox', widget)

    def _is_readonly(self, widget):
        try:
            inner = self._inner_text(widget)
            return str(inner.cget('state')) == 'disabled'
        except Exception:
            return False

    def _text_copy(self, widget):
        try:
            inner = self._inner_text(widget)
            if inner.tag_ranges('sel'):
                text = inner.get('sel.first', 'sel.last')
                self.clipboard_clear()
                self.clipboard_append(text)
        except Exception:
            pass

    def _text_paste(self, widget):
        try:
            if self._is_readonly(widget):
                return
            inner = self._inner_text(widget)
            try:
                data = self.clipboard_get()
            except Exception:
                return
            data = data.replace('\r\n', '\n').replace('\r', '\n')
            if inner.tag_ranges('sel'):
                inner.delete('sel.first', 'sel.last')
            inner.insert('insert', data)
        except Exception:
            pass

    def _text_cut(self, widget):
        try:
            if self._is_readonly(widget):
                return
            inner = self._inner_text(widget)
            if inner.tag_ranges('sel'):
                data = inner.get('sel.first', 'sel.last')
                self.clipboard_clear()
                self.clipboard_append(data)
                inner.delete('sel.first', 'sel.last')
        except Exception:
            pass

    def _text_select_all(self, widget):
        try:
            inner = self._inner_text(widget)
            inner.tag_add('sel', '1.0', 'end-1c')
            inner.mark_set('insert', 'end-1c')
            inner.see('end-1c')
        except Exception:
            pass

    def _text_delete(self, widget):
        try:
            if self._is_readonly(widget):
                return
            inner = self._inner_text(widget)
            inner.delete('sel.first', 'sel.last')
        except Exception:
            pass

    def _text_undo(self, widget):
        try:
            if self._is_readonly(widget):
                return
            self._inner_text(widget).edit_undo()
        except Exception:
            pass

    def _text_redo(self, widget):
        try:
            if self._is_readonly(widget):
                return
            self._inner_text(widget).edit_redo()
        except Exception:
            pass

    def _attach_text_menu(self, widget):
        def show_menu(event=None):
            inner = self._inner_text(widget)
            try:
                has_selection = bool(inner.tag_ranges('sel'))
            except Exception:
                has_selection = False
            is_readonly = self._is_readonly(widget)
            menu = Menu(self, tearoff=0)
            menu.add_command(
                label='Копировать',
                command=lambda w=widget: self._text_copy(w),
                state='normal' if has_selection else 'disabled')
            if not is_readonly:
                menu.add_command(
                    label='Вставить',
                    command=lambda w=widget: self._text_paste(w))
                menu.add_command(
                    label='Вырезать',
                    command=lambda w=widget: self._text_cut(w),
                    state='normal' if has_selection else 'disabled')
                menu.add_command(
                    label='Удалить',
                    command=lambda w=widget: self._text_delete(w),
                    state='normal' if has_selection else 'disabled')
            menu.add_separator()
            menu.add_command(
                label='Выделить всё',
                command=lambda w=widget: self._text_select_all(w))
            if not is_readonly:
                menu.add_separator()
                menu.add_command(
                    label='Отменить',
                    command=lambda w=widget: self._text_undo(w))
                menu.add_command(
                    label='Повторить',
                    command=lambda w=widget: self._text_redo(w))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return 'break'

        def on_key(event=None):
            if event is None:
                return None
            if not (event.state & 0x4):
                return None
            kc = event.keycode
            if kc == 65:
                self._text_select_all(widget); return 'break'
            if kc == 67:
                self._text_copy(widget); return 'break'
            if kc == 86:
                self._text_paste(widget); return 'break'
            if kc == 88:
                self._text_cut(widget); return 'break'
            if kc == 89:
                self._text_redo(widget); return 'break'
            if kc == 90:
                self._text_undo(widget); return 'break'
            return None

        widget.bind('<Button-3>', show_menu)
        widget.bind('<KeyPress>', on_key)

    def _make_readonly_textbox(self, parent, text, **kwargs):
        """
        Создаёт tk.Text (не CTkTextbox!) в режиме disabled — быстро, поддерживает
        выделение и копирование, тёмная тема через kwargs.
        """
        bg = kwargs.pop('fg_color', '#0d1220')
        fg = kwargs.pop('text_color', '#8f9db0')
        font = kwargs.pop('font', ('Segoe UI', 11))
        wrap = kwargs.pop('wrap', 'word')
        min_h = kwargs.pop('height', 30)  # не используется, оставлено для совместимости
        # tk.Text height измеряется в строках.
        try:
            display_lines = text.count('\n') + 1
        except Exception:
            display_lines = 1
        height_lines = max(2, min(15, display_lines))

        box = tk.Text(
            parent,
            height=height_lines,
            width=1,
            bg=bg, fg=fg,
            font=font, wrap=wrap,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground='#1a1f2e',
            highlightcolor='#1a1f2e',
            padx=6, pady=4,
            relief='flat',
            insertbackground=fg,
        )
        box.insert('1.0', text)
        box.configure(state='disabled')
        self._attach_text_menu(box)
        return box

    def _make_editable_textbox(self, parent, text='', **kwargs):
        """Аналогично, но с undo и state='normal'."""
        bg = kwargs.pop('fg_color', '#0b0f18')
        fg = kwargs.pop('text_color', '#c7d3e3')
        font = kwargs.pop('font', ('Segoe UI', 12))
        wrap = kwargs.pop('wrap', 'word')
        border = kwargs.pop('border_color', '#2a3345')
        try:
            display_lines = text.count('\n') + 1
        except Exception:
            display_lines = 1
        height_lines = max(2, min(15, display_lines))

        box = tk.Text(
            parent,
            height=height_lines,
            width=1,
            bg=bg, fg=fg,
            font=font, wrap=wrap,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor='#3a7bfa',
            padx=6, pady=4,
            relief='flat',
            insertbackground=fg,
            undo=True,
            autoseparators=True,
            maxundo=-1,
        )
        box.insert('1.0', text)
        self._attach_text_menu(box)
        return box

    def _autosize_textbox(self, box, min_lines=2, max_lines=15):
        """Растягивает tk.Text по высоте под содержимое (в строках)."""
        try:
            try:
                end_index = box.index('end-1c')
                lines = int(end_index.split('.')[0])
            except Exception:
                text = box.get('1.0', 'end-1c')
                lines = text.count('\n') + 1
            lines = max(min_lines, min(max_lines, lines))
            box.configure(height=lines)
        except Exception:
            pass

    def _attach_scroll_to(self, scrollable, widget):
        """
        Колесо мыши над widget → прокрутка scrollable.
        Средняя кнопка мыши (зажатие + движение) → панорамирование scrollable.
        """
        def _canvas():
            return getattr(scrollable, '_parent_canvas', None)

        def _on_wheel(event=None):
            try:
                canvas = _canvas()
                if canvas is not None:
                    current = canvas.yview()[0]
                    delta = event.delta if event is not None else 0
                    step = -0.06 * (delta / 120.0)
                    canvas.yview_moveto(max(0.0, min(1.0, current + step)))
            except Exception:
                pass
            return 'break'

        def _on_middle_down(event=None):
            try:
                widget._pan_active = True
                widget._pan_start_y = event.y_root if event else 0
                widget._pan_canvas = _canvas()
                try:
                    widget.configure(cursor='fleur')
                except Exception:
                    pass
            except Exception:
                pass
            return 'break'

        def _on_middle_move(event=None):
            try:
                if not getattr(widget, '_pan_active', False):
                    return
                canvas = getattr(widget, '_pan_canvas', None)
                if canvas is None or event is None:
                    return
                delta_y = event.y_root - widget._pan_start_y
                current = canvas.yview()[0]
                frac = -delta_y / 300.0
                canvas.yview_moveto(max(0.0, min(1.0, current + frac)))
                widget._pan_start_y = event.y_root
            except Exception:
                pass
            return 'break'

        def _on_middle_up(event=None):
            try:
                widget._pan_active = False
                widget._pan_canvas = None
                try:
                    widget.configure(cursor='')
                except Exception:
                    pass
            except Exception:
                pass
            return 'break'

        try:
            widget.bind('<MouseWheel>', _on_wheel)
            widget.bind('<Button-2>', _on_middle_down)
            widget.bind('<B2-Motion>', _on_middle_move)
            widget.bind('<ButtonRelease-2>', _on_middle_up)
        except Exception:
            pass

        for child in widget.winfo_children():
            self._attach_scroll_to(scrollable, child)

    def _attach_scroll_wheel(self, widget):
        self._attach_scroll_to(self.translation_scroll, widget)

    def _boost_scroll(self, scrollable):
        self._attach_scroll_to(scrollable, scrollable)

    # ---------- UI ----------
    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color='transparent')
        top.pack(fill='x', padx=16, pady=(14, 8))

        ctk.CTkLabel(top, text='ПАПКА МОДОВ',
                     font=('Segoe UI', 12, 'bold'), text_color='#7d8ca3').pack(anchor='w')
        row1 = ctk.CTkFrame(top, fg_color='transparent')
        row1.pack(fill='x', pady=(4, 0))
        self.path_entry = ctk.CTkEntry(row1, height=38,
                                       font=('Segoe UI', 13), fg_color='#1a1f2e',
                                       border_color='#2a3345')
        self.path_entry.pack(side='left', fill='x', expand=True)
        self.path_entry.insert(0, self.mods_root)
        ctk.CTkButton(row1, text='Обзор', width=100, height=38,
                      font=('Segoe UI', 12),
                      command=self._pick_root).pack(side='left', padx=(8, 0))
        ctk.CTkButton(row1, text='Загрузить список', width=170, height=38,
                      fg_color='#2a3345', hover_color='#3a4459',
                      font=('Segoe UI', 12),
                      command=self._load_mods).pack(side='left', padx=(8, 0))

        actions = ctk.CTkFrame(self, fg_color='transparent')
        actions.pack(fill='x', padx=16, pady=(4, 8))

        buttons = [
            ('Сканировать',        '#3a7bfa', self._scan,        'Извлечь строки из выбранных модов в master.tsv'),
            ('Открыть master.tsv', '#2a3345', self._open_master, 'Открыть файл для перевода'),
            ('Импорт перевода',    '#2a3345', self._import,      'Загрузить переведённый TSV'),
            ('Бэкап',              '#2a3345', self._backup,      'Копия файлов, которые будут изменены'),
            ('Инжект',             '#28a745', self._inject,      'Применить переводы из master_ru_fixed.tsv'),
            ('Диагностика',        '#2a3345', self._diag,        'Проверить, что не применилось'),
        ]
        for text, color, cmd, tip in buttons:
            btn = ctk.CTkButton(actions, text=text, fg_color=color,
                                hover_color=self._hover(color), height=40,
                                font=('Segoe UI', 13, 'bold'), command=cmd)
            btn.pack(side='left', padx=(0, 6))

        body = ctk.CTkFrame(self, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        body.grid_columnconfigure(0, weight=2)
        body.grid_columnconfigure(1, weight=3)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color='#141822', corner_radius=10,
                            border_width=1, border_color='#232a38')
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 8))

        head = ctk.CTkFrame(left, fg_color='transparent')
        head.pack(fill='x', padx=14, pady=(12, 6))
        ctk.CTkLabel(head, text='МОДЫ', font=('Segoe UI', 12, 'bold'),
                     text_color='#7d8ca3').pack(side='left')
        self.count_label = ctk.CTkLabel(head, text='0 / 0', font=('Segoe UI', 12),
                                        text_color='#5a6a80')
        self.count_label.pack(side='right')

        self.search_entry = ctk.CTkEntry(left, height=36,
                                          placeholder_text='Поиск по имени или ID',
                                          font=('Segoe UI', 12),
                                          fg_color='#0f131c', border_color='#232a38')
        self.search_entry.pack(fill='x', padx=14, pady=(0, 6))
        self.search_entry.bind('<KeyRelease>', lambda e=None: self._filter_mods())

        bulk = ctk.CTkFrame(left, fg_color='transparent')
        bulk.pack(fill='x', padx=14, pady=(0, 6))
        for text, cmd in [('Все', self._select_all), ('Никто', self._select_none), ('Инверт', self._invert)]:
            ctk.CTkButton(bulk, text=text, width=80, height=30,
                          fg_color='#1f2733', hover_color='#2a3345',
                          font=('Segoe UI', 12), command=cmd).pack(side='left', padx=(0, 4))

        self.mods_scroll = ctk.CTkScrollableFrame(left, fg_color='transparent')
        self.mods_scroll.pack(fill='both', expand=True, padx=8, pady=(0, 10))
        self._boost_scroll(self.mods_scroll)

        right = ctk.CTkFrame(body, fg_color='#141822', corner_radius=10,
                             border_width=1, border_color='#232a38')
        right.grid(row=0, column=1, sticky='nsew')

        head2 = ctk.CTkFrame(right, fg_color='transparent')
        head2.pack(fill='x', padx=14, pady=(12, 6))
        ctk.CTkLabel(head2, text='РАБОЧАЯ ОБЛАСТЬ', font=('Segoe UI', 12, 'bold'),
                     text_color='#7d8ca3').pack(side='left')
        self.work_mode_label = ctk.CTkLabel(head2, text='Журнал', font=('Segoe UI', 11),
                                            text_color='#5a6a80')
        self.work_mode_label.pack(side='right')

        self.work_tabs = ctk.CTkTabview(right, fg_color='#141822',
                                        segmented_button_fg_color='#1f2733',
                                        segmented_button_selected_color='#3a7bfa',
                                        segmented_button_selected_hover_color='#5590fc')
        self.work_tabs.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        self.work_tabs.add('Журнал')
        self.work_tabs.add('Перевод мода')

        journal_tab = self.work_tabs.tab('Журнал')
        journal_tools = ctk.CTkFrame(journal_tab, fg_color='transparent')
        journal_tools.pack(fill='x', padx=4, pady=(2, 5))
        for text, cmd in [('Копировать', lambda: self._edit_action('copy')),
                          ('Вставить', lambda: self._edit_action('paste')),
                          ('Вырезать', lambda: self._edit_action('cut')),
                          ('Выделить всё', lambda: self._edit_action('select_all')),
                          ('Отменить', lambda: self._edit_action('undo')),
                          ('Повторить', lambda: self._edit_action('redo')),
                          ('Найти', self._find_log),
                          ('Сохранить журнал', self._save_log)]:
            ctk.CTkButton(journal_tools, text=text,
                          width=92 if text not in ('Выделить всё',) else 108,
                          height=28, fg_color='#1f2733', hover_color='#2a3345',
                          font=('Segoe UI', 11), command=cmd).pack(side='left', padx=(0, 4))
        ctk.CTkButton(journal_tools, text='Очистить', width=86, height=28,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11), command=self._clear_log).pack(side='right')

        self.log_box = ctk.CTkTextbox(journal_tab, fg_color='#0b0f18',
                                       text_color='#c7d3e3', font=('Consolas', 13),
                                       border_width=0, wrap='word', undo=True,
                                       autoseparators=True, maxundo=-1)
        self.log_box.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        self._attach_text_menu(self.log_box)
        self.log_box.bind('<Control-f>', lambda e=None: (self._find_log(), 'break')[1])

        trans_tab = self.work_tabs.tab('Перевод мода')
        trans_head = ctk.CTkFrame(trans_tab, fg_color='transparent')
        trans_head.pack(fill='x', padx=4, pady=(2, 5))
        self.translation_title = ctk.CTkLabel(trans_head, text='Выберите мод',
                                              font=('Segoe UI', 12, 'bold'),
                                              text_color='#c7d3e3')
        self.translation_title.pack(side='left')

        ctk.CTkButton(trans_head, text='Сохранить правки', width=150, height=32,
                      fg_color='#28a745', hover_color='#33c057',
                      font=('Segoe UI', 11, 'bold'),
                      command=self._save_translation_edits).pack(side='right', padx=(6, 0))
        ctk.CTkButton(trans_head, text='Применить мод', width=140, height=32,
                      fg_color='#28a745', hover_color='#33c057',
                      font=('Segoe UI', 11, 'bold'),
                      command=self._apply_current_mod).pack(side='right', padx=(6, 0))
        ctk.CTkButton(trans_head, text='Пересканировать мод', width=175, height=32,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._rescan_current_mod).pack(side='right', padx=(6, 0))
        ctk.CTkButton(trans_head, text='Открыть папку мода', width=165, height=32,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._open_current_mod_folder).pack(side='right', padx=(6, 0))
        ctk.CTkButton(trans_head, text='Отменить последнее сохранение', width=215, height=32,
                      fg_color='#8a3f3f', hover_color='#a64d4d',
                      font=('Segoe UI', 11),
                      command=self._undo_tsv_edit).pack(side='right', padx=(6, 0))

        filter_bar = ctk.CTkFrame(trans_tab, fg_color='transparent')
        filter_bar.pack(fill='x', padx=4, pady=(0, 5))
        ctk.CTkLabel(filter_bar, text='Фильтр:',
                     font=('Segoe UI', 11, 'bold'),
                     text_color='#7d8ca3').pack(side='left', padx=(0, 6))

        self.filter_buttons = {}
        for text, value in [
            ('Все', 'all'),
            ('Переведённые', 'translated'),
            ('Непереведённые', 'untranslated'),
            ('Проблемные', 'problem'),
        ]:
            btn = ctk.CTkButton(
                filter_bar, text=text, width=125, height=31,
                fg_color='#1f2733', hover_color='#2a3345',
                font=('Segoe UI', 11),
                command=lambda v=value: self._set_translation_filter(v))
            btn.pack(side='left', padx=(0, 4))
            self.filter_buttons[value] = btn

        ctk.CTkButton(filter_bar, text='← Предыдущая ошибка', width=185, height=31,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._previous_problem).pack(side='left', padx=(10, 4))
        ctk.CTkButton(filter_bar, text='Следующая ошибка →', width=185, height=31,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._next_problem).pack(side='left', padx=(0, 4))

        io_bar = ctk.CTkFrame(trans_tab, fg_color='transparent')
        io_bar.pack(fill='x', padx=4, pady=(0, 5))
        ctk.CTkButton(io_bar, text='Экспорт для перевода', width=200, height=31,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._export_current_mod_translation).pack(side='left', padx=(0, 4))
        ctk.CTkButton(io_bar, text='Загрузить перевод', width=180, height=31,
                      fg_color='#1f2733', hover_color='#2a3345',
                      font=('Segoe UI', 11),
                      command=self._import_current_mod_translation).pack(side='left', padx=(0, 4))

        self.render_status = ctk.CTkLabel(io_bar, text='',
                                          font=('Segoe UI', 11), text_color='#7d8ca3')
        self.render_status.pack(side='left', padx=(10, 0))

        # ---- Панель пагинации ----
        page_bar = ctk.CTkFrame(trans_tab, fg_color='#1a1f2e', corner_radius=6)
        page_bar.pack(fill='x', padx=4, pady=(0, 5))

        self.page_prev_btn = ctk.CTkButton(
            page_bar, text='← Назад',
            width=100, height=30,
            fg_color='#2a3345', hover_color='#3a4459',
            font=('Segoe UI', 11), command=self._prev_page)
        self.page_prev_btn.pack(side='left', padx=(8, 4), pady=4)

        self.page_label = ctk.CTkLabel(page_bar, text='Страница 1 / 1',
                                       font=('Segoe UI', 11, 'bold'),
                                       text_color='#c7d3e3')
        self.page_label.pack(side='left', padx=(4, 4))

        self.page_next_btn = ctk.CTkButton(
            page_bar, text='Вперёд →',
            width=100, height=30,
            fg_color='#2a3345', hover_color='#3a4459',
            font=('Segoe UI', 11), command=self._next_page)
        self.page_next_btn.pack(side='left', padx=(4, 8), pady=4)

        # Размер страницы
        ctk.CTkLabel(page_bar, text='Строк на странице:',
                     font=('Segoe UI', 10), text_color='#7d8ca3').pack(
                         side='left', padx=(12, 4))
        self._page_size_var = ctk.StringVar(value='100')
        self.page_size_menu = ctk.CTkOptionMenu(
            page_bar, values=['50', '100', '200', '500'],
            variable=self._page_size_var,
            width=90, height=30,
            font=('Segoe UI', 11),
            command=self._on_page_size_change)
        self.page_size_menu.pack(side='left', padx=(0, 8), pady=4)

        # Быстрый переход
        ctk.CTkLabel(page_bar, text='Перейти к стр.:',
                     font=('Segoe UI', 10), text_color='#7d8ca3').pack(
                         side='left', padx=(12, 4))
        self.page_jump_entry = ctk.CTkEntry(page_bar, width=70, height=30,
                                            font=('Segoe UI', 11))
        self.page_jump_entry.pack(side='left', padx=(0, 4), pady=4)
        self.page_jump_entry.bind('<Return>',
                                  lambda e=None: self._jump_to_page())
        ctk.CTkButton(page_bar, text='Перейти', width=80, height=30,
                      fg_color='#3a7bfa', hover_color='#5590fc',
                      font=('Segoe UI', 11),
                      command=self._jump_to_page).pack(side='left', padx=(0, 8), pady=4)

        self.render_cancel_btn = ctk.CTkButton(
            io_bar, text='Отменить загрузку', width=170, height=31,
            fg_color='#8a3f3f', hover_color='#a64d4d',
            font=('Segoe UI', 11), command=self._cancel_render)
        self.render_cancel_btn.pack(side='right', padx=(4, 0))
        self.render_cancel_btn.configure(state='disabled')

        self.translation_scroll = ctk.CTkScrollableFrame(trans_tab, fg_color='#0b0f18')
        self.translation_scroll.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        self._boost_scroll(self.translation_scroll)

        prog_frame = ctk.CTkFrame(right, fg_color='transparent')
        prog_frame.pack(fill='x', padx=14, pady=(0, 10))
        self.progress = ctk.CTkProgressBar(prog_frame, height=12,
                                           progress_color='#3a7bfa', fg_color='#1a1f2e')
        self.progress.pack(fill='x')
        self.progress.set(0)
        self.status_label = ctk.CTkLabel(prog_frame, text='Готов',
                                          font=('Segoe UI', 12), text_color='#7d8ca3')
        self.status_label.pack(anchor='w', pady=(4, 0))

        stats = ctk.CTkFrame(right, fg_color='#0f131c', corner_radius=8)
        stats.pack(fill='x', padx=14, pady=(0, 14))
        self.stat_labels = {}
        for i, (k, label) in enumerate([
            ('mods', 'Модов в списке'), ('selected', 'Выбрано'),
            ('strings', 'Строк в TSV'), ('filled', 'Переведено'),
        ]):
            col = i % 2
            row = i // 2
            cell = ctk.CTkFrame(stats, fg_color='transparent')
            cell.grid(row=row, column=col, sticky='ew', padx=12, pady=8)
            stats.grid_columnconfigure(col, weight=1)
            ctk.CTkLabel(cell, text=label.upper(), font=('Segoe UI', 10, 'bold'),
                         text_color='#5a6a80').pack(anchor='w')
            v = ctk.CTkLabel(cell, text='—', font=('Segoe UI', 20, 'bold'),
                             text_color='#c7d3e3')
            v.pack(anchor='w')
            self.stat_labels[k] = v

    def _hover(self, color):
        return {'#3a7bfa': '#5590fc', '#28a745': '#33c057'}.get(color, '#2a3345')

    def _pick_root(self):
        try:
            d = filedialog.askdirectory(initialdir=self.mods_root)
        except Exception as e:
            self._log('Ошибка диалога: {}'.format(e))
            return
        if d:
            self.mods_root = d
            self.path_entry.delete(0, 'end')
            self.path_entry.insert(0, d)
            self._save_cfg()
            self._load_mods()

    def _initial_load(self):
        if os.path.isdir(self.mods_root):
            self._load_mods()

    def _load_mods(self):
        root = self.path_entry.get().strip()
        if not os.path.isdir(root):
            self._log('Не найдена папка: {}'.format(root))
            return
        self.mods_root = root
        self._save_cfg()
        for w in list(self.mods_scroll.winfo_children()):
            w.destroy()
        self.mod_widgets.clear()
        self._scanned_mods.clear()
        self._scanning_mods.clear()

        ids = sorted([d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d))
                      and '_backup_' not in d and d.isdigit()],
                     key=lambda x: int(x))

        unknown = [mid for mid in ids if mid not in self.mod_names]
        if unknown:
            threading.Thread(target=self._fetch_names_worker,
                             args=(unknown,), daemon=True).start()

        for mid in ids:
            self._add_mod_row(mid)
        self._update_counts()
        self._log('Найдено {} модов'.format(len(ids)))

    def _fetch_names_worker(self, unknown):
        try:
            names = fetch_mod_names(unknown)
            for k, v in names.items():
                self.mod_names[k] = v
            self.q.put(('refresh_names', None))
        except Exception as e:
            self.q.put(('log', 'Steam API недоступен: {}'.format(e)))

    def _add_mod_row(self, mid):
        row = ctk.CTkFrame(self.mods_scroll, fg_color='#0f131c', corner_radius=6,
                           height=42)
        row.pack(fill='x', pady=2)
        row.pack_propagate(False)
        var = ctk.BooleanVar(value=False)
        cb = ctk.CTkCheckBox(row, text='', variable=var, width=24,
                             checkbox_width=22, checkbox_height=22,
                             fg_color='#3a7bfa', hover_color='#5590fc',
                             command=lambda m=mid, v=var: self._on_mod_selected(m, v))
        cb.pack(side='left', padx=(10, 8))
        name = self.mod_names.get(mid, '') or '(без названия)'
        lbl = ctk.CTkLabel(row, text='{}'.format(name),
                           font=('Segoe UI', 13), text_color='#c7d3e3',
                           anchor='w')
        lbl.pack(side='left', fill='x', expand=True)
        ctk.CTkLabel(row, text=mid, font=('Consolas', 11),
                     text_color='#5a6a80').pack(side='right', padx=(6, 10))
        self.mod_widgets[mid] = (row, var, lbl, name)

        for widget in (row, lbl):
            widget.bind('<Double-Button-1>',
                        lambda e=None, m=mid: self._open_mod_translation(m))
        lbl.bind('<Button-1>', lambda e=None, m=mid: self._preview_mod(m))

        try:
            self._attach_scroll_to(self.mods_scroll, row)
        except Exception:
            pass

    def _on_mod_selected(self, mid, var):
        self._update_counts()
        if var.get():
            self.current_mod_id = mid
            self._load_selected_mod_translation(mid)

    def _preview_mod(self, mid):
        self.current_mod_id = mid

    def _open_mod_translation(self, mid):
        for other_mid, (row, var, lbl, name) in self.mod_widgets.items():
            var.set(other_mid == mid)
        self.current_mod_id = mid
        self._update_counts()
        self._load_selected_mod_translation(mid)
        self.work_tabs.set('Перевод мода')

    def _row_id(self, row):
        return (row[0], row[1], row[2], row[3], row[4], row[5], row[6])

    def _load_selected_mod_translation(self, mid):
        if self._save_after_id:
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
            self._save_after_id = None
        if self._translation_dirty and self.translation_widgets:
            try:
                self._save_translation_edits(automatic=True)
            except Exception as e:
                self._log('Не удалось сохранить предыдущие правки: {}'.format(e))

        self.current_mod_id = mid
        self._clear_translation_editor()
        self._translation_dirty = False

        name = self.mod_names.get(mid, '') or '(без названия)'
        self.work_mode_label.configure(text='Мод: {}'.format(name))

        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        rows = []
        if os.path.isfile(tsv):
            try:
                with _TSV_LOCK:
                    with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                        r = csv.reader(f, delimiter='\t')
                        header = next(r, None)
                        if header != _TSV_HEADER:
                            raise ValueError('неверный заголовок TSV')
                        all_raw = [row for row in r if len(row) == 8]

                repaired = 0
                for row in all_raw:
                    clean = [_sanitize_field(c) for c in row[:8]]
                    if clean[0] != mid:
                        continue
                    orig_val = clean[7]
                    clean[7] = _strip_translated_wrapper_global(clean[6], clean[7])
                    if clean[7] != orig_val:
                        repaired += 1
                    rows.append(clean)

                if repaired:
                    self._log('Автоочистка: восстановлено {} строк.'.format(repaired))
                    try:
                        with _TSV_LOCK:
                            full_rows = []
                            for row in all_raw:
                                rr = [_sanitize_field(c) for c in row[:8]]
                                rr[7] = _strip_translated_wrapper_global(rr[6], rr[7])
                                full_rows.append(rr)
                            tmp = tsv + '.tmp'
                            with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                                w = csv.writer(f, delimiter='\t',
                                               quoting=csv.QUOTE_MINIMAL,
                                               lineterminator='\n')
                                w.writerow(_TSV_HEADER)
                                w.writerows(full_rows)
                            os.replace(tmp, tsv)
                    except Exception as e:
                        self._log('Не удалось перезаписать TSV: {}'.format(e))
            except Exception as e:
                self._log('Ошибка загрузки перевода {}: {}'.format(mid, e))
                return

        if not rows:
            if mid in self._scanning_mods:
                self.translation_title.configure(text='{} — сканирование...'.format(name))
                return
            if mid in self._scanned_mods:
                self.translation_title.configure(
                    text='{} — строк для перевода не найдено'.format(name))
                self._log('Мод {}: строк для перевода не найдено.'.format(mid))
                return
            self.translation_title.configure(text='{} — сканирование...'.format(name))
            self._log('Мод {}: сканирую строки...'.format(mid))
            self._schedule_mod_scan(mid)
            return

        self.translation_rows = rows
        self._update_mod_statistics()
        self._rebuild_translation_editor()

    def _schedule_mod_scan(self, mid, force=False):
        if not force and mid in self._scanning_mods:
            return
        self._scanning_mods.add(mid)

        def worker():
            try:
                path = os.path.join(self.mods_root, mid)
                scanned = scan_mod(path)
                self.q.put(('scan_result', (mid, scanned)))
            except Exception as e:
                self.q.put(('log', 'Ошибка сканирования {}: {}'.format(mid, e)))
                self.q.put(('scan_result', (mid, [])))

        threading.Thread(target=worker, daemon=True).start()

    def _merge_scanned_into_tsv(self, mid, scanned):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')

        with _TSV_LOCK:
            existing_others = []
            existing_for_mod = {}

            if os.path.isfile(tsv):
                try:
                    with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                        r = csv.reader(f, delimiter='\t')
                        h = next(r, None)
                        if h != _TSV_HEADER:
                            bad = tsv + '.bad_' + datetime.now().strftime('%Y%m%d_%H%M%S')
                            os.replace(tsv, bad)
                            self.q.put(('log',
                                        'TSV имел неверный заголовок, '
                                        'переименован в {}'.format(os.path.basename(bad))))
                        else:
                            for row in r:
                                if len(row) != 8:
                                    continue
                                row = [_sanitize_field(c) for c in row[:8]]
                                row[7] = _strip_translated_wrapper_global(row[6], row[7])
                                if row[0] == mid:
                                    existing_for_mod[self._row_id(row)] = row
                                else:
                                    existing_others.append(row)
                except Exception as e:
                    self.q.put(('log', 'Ошибка чтения TSV при merge: {}'.format(e)))
                    return

            scanned_ids = set()
            mod_rows = []
            added = 0
            kept = 0
            for kind, rel, ln, col, key, orig in scanned:
                rid = (mid, rel, str(ln), str(col), kind, key, orig)
                scanned_ids.add(rid)
                if rid in existing_for_mod:
                    mod_rows.append(existing_for_mod[rid])
                    kept += 1
                else:
                    mod_rows.append([mid, rel, str(ln), str(col), kind, key, orig, ''])
                    added += 1

            for rid, row in existing_for_mod.items():
                if rid not in scanned_ids:
                    mod_rows.append(row)
                    kept += 1

            all_rows = existing_others + mod_rows

            def sort_key(r):
                try: mid_key = int(r[0])
                except (ValueError, TypeError): mid_key = 0
                try: ln_key = int(r[2])
                except (ValueError, TypeError): ln_key = 0
                try: col_key = int(r[3])
                except (ValueError, TypeError): col_key = 0
                return (mid_key, r[1], ln_key, col_key)

            all_rows.sort(key=sort_key)

            tmp = tsv + '.tmp'
            with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                               lineterminator='\n')
                w.writerow(_TSV_HEADER)
                for r in all_rows:
                    w.writerow([_sanitize_field(c) for c in r])
            os.replace(tmp, tsv)

        if added:
            self._log('Мод {}: добавлено {} новых строк (сохранено {} старых).'.format(
                mid, added, kept))
        else:
            self._log('Мод {}: {} строк уже были в TSV.'.format(mid, kept))

        try:
            with _TSV_LOCK:
                with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    total = sum(1 for _ in csv.reader(f, delimiter='\t')) - 1
            filled = self._count_filled_tsv()
            self.stat_labels['strings'].configure(text=str(total))
            self.stat_labels['filled'].configure(text=str(filled))
        except Exception:
            pass

    def _clear_translation_editor(self):
        self._render_gen += 1
        self._render_cancel = True
        for w in self.translation_scroll.winfo_children():
            w.destroy()
        self.translation_widgets = []
        self.translation_rows = []
        self._visible_rows = []
        self._render_pos = 0
        self._current_page = 0
        self._total_pages = 1

    def _compute_visible_rows(self):
        visible = []
        for row in self.translation_rows:
            status = self._translation_status(row)
            if self.translation_filter == 'all':
                show = True
            elif self.translation_filter == 'translated':
                show = status in ('translated', 'applied', 'already')
            elif self.translation_filter == 'untranslated':
                show = status == 'untranslated'
            elif self.translation_filter == 'problem':
                show = status == 'failed'
            else:
                show = True
            if show:
                visible.append(row)
        return visible

    def _add_translation_row(self, idx, row):
        row_id = self._row_id(row)
        card = ctk.CTkFrame(
            self.translation_scroll,
            fg_color='#111722',
            corner_radius=6,
            border_width=1,
            border_color='#232a38')
        card.pack(fill='x', padx=4, pady=4)

        status = self.diagnostic_results.get(row_id)
        if status == 'failed':
            border = '#d9534f'
            status_text = '○ В ФАЙЛЕ ОРИГИНАЛ — НЕ ПРИМЕНЕНО'
        elif status == 'already':
            border = '#d9a441'
            status_text = '⚠ В ФАЙЛЕ УЖЕ ДРУГОЙ ПЕРЕВОД'
        elif status == 'applied':
            border = '#28a745'
            status_text = '✓ ПРИМЕНЕНО (файл совпадает с переводом)'
        elif status == 'empty':
            border = '#5a6a80'
            status_text = '○ ПУСТО (перевод не введён)'
        elif row[7].strip():
            border = '#28a745'
            status_text = '✓ ПЕРЕВЕДЕНО (не проверено диагностикой)'
        else:
            border = '#d9a441'
            status_text = '○ НЕ ПЕРЕВЕДЕНО'
        card.configure(border_color=border)

        meta = '{}. {}:{}  [{}{}]'.format(
            idx, row[1], row[2], row[4], (' / ' + row[5]) if row[5] else '')
        ctk.CTkLabel(card, text=meta, font=('Consolas', 11, 'bold'),
                     text_color='#7d8ca3', anchor='w').pack(fill='x', padx=8, pady=(8, 2))

        # Оригинал (read-only) на tk.Text — быстро и с копированием.
        self._make_readonly_textbox(
            card, row[6],
            fg_color='#0d1220',
            text_color='#8f9db0',
            font=('Segoe UI', 12),
            wrap='word'
        ).pack(fill='x', padx=8, pady=(0, 4))

        cv = self.current_file_values.get(row_id)
        if cv is not None and cv != row[6] and cv != row[7]:
            ctk.CTkLabel(card, text='В ФАЙЛЕ СЕЙЧАС:',
                         font=('Segoe UI', 10, 'bold'),
                         text_color='#d9a441', anchor='w').pack(
                             fill='x', padx=8, pady=(2, 0))
            self._make_readonly_textbox(
                card, str(cv),
                fg_color='#1a1408',
                text_color='#d9c89a',
                font=('Segoe UI', 11),
                wrap='word'
            ).pack(fill='x', padx=8, pady=(0, 4))

        if row[4] == 'sbc' and row[6].strip().startswith('<'):
            ctk.CTkLabel(card, text='Подсказка: пишите только сам перевод, теги XML добавляются автоматически.',
                         font=('Segoe UI', 10), text_color='#5a6a80', anchor='w').pack(
                             fill='x', padx=8, pady=(0, 4))

        ctk.CTkLabel(card, text=status_text, font=('Segoe UI', 12, 'bold'),
                     text_color=border, anchor='w').pack(fill='x', padx=8, pady=(0, 4))

        # Поле перевода — tk.Text, растёт по содержимому.
        box = self._make_editable_textbox(
            card, row[7],
            fg_color='#0b0f18', text_color='#c7d3e3',
            font=('Segoe UI', 12), wrap='word',
            border_color='#2a3345'
        )
        box.pack(fill='x', padx=8, pady=(0, 8))
        box.bind('<KeyRelease>',
                 lambda e=None, r=row, b=box: self._translation_changed(r, b))

        try:
            self._attach_scroll_to(self.translation_scroll, card)
        except Exception:
            pass

        self.translation_widgets.append({'row': row, 'box': box, 'card': card})

    def _translation_status(self, row):
        row_id = self._row_id(row)
        diagnostic = self.diagnostic_results.get(row_id)
        if diagnostic:
            return diagnostic
        if row[7].strip():
            return 'translated'
        return 'untranslated'

    def _set_translation_filter(self, value):
        if self._translation_dirty:
            self._auto_save_translation()
        self.translation_filter = value
        self._rebuild_translation_editor()

    def _rebuild_translation_editor(self):
        if not self.current_mod_id:
            return
        self._render_gen += 1
        self._render_cancel = True

        self._visible_rows = self._compute_visible_rows()
        total = len(self._visible_rows)
        page_size = max(10, self._page_size)
        self._total_pages = max(1, (total + page_size - 1) // page_size)
        if self._current_page >= self._total_pages:
            self._current_page = self._total_pages - 1
        if self._current_page < 0:
            self._current_page = 0

        for widget in self.translation_scroll.winfo_children():
            widget.destroy()
        self.translation_widgets = []
        self._render_pos = 0

        for value, btn in self.filter_buttons.items():
            if value == self.translation_filter:
                btn.configure(fg_color='#3a7bfa', hover_color='#5590fc')
            else:
                btn.configure(fg_color='#1f2733', hover_color='#2a3345')

        # Пагинация
        self.page_label.configure(
            text='Страница {} / {}  (всего {} строк)'.format(
                self._current_page + 1, self._total_pages, total))
        self.page_prev_btn.configure(
            state='normal' if self._current_page > 0 else 'disabled')
        self.page_next_btn.configure(
            state='normal' if self._current_page < self._total_pages - 1 else 'disabled')

        self._update_mod_statistics()

        if total == 0:
            self.render_status.configure(text='Нет строк для отображения')
            self.render_cancel_btn.configure(state='disabled')
            return

        # Отключаем пагинацию и рендерим всю страницу синхронно.
        # 100 карточек на tk.Text — быстро (< 1 сек).
        start = self._current_page * page_size
        end = min(start + page_size, total)
        self.render_status.configure(
            text='Показано: {}–{} из {}'.format(start + 1, end, total))
        self.render_cancel_btn.configure(state='disabled')

        for i in range(start, end):
            self._add_translation_row(i + 1, self._visible_rows[i])
        self._render_pos = end - start

    def _prev_page(self):
        if self._current_page > 0:
            self._save_current_edits_into_memory()
            self._current_page -= 1
            self._rebuild_translation_editor()
            self._scroll_to_top()

    def _next_page(self):
        if self._current_page < self._total_pages - 1:
            self._save_current_edits_into_memory()
            self._current_page += 1
            self._rebuild_translation_editor()
            self._scroll_to_top()

    def _jump_to_page(self):
        try:
            val = int(self.page_jump_entry.get().strip())
        except Exception:
            return
        if val < 1:
            val = 1
        if val > self._total_pages:
            val = self._total_pages
        self._save_current_edits_into_memory()
        self._current_page = val - 1
        self._rebuild_translation_editor()
        self._scroll_to_top()

    def _on_page_size_change(self, value):
        try:
            self._page_size = int(value)
        except Exception:
            self._page_size = 100
        self._current_page = 0
        self._save_current_edits_into_memory()
        self._rebuild_translation_editor()

    def _save_current_edits_into_memory(self):
        """
        Считывает содержимое видимых полей ввода в self.translation_rows
        (in-memory), чтобы не потерять правки при переходе между страницами.
        Не пишет на диск.
        """
        for item in self.translation_widgets:
            row = item['row']
            box = item['box']
            try:
                new_text = _sanitize_field(box.get('1.0', 'end-1c'))
                cleaned = _strip_translated_wrapper_global(row[6], new_text)
                if cleaned != row[7]:
                    row[7] = cleaned
                    self._translation_dirty = True
            except Exception:
                pass

    def _scroll_to_top(self):
        try:
            canvas = getattr(self.translation_scroll, '_parent_canvas', None)
            if canvas is not None:
                canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _render_next_chunk(self, gen):
        # Больше не используется — рендер идёт по страницам синхронно.
        return

    def _cancel_render(self):
        if self._render_pos >= len(self._visible_rows):
            return
        self._render_cancel = True
        self.render_status.configure(
            text='Загрузка отменена: {} / {}'.format(
                self._render_pos, len(self._visible_rows)))
        self.render_cancel_btn.configure(state='disabled')

    def _update_mod_statistics(self):
        if not self.current_mod_id:
            return
        rows = self.translation_rows
        total = len(rows)
        translated = sum(1 for row in rows if row[7].strip())
        empty = total - translated
        problematic = sum(
            1 for row in rows
            if self.diagnostic_results.get(self._row_id(row)) == 'failed')
        self.translation_title.configure(
            text='{} — {} строк / {} переведено / {} пустых / {} проблемных'.format(
                self.mod_names.get(self.current_mod_id, self.current_mod_id),
                total, translated, empty, problematic))

    def _rebuild_problem_list(self):
        self.problem_indices = []
        for i, row in enumerate(self.translation_rows):
            row_id = self._row_id(row)
            if self.diagnostic_results.get(row_id) == 'failed':
                self.problem_indices.append(i)
        self.current_problem_pos = -1

    def _next_problem(self):
        if not self.problem_indices:
            messagebox.showinfo('Ошибок нет',
                                'Диагностика не обнаружила проблемных строк.')
            return
        self.current_problem_pos += 1
        if self.current_problem_pos >= len(self.problem_indices):
            self.current_problem_pos = 0
        self._focus_translation_row(self.problem_indices[self.current_problem_pos])

    def _previous_problem(self):
        if not self.problem_indices:
            messagebox.showinfo('Ошибок нет',
                                'Диагностика не обнаружила проблемных строк.')
            return
        self.current_problem_pos -= 1
        if self.current_problem_pos < 0:
            self.current_problem_pos = len(self.problem_indices) - 1
        self._focus_translation_row(self.problem_indices[self.current_problem_pos])

    def _focus_translation_row(self, index):
        if index < 0 or index >= len(self.translation_rows):
            return
        target = self.translation_rows[index]
        target_id = self._row_id(target)

        # Переключаем фильтр на «Проблемные»
        self.translation_filter = 'problem'
        self._visible_rows = self._compute_visible_rows()
        total = len(self._visible_rows)
        page_size = max(10, self._page_size)
        self._total_pages = max(1, (total + page_size - 1) // page_size)

        # Ищем целевую строку в visible_rows
        target_visible_idx = None
        for i, r in enumerate(self._visible_rows):
            if self._row_id(r) == target_id:
                target_visible_idx = i
                break
        if target_visible_idx is None:
            return

        # Определяем нужную страницу
        wanted_page = target_visible_idx // page_size
        self._current_page = wanted_page
        self._rebuild_translation_editor()

        # Подсвечиваем
        for item in self.translation_widgets:
            if self._row_id(item['row']) == target_id:
                item['card'].configure(border_color='#ff5555')
                item['box'].focus_set()
                item['box'].see('1.0')
                # Прокручиваем к карточке
                try:
                    self.translation_scroll._parent_canvas.yview_moveto(
                        item['card'].winfo_y() /
                        max(1, self.translation_scroll._parent_canvas.bbox('all')[3]))
                except Exception:
                    pass
                break

    # ---------- EXPORT / IMPORT ----------
    def _export_current_mod_translation(self):
        if not self.current_mod_id:
            messagebox.showinfo('Мод не выбран', 'Сначала выбери мод.')
            return
        if self._save_after_id:
            try: self.after_cancel(self._save_after_id)
            except Exception: pass
            self._save_after_id = None
        if self._translation_dirty and self.translation_widgets:
            self._save_translation_edits(automatic=True)
        if not self.translation_rows:
            messagebox.showinfo('Нет строк', 'Для этого мода нет строк для перевода.')
            return
        # Экспорт по текущему фильтру — ВСЕ страницы, не только видимая.
        self._save_current_edits_into_memory()
        rows_to_export = self._compute_visible_rows()
        if not rows_to_export:
            messagebox.showinfo('Нет строк', 'По текущему фильтру нет строк.')
            return
        default_name = 'translation_{}.tsv'.format(self.current_mod_id)
        try:
            p = filedialog.asksaveasfilename(
                title='Экспорт перевода мода',
                initialdir=self.loc_dir,
                initialfile=default_name,
                defaultextension='.tsv',
                filetypes=[('TSV', '*.tsv'), ('Все файлы', '*.*')])
        except Exception as e:
            self._log('Диалог сохранения не открылся: {}'.format(e))
            return
        if not p:
            return
        try:
            with open(p, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                               lineterminator='\n')
                w.writerow(_TSV_HEADER)
                for row in rows_to_export:
                    w.writerow([_sanitize_field(c) for c in row])
            self._last_export_rows = [[_sanitize_field(c) for c in r] for r in rows_to_export]
            self.cfg['last_export_mod_id'] = self.current_mod_id
            self.cfg['last_export_filter'] = self.translation_filter
            self._save_cfg()
            self._log('Экспортировано {} строк в {}'.format(
                len(rows_to_export), os.path.basename(p)))
            messagebox.showinfo(
                'Экспорт готов',
                'Файл: {}\nСтрок: {}\n(экспортировано по фильтру «{}»)'.format(
                    os.path.basename(p), len(rows_to_export),
                    self.translation_filter))
        except Exception as e:
            messagebox.showerror('Ошибка экспорта', str(e))

    def _load_tsv_directly(self, path):
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                content = f.read()
        except (OSError, UnicodeError):
            return None
        while content.startswith('\ufeff'):
            content = content[1:]
        if not content.strip():
            return None
        first_line = content.split('\n', 1)[0]
        delim = '\t'
        if first_line.count('\t') < 2:
            if first_line.count(',') >= 6:
                delim = ','
            elif first_line.count(';') >= 6:
                delim = ';'
        try:
            reader = csv.reader(content.splitlines(), delimiter=delim)
            rows = list(reader)
        except csv.Error:
            return None
        if not rows:
            return None
        header = [c.replace('\ufeff', '').strip() for c in rows[0]]
        need = ['path', 'line', 'col', 'kind', 'key', 'original', 'translated']
        if not all(n in header for n in need):
            return None
        hmap = {name: i for i, name in enumerate(header)}
        edited = {}
        for row in rows[1:]:
            if not row or not any(str(c).strip() for c in row):
                continue
            row = [_sanitize_field(c) for c in row]
            try:
                path_v = row[hmap['path']]
                line_v = row[hmap['line']]
                col_v = row[hmap['col']]
                kind_v = row[hmap['kind']]
                key_v = row[hmap['key']]
                orig_v = row[hmap['original']]
                trans_v = row[hmap['translated']]
            except IndexError:
                continue
            if not (path_v and orig_v):
                continue
            key = (path_v, line_v, col_v, kind_v, key_v, orig_v)
            edited[key] = trans_v
        return edited if edited else None

    def _read_translation_file(self, path):
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            content = f.read()
        if not content.strip():
            raise ValueError('Файл пустой')
        while content.startswith('\ufeff'):
            content = content[1:]
        first_line = content.split('\n', 1)[0]
        delim = '\t'
        if first_line.count('\t') < 2:
            if first_line.count(',') >= 6:
                delim = ','
            elif first_line.count(';') >= 6:
                delim = ';'
        reader = csv.reader(content.splitlines(), delimiter=delim)
        rows = list(reader)
        if not rows:
            raise ValueError('Нет строк в файле')
        clean_header = [_sanitize_field(c.replace('\ufeff', '')) for c in rows[0]]
        clean_rows = []
        for r in rows[1:]:
            if not r:
                continue
            clean_rows.append([_sanitize_field(c) for c in r])
        return clean_header, clean_rows

    def _try_order_based_import(self, text, mid):
        if not self._last_export_rows:
            messagebox.showerror(
                'Формат не распознан',
                'Файл не является TSV и не похож на формат экспорта.\n\n'
                'Автосопоставление по порядку недоступно: в этой сессии '
                '(и в сохранённом конфиге) нет данных о последнем экспорте.\n\n'
                'Сначала нажмите «Экспорт для перевода», отдайте файл '
                'нейросети, а затем импортируйте результат.')
            return None
        last_mod = self.cfg.get('last_export_mod_id')
        if last_mod and last_mod != mid:
            if not messagebox.askyesno(
                    'Другой мод',
                    'Последний экспорт был для мода {}, а сейчас открыт {}.\n\n'
                    'Продолжить сопоставление по порядку?'.format(
                        last_mod, mid)):
                return None
        lines = text.splitlines()
        clean = []
        skip_headers = {'translated', 'перевод', 'translation',
                        'оригинал', 'original', 'text', 'текст'}
        for ln in lines:
            s = ln.rstrip('\r\n')
            if not s.strip():
                continue
            if s.strip().lower() in skip_headers:
                continue
            clean.append(_sanitize_field(s))
        n_file = len(clean)
        n_export = len(self._last_export_rows)
        if n_file != n_export:
            messagebox.showerror(
                'Количество не совпадает',
                'В файле {} непустых строк, а в последнем экспорте было {}.\n\n'
                'Автосопоставление по порядку невозможно.'.format(
                    n_file, n_export))
            return None
        preview = '\n'.join(clean[:5])
        if not messagebox.askyesno(
                'Импорт по порядку',
                'Файл не является TSV, но содержит {} непустых строк — '
                'столько же, сколько было в последнем экспорте.\n\n'
                'Сопоставить строки по порядку?\n\n'
                'Первые строки файла:\n{}'.format(n_file, preview)):
            return None
        edited = {}
        for i, row in enumerate(self._last_export_rows):
            key = (row[1], row[2], row[3], row[4], row[5], row[6])
            edited[key] = clean[i]
        return edited

    def _apply_translation_import(self, mid, edited):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        with _TSV_LOCK:
            try:
                with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    all_rows = [list(r) for r in csv.reader(f, delimiter='\t')]
                if not all_rows or all_rows[0][:8] != _TSV_HEADER:
                    raise ValueError('Неверный TSV')
                for i in range(1, len(all_rows)):
                    if len(all_rows[i]) >= 8:
                        all_rows[i] = [_sanitize_field(c) for c in all_rows[i][:8]]
                self._backup_tsv_before_edit(tsv)
                mod_idx = [i for i, r in enumerate(all_rows[1:], 1)
                           if len(r) >= 8 and r[0] == mid]
                by_path_orig = {}
                by_path_pos = {}
                by_path_norm = {}
                by_norm_orig = {}
                for i in mod_idx:
                    r = all_rows[i]
                    by_path_orig.setdefault((r[1], r[6]), []).append(i)
                    by_path_pos.setdefault((r[1], r[2], r[3]), []).append(i)
                    by_path_norm.setdefault((_normalize_key_part(r[1]),
                                             _normalize_key_part(r[6])), []).append(i)
                    by_norm_orig.setdefault(_normalize_key_part(r[6]), []).append(i)
                used = set()
                matched_exact = 0
                matched_orig = 0
                matched_pos = 0
                matched_norm = 0
                matched_only_orig = 0
                changed = 0
                unmatched_examples = []
                for key, new_val in edited.items():
                    path, line, col, kind, kfield, orig = key
                    target_idx = None
                    for i in mod_idx:
                        if i in used:
                            continue
                        r = all_rows[i]
                        if (r[1], r[2], r[3], r[4], r[5], r[6]) == key:
                            target_idx = i
                            matched_exact += 1
                            break
                    if target_idx is None:
                        for i in by_path_orig.get((path, orig), []):
                            if i not in used:
                                target_idx = i
                                matched_orig += 1
                                break
                    if target_idx is None:
                        for i in by_path_pos.get((path, line, col), []):
                            if i not in used:
                                target_idx = i
                                matched_pos += 1
                                break
                    if target_idx is None:
                        npath = _normalize_key_part(path)
                        norig = _normalize_key_part(orig)
                        for i in by_path_norm.get((npath, norig), []):
                            if i not in used:
                                target_idx = i
                                matched_norm += 1
                                break
                    if target_idx is None:
                        norig = _normalize_key_part(orig)
                        for i in by_norm_orig.get(norig, []):
                            if i not in used:
                                target_idx = i
                                matched_only_orig += 1
                                break
                    if target_idx is None:
                        if len(unmatched_examples) < 10:
                            unmatched_examples.append((path, line, col, orig))
                        continue
                    used.add(target_idx)
                    r = all_rows[target_idx]
                    cleaned = _sanitize_field(new_val)
                    cleaned = _strip_translated_wrapper_global(orig, cleaned)
                    if r[7] != cleaned:
                        r[7] = cleaned
                        changed += 1
                matched = (matched_exact + matched_orig + matched_pos +
                           matched_norm + matched_only_orig)
                tmp = tsv + '.tmp'
                with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                    csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                               lineterminator='\n').writerows(all_rows)
                os.replace(tmp, tsv)
            except Exception as e:
                messagebox.showerror('Ошибка импорта', str(e))
                return
        if matched == 0:
            sample_file = list(edited.items())[:3]
            sample_master = []
            with _TSV_LOCK:
                with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    r = csv.reader(f, delimiter='\t')
                    next(r, None)
                    for row in r:
                        if len(row) >= 8 and row[0] == mid:
                            sample_master.append(
                                (row[1], row[2], row[3], row[4], row[5], row[6]))
                            if len(sample_master) >= 3:
                                break
            def fmt(k):
                p, ln, cl, kd, kf, og = k
                og_short = og[:80] + ('...' if len(og) > 80 else '')
                return '{} | line={} col={} | {} | {} | {}'.format(
                    p, ln, cl, kd, kf, og_short)
            file_lines = '\n'.join('  ' + fmt(k) for k, _ in sample_file)
            master_lines = '\n'.join('  ' + fmt(k) for k in sample_master)
            messagebox.showwarning(
                'Ничего не совпало',
                'Распознано записей: {}, но ни одна не совпала с текущим '
                'модом {}.\n\n'
                'Примеры из файла импорта:\n{}\n\n'
                'Примеры из master_ru_fixed.tsv:\n{}\n\n'
                'Проверьте, что импортируете в тот же мод, из которого '
                'экспортировали.'.format(
                    len(edited), mid, file_lines or '  (пусто)',
                    master_lines or '  (нет строк для этого мода)'))
            return
        if unmatched_examples:
            self._log('НЕ СОВПАЛО ({} шт., первые 10):'.format(len(unmatched_examples)))
            for (p, ln, cl, og) in unmatched_examples[:10]:
                og_short = og[:90] + ('...' if len(og) > 90 else '')
                self._log('  {} | line={} col={} | {}'.format(p, ln, cl, og_short))
        self._log('Импорт перевода мода {}: всего {}, изменено {}. '
                  'Точных={}, path+orig={}, path+pos={}, по норм.={}, только orig={}.'.format(
                      mid, matched, changed,
                      matched_exact, matched_orig, matched_pos,
                      matched_norm, matched_only_orig))
        self.stat_labels['filled'].configure(text=str(self._count_filled_tsv()))
        self._load_selected_mod_translation(mid)
        extra = ''
        if unmatched_examples:
            extra = ('\n\nНЕ СОВПАЛО: {} шт. (первые — в журнале «Журнал»).'
                     .format(len(unmatched_examples)))
        messagebox.showinfo(
            'Импорт готов',
            'Записей в файле: {}\n'
            'Совпавших строк: {}\n'
            '  точно: {}\n'
            '  по (path + original): {}\n'
            '  по (path + позиция): {}\n'
            '  по нормализованному: {}\n'
            '  только по original: {}\n'
            'Изменено: {}'
            '{}'.format(
                len(edited), matched,
                matched_exact, matched_orig, matched_pos,
                matched_norm, matched_only_orig, changed, extra))

    def _import_current_mod_translation(self):
        if not self.current_mod_id:
            messagebox.showinfo('Мод не выбран', 'Сначала выбери мод.')
            return
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            messagebox.showwarning('Нет TSV',
                                   'Для этого мода ещё не создан TSV.')
            return
        if self._save_after_id:
            try: self.after_cancel(self._save_after_id)
            except Exception: pass
            self._save_after_id = None
        if self._translation_dirty and self.translation_widgets:
            self._save_translation_edits(automatic=True)
        try:
            p = filedialog.askopenfilename(
                title='Выберите переведённый файл',
                initialdir=self.loc_dir,
                filetypes=[('TSV / CSV / текст', '*.tsv *.csv *.txt'),
                           ('TSV', '*.tsv'), ('CSV', '*.csv'), ('Текст', '*.txt'),
                           ('Все файлы', '*.*')])
        except Exception as e:
            self._log('Диалог открытия не сработал: {}'.format(e))
            return
        if not p:
            return
        mid = self.current_mod_id
        edited = self._load_tsv_directly(p)
        if edited:
            self._log('Импорт: распознан как TSV напрямую, {} записей.'.format(
                len(edited)))
        if not edited:
            tmp_tsv = os.path.join(self.loc_dir, '.import_tmp.tsv')
            try:
                fix_tsv(p, tmp_tsv, log_cb=None)
                with open(tmp_tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    r = csv.reader(f, delimiter='\t')
                    header = next(r, None)
                    raw_rows = [row for row in r if row and any(c.strip() for c in row)]
                normalized = []
                for row in raw_rows:
                    row = [_sanitize_field(c.replace('\ufeff', '')) for c in row]
                    if len(row) == 7:
                        row = row[:5] + [''] + row[5:]
                    if len(row) >= 8:
                        normalized.append(row)
                if normalized and any(row[7].strip() for row in normalized):
                    edited = {}
                    for row in normalized:
                        key = (row[1], row[2], row[3], row[4], row[5], row[6])
                        edited[key] = row[7]
                    self._log('Импорт: распознан через fix_tsv, {} записей.'.format(
                        len(edited)))
            except Exception:
                edited = None
            finally:
                try: os.remove(tmp_tsv)
                except Exception: pass
        if not edited:
            try:
                with open(p, 'r', encoding='utf-8-sig', newline='') as f:
                    text = f.read()
            except Exception as e:
                messagebox.showerror('Ошибка чтения', str(e))
                return
            edited = self._try_order_based_import(text, mid)
            if edited is None:
                return
            self._log('Импорт: распознан как обычный текст, {} записей.'.format(
                len(edited)))
        if not edited:
            messagebox.showinfo(
                'Ничего не распознано',
                'Не удалось извлечь переводы из выбранного файла.')
            return
        self._apply_translation_import(mid, edited)

    # ---------- AUTOSAVE / BACKUP / UNDO ----------
    def _translation_changed(self, row, box):
        # Авто-растягивание поля перевода по содержимому.
        try:
            self._autosize_textbox(box, min_lines=2, max_lines=15)
        except Exception:
            pass
        new_text = box.get('1.0', 'end-1c')
        if new_text == row[7]:
            return
        self._translation_dirty = True
        if self._save_after_id:
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.after(800, self._auto_save_translation)

    def _auto_save_translation(self):
        if self._save_after_id:
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
            self._save_after_id = None
        if not self._translation_dirty:
            return
        self._save_translation_edits(automatic=True)

    def _backup_tsv_before_edit(self, tsv):
        if not os.path.isfile(tsv):
            return None
        if self._tsv_undo_backup and os.path.isfile(self._tsv_undo_backup):
            return self._tsv_undo_backup
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = tsv + '.edit_backup_' + stamp
        shutil.copy2(tsv, backup)
        self._tsv_undo_backup = backup
        self.cfg['tsv_undo_backup'] = backup
        self._save_cfg()
        return backup

    def _undo_tsv_edit(self):
        if not self._tsv_undo_backup:
            messagebox.showinfo('Отмена',
                                'Нет последнего сохранения, которое можно отменить.')
            return
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(self._tsv_undo_backup):
            messagebox.showwarning('Backup не найден', self._tsv_undo_backup)
            return
        if not messagebox.askyesno('Отмена сохранения',
                                   'Восстановить TSV до последнего ручного изменения?'):
            return
        try:
            with _TSV_LOCK:
                shutil.copy2(self._tsv_undo_backup, tsv)
            self._tsv_undo_backup = None
            self.cfg['tsv_undo_backup'] = None
            self._translation_dirty = False
            self._save_cfg()
            self._load_selected_mod_translation(self.current_mod_id)
            self._log('TSV восстановлен из бэкапа.')
        except Exception as e:
            messagebox.showerror('Ошибка', str(e))

    def _open_current_mod_folder(self):
        if not self.current_mod_id:
            messagebox.showinfo('Мод не выбран', 'Сначала выбери мод.')
            return
        path = os.path.join(self.mods_root, self.current_mod_id)
        if not os.path.isdir(path):
            messagebox.showwarning('Папка не найдена', path)
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror('Ошибка', str(e))

    def _rescan_current_mod(self):
        if not self.current_mod_id:
            messagebox.showinfo('Мод не выбран', 'Сначала выбери мод.')
            return
        mid = self.current_mod_id
        self._scanned_mods.discard(mid)
        self._scanning_mods.discard(mid)
        self._log('Пересканирование мода {}...'.format(mid))
        self.translation_title.configure(text='Пересканирование...')
        self._schedule_mod_scan(mid, force=True)

    def _apply_current_mod(self):
        if not self.current_mod_id:
            messagebox.showinfo('Мод не выбран', 'Сначала выбери мод.')
            return
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            messagebox.showwarning('Нет TSV', 'Для этого мода нет строк перевода.')
            return
        if self._save_after_id:
            try: self.after_cancel(self._save_after_id)
            except Exception: pass
            self._save_after_id = None
        if self._translation_dirty and self.translation_widgets:
            self._save_translation_edits(automatic=True)
        mid = self.current_mod_id
        if not messagebox.askyesno(
                'Применить мод',
                'Применить переводы мода {}?\n\n'
                'Будет создан бэкап всех изменяемых файлов этого мода.'.format(mid)):
            return
        def work():
            n = backup_all(self.mods_root, tsv,
                           log_cb=lambda s: self.q.put(('log', s)),
                           only_mod=mid)
            self.q.put(('log', 'Бэкапов создано: {}'.format(n)))
            ok, skip, already, files = inject_all(
                self.mods_root, tsv,
                log_cb=lambda s: self.q.put(('log', s)),
                only_mod=mid)
            self.q.put(('log', 'Файлов изменено: {}'.format(files)))
            self.q.put(('log', 'Применено: {}'.format(ok)))
            self.q.put(('log', 'Уже переведено (совпадает с нашим): {}'.format(already)))
            self.q.put(('log', 'Не удалось применить: {}'.format(skip)))
            results, current_values = diag_all(
                self.mods_root, tsv,
                log_cb=lambda s: self.q.put(('log', s)),
                only_mod=mid)
            self.q.put(('diagnostic', (results, current_values)))
            self.q.put(('done', 'Перевод мода применён'))
        self._run_task('Применение мода', lambda _: work(), None)

    def _save_translation_edits(self, automatic=False):
        if not self.current_mod_id or not self.translation_widgets:
            if not automatic:
                self._log('Нет выбранного мода/переводов для сохранения.')
            return
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            if not automatic:
                messagebox.showwarning('Нет TSV', 'Сначала импортируй перевод.')
            return
        edited = {}
        for item in self.translation_widgets:
            row = item['row']
            box = item['box']
            new_text = _sanitize_field(box.get('1.0', 'end-1c'))
            cleaned = _strip_translated_wrapper_global(row[6], new_text)
            if cleaned != row[7]:
                edited[self._row_id(row)] = cleaned
        if not edited:
            self._translation_dirty = False
            if not automatic:
                self._log('Ручных изменений нет.')
            return
        try:
            with _TSV_LOCK:
                with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    reader = csv.reader(f, delimiter='\t')
                    rows = list(reader)
                if not rows or rows[0][:8] != _TSV_HEADER:
                    raise ValueError('Неверный TSV')
                backup = self._backup_tsv_before_edit(tsv)
                changed = 0
                for row in rows[1:]:
                    if len(row) < 8:
                        continue
                    row = [_sanitize_field(c) for c in row[:8]]
                    key = (row[0], row[1], row[2], row[3], row[4], row[5], row[6])
                    if key in edited and row[7] != edited[key]:
                        row[7] = edited[key]
                        changed += 1
                clean_rows = [rows[0][:8]]
                for row in rows[1:]:
                    if len(row) < 8:
                        continue
                    clean_rows.append([_sanitize_field(c) for c in row[:8]])
                tmp = tsv + '.tmp'
                with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                    csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                               lineterminator='\n').writerows(clean_rows)
                os.replace(tmp, tsv)
            if backup and not automatic:
                self._log('Backup TSV создан: {}'.format(os.path.basename(backup)))
            for item in self.translation_widgets:
                raw = _sanitize_field(item['box'].get('1.0', 'end-1c'))
                item['row'][7] = _strip_translated_wrapper_global(
                    item['row'][6], raw)
            self._translation_dirty = False
            if automatic:
                self._log('Автосохранение TSV: изменено {} строк.'.format(changed))
            else:
                self._log('Сохранено ручных правок: {}.'.format(changed))
            self.stat_labels['filled'].configure(text=str(self._count_filled_tsv()))
            self._update_mod_statistics()
        except Exception as e:
            try:
                if os.path.isfile(tsv + '.tmp'):
                    os.remove(tsv + '.tmp')
            except OSError:
                pass
            if not automatic:
                messagebox.showerror('Ошибка сохранения', str(e))
            else:
                self._log('Ошибка автосохранения: {}'.format(e))

    def _count_filled_tsv(self):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        try:
            with _TSV_LOCK:
                with open(tsv, 'r', encoding='utf-8-sig', newline='') as f:
                    r = csv.reader(f, delimiter='\t')
                    next(r, None)
                    return sum(1 for row in r if len(row) >= 8 and row[7].strip())
        except Exception:
            return 0

    # ---------- LOG CONTROLS ----------
    def _edit_action(self, action):
        widget = self.log_box
        try:
            if action == 'copy':
                self._text_copy(widget)
            elif action == 'paste':
                self._text_paste(widget)
            elif action == 'cut':
                self._text_cut(widget)
            elif action == 'select_all':
                self._text_select_all(widget)
            elif action == 'delete':
                self._text_delete(widget)
            elif action == 'undo':
                self._text_undo(widget)
            elif action == 'redo':
                self._text_redo(widget)
            widget.focus_set()
        except Exception:
            pass
        return 'break'

    def _find_log(self):
        q = simpledialog.askstring('Поиск', 'Найти в журнале:', parent=self)
        if not q:
            return
        self.log_box.tag_remove('search_hit', '1.0', 'end')
        pos = '1.0'
        found = 0
        while True:
            pos = self.log_box.search(q, pos, stopindex='end', nocase=True)
            if not pos:
                break
            end = '{}+{}c'.format(pos, len(q))
            self.log_box.tag_add('search_hit', pos, end)
            self.log_box.see(pos)
            pos = end
            found += 1
        self.log_box.tag_configure('search_hit', background='#3a4a68')
        self._log('Поиск «{}»: найдено {}'.format(q, found))

    def _save_log(self):
        try:
            p = filedialog.asksaveasfilename(title='Сохранить журнал', defaultextension='.txt',
                                             filetypes=[('Текст', '*.txt'), ('Все файлы', '*.*')])
        except Exception as e:
            self._log('Диалог не сработал: {}'.format(e))
            return
        if not p:
            return
        try:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(self.log_box.get('1.0', 'end-1c'))
            self._log('Журнал сохранён: {}'.format(p))
        except Exception as e:
            messagebox.showerror('Ошибка', str(e))

    def _refresh_names(self):
        for mid, (row, var, lbl, _) in self.mod_widgets.items():
            new = self.mod_names.get(mid, '')
            if new:
                lbl.configure(text=new)
        self._save_cfg()

    def _filter_mods(self):
        q = self.search_entry.get().strip().lower()
        for mid, (row, var, lbl, name) in self.mod_widgets.items():
            if not q or q in mid.lower() or q in name.lower():
                row.pack(fill='x', pady=2)
            else:
                row.pack_forget()

    def _select_all(self):
        for mid, (r, v, l, n) in self.mod_widgets.items():
            if r.winfo_ismapped(): v.set(True)
        self._update_counts()

    def _select_none(self):
        for mid, (r, v, l, n) in self.mod_widgets.items():
            v.set(False)
        self._update_counts()

    def _invert(self):
        for mid, (r, v, l, n) in self.mod_widgets.items():
            if r.winfo_ismapped(): v.set(not v.get())
        self._update_counts()

    def _update_counts(self):
        total = len(self.mod_widgets)
        sel = sum(1 for _, (r, v, l, n) in self.mod_widgets.items() if v.get())
        self.count_label.configure(text='{} / {}'.format(sel, total))
        self.stat_labels['mods'].configure(text=str(total))
        self.stat_labels['selected'].configure(text=str(sel))

    def _selected_mods(self):
        return [mid for mid, (r, v, l, n) in self.mod_widgets.items() if v.get()]

    # ---------- ACTIONS ----------
    def _scan(self):
        if self.task_running: return
        sel = self._selected_mods()
        if not sel:
            if not messagebox.askyesno('Сканировать все?',
                                       'Ничего не выбрано. Сканировать все моды?'):
                return
            sel = list(self.mod_widgets.keys())
        self._run_task('Сканирование', self._scan_worker, sel)

    def _scan_worker(self, mods):
        total_rows = []
        n = len(mods)
        for i, mid in enumerate(mods, 1):
            self.q.put(('progress', i / n))
            self.q.put(('status', 'Обработка {}/{}: {}'.format(i, n, mid)))
            path = os.path.join(self.mods_root, mid)
            try:
                rows = scan_mod(path)
            except Exception as e:
                self.q.put(('log', '  {}: ошибка {}'.format(mid, e)))
                continue
            self.q.put(('log', '  {:>12}  {:>4} строк'.format(mid, len(rows))))
            for kind, rel, ln, col, key, orig in rows:
                total_rows.append((mid, rel, ln, col, kind, key, orig, ''))
        out = os.path.join(self.loc_dir, 'master.tsv')
        with open(out, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL)
            w.writerow(_TSV_HEADER)
            for r in total_rows:
                w.writerow([_sanitize_field(c) for c in r])
        self.q.put(('log', 'Итого: {} строк -> {}'.format(len(total_rows), out)))
        self.q.put(('stats', (len(total_rows), 0)))
        self.q.put(('done', 'Сканирование завершено'))

    def _open_master(self):
        p = os.path.join(self.loc_dir, 'master.tsv')
        if not os.path.isfile(p):
            messagebox.showwarning('Файл не найден', 'Сначала нажми «Сканировать».')
            return
        try:
            os.startfile(p)
        except Exception as e:
            self._log('Не удалось открыть: {}'.format(e))

    def _import(self):
        try:
            p = filedialog.askopenfilename(
                title='Выберите переведённый TSV',
                initialdir=self.loc_dir,
                filetypes=[('TSV / текст', '*.tsv *.txt'),
                           ('TSV', '*.tsv'), ('Текст', '*.txt'),
                           ('Все файлы', '*.*')])
        except Exception as e:
            self._log('Диалог не сработал: {}'.format(e))
            return
        if not p:
            return
        fixed = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        try:
            ok, skip = fix_tsv(p, fixed, log_cb=self._log)
        except Exception as e:
            messagebox.showerror('Ошибка', str(e))
            return
        with open(fixed, 'r', encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f, delimiter='\t'))[1:]
        bad = sum(1 for r in rows if len(r) != 8)
        filled = sum(1 for r in rows if len(r) >= 8 and r[7].strip())
        self._log('Импорт: {} строк, {} заполнено, {} битых'.format(len(rows), filled, bad))
        self.stat_labels['strings'].configure(text=str(len(rows)))
        self.stat_labels['filled'].configure(text=str(filled))
        messagebox.showinfo('Импорт готов',
                            'Файл: {}\nСтрок: {}\nПереведено: {}\nБитых: {}'.format(
                                os.path.basename(fixed), len(rows), filled, bad))

    def _backup(self):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            messagebox.showwarning('Нет файла', 'Сначала сделай Импорт.')
            return
        if not messagebox.askyesno('Бэкап',
                                   'Создать копии файлов, которые будут изменены?'):
            return
        def work():
            n = backup_all(self.mods_root, tsv,
                           log_cb=lambda s: self.q.put(('log', s)))
            self.q.put(('log', 'Бэкапов создано: {}'.format(n)))
            self.q.put(('done', 'Бэкап завершён'))
        self._run_task('Бэкап', lambda _: work(), None)

    def _inject(self):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            messagebox.showwarning('Нет файла', 'Сначала сделай Импорт.')
            return
        if not messagebox.askyesno('Инжект',
                                   'Применить переводы? Убедись, что сделан бэкап.'):
            return
        def work():
            ok, skip, already, files = inject_all(
                self.mods_root, tsv,
                log_cb=lambda s: self.q.put(('log', s)))
            self.q.put(('log', 'Файлов изменено: {}'.format(files)))
            self.q.put(('log', 'Применено: {}'.format(ok)))
            self.q.put(('log', 'Уже переведено: {}'.format(already)))
            self.q.put(('log', 'Не удалось применить: {}'.format(skip)))
            self.q.put(('done', 'Инжект завершён'))
        self._run_task('Инжект', lambda _: work(), None)

    def _diag(self):
        tsv = os.path.join(self.loc_dir, 'master_ru_fixed.tsv')
        if not os.path.isfile(tsv):
            messagebox.showwarning('Нет файла', 'Сначала сделай Импорт.')
            return
        if not self.current_mod_id:
            messagebox.showwarning('Мод не выбран', 'Сначала выбери мод.')
            return
        mid = self.current_mod_id
        def work():
            results, current_values = diag_all(
                self.mods_root, tsv,
                log_cb=lambda s: self.q.put(('log', s)),
                only_mod=mid)
            self.q.put(('diagnostic', (results, current_values)))
            failed = sum(1 for v in results.values() if v == 'failed')
            applied = sum(1 for v in results.values() if v == 'applied')
            already = sum(1 for v in results.values() if v == 'already')
            empty = sum(1 for v in results.values() if v == 'empty')
            self.q.put(('log',
                        'Диагностика: применено {}, уже другое в файле {}, не применилось {}, пусто {}'.format(
                            applied, already, failed, empty)))
            self.q.put(('done', 'Диагностика завершена'))
        self._run_task('Диагностика', lambda _: work(), None)

    def _run_task(self, name, worker, args):
        if self.task_running:
            messagebox.showinfo('Занято', 'Дождись завершения текущей задачи.')
            return
        self.task_running = True
        self.progress.set(0)
        self._log('── {} ──'.format(name))
        def runner():
            try:
                worker(args)
            except Exception as e:
                self.q.put(('log', 'ОШИБКА: {}'.format(e)))
                self.q.put(('done', 'Ошибка: {}'.format(e)))
        threading.Thread(target=runner, daemon=True).start()

    def _clear_log(self):
        self.log_box.delete('1.0', 'end')

    def _log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_box.insert('end', '[{}] {}\n'.format(ts, msg))
        self.log_box.see('end')

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    self._log(payload)
                elif kind == 'progress':
                    self.progress.set(payload)
                elif kind == 'status':
                    self.status_label.configure(text=payload)
                elif kind == 'stats':
                    strings, filled = payload
                    self.stat_labels['strings'].configure(text=str(strings))
                    self.stat_labels['filled'].configure(text=str(filled))
                elif kind == 'refresh_names':
                    self._refresh_names()
                elif kind == 'scan_result':
                    mid, scanned = payload
                    self._scanning_mods.discard(mid)
                    self._scanned_mods.add(mid)
                    self._merge_scanned_into_tsv(mid, scanned)
                    if self.current_mod_id == mid:
                        self._load_selected_mod_translation(mid)
                elif kind == 'diagnostic':
                    results, current_values = payload
                    self.diagnostic_results = results
                    self.current_file_values = current_values
                    if self.current_mod_id:
                        self._rebuild_problem_list()
                        failed = sum(
                            1 for row in self.translation_rows
                            if self.diagnostic_results.get(self._row_id(row)) == 'failed')
                        if failed:
                            self._set_translation_filter('problem')
                        else:
                            self._rebuild_translation_editor()
                elif kind == 'done':
                    self.status_label.configure(text=payload)
                    self.progress.set(1)
                    self.task_running = False
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def destroy(self):
        self._render_gen += 1
        self._render_cancel = True
        if self._save_after_id:
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
            self._save_after_id = None
        if self._translation_dirty and self.translation_widgets:
            try:
                self._save_translation_edits(automatic=True)
            except Exception:
                pass
        self._save_cfg()
        super().destroy()


if __name__ == '__main__':
    App().mainloop()