#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ファイル仕分け Web アプリ（ローカル専用 / 依存ライブラリなし / macOS 向け）

設定した「整理先フォルダ（root_dir）」と、デスクトップ・ダウンロード等の
散らかりやすい場所のファイルを 1 つずつプレビューしながら、
提示された移動先候補ボタンをクリックして移動していくための簡易サーバ。

使い方:
    python3 server.py
    → http://localhost:8765 をブラウザで開く（自動で開きます）

設定:
    同じフォルダの config.json を編集する（整理先フォルダ・候補推定ルール等）。
    config.json が無ければ ~/Documents を整理先とする汎用デフォルトで起動する。
    詳しくは README.md と config.json を参照。

安全設計:
  - ファイルは「移動(mv)」か「ゴミ箱へ移動」のみ。完全消去（os.remove/rmtree）はしない。
  - 移動先が無ければ作成。同名衝突時は " (2)" を付与してリネーム退避。
  - 全操作を moves.jsonl に追記 → Undo（元に戻す）可能。
  - 設定範囲（root_dir と追加走査元）の外への移動・アクセスは拒否。
  - .logicx / .key / .pages 等の macOS パッケージは「1 個の塊」として扱う。
"""

import json
import os
import re
import shutil
import time
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────────────────────
# 基本パス（環境に依らない）
# ─────────────────────────────────────────────────────────────
HOME = os.path.abspath(os.path.expanduser("~"))
PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
MOVES_LOG = os.path.join(HERE, "moves.jsonl")
CONFIG_PATH = os.path.join(HERE, "config.json")
# 「削除」は完全消去ではなく macOS のゴミ箱へ移動（Finder からもアプリの Undo からも復元可）
TRASH = os.path.abspath(os.path.expanduser("~/.Trash"))

# ─────────────────────────────────────────────────────────────
# 拡張子グループ・除外設定（通常は変更不要）
# ─────────────────────────────────────────────────────────────
# ユーザーが普段扱わない形式。一覧に一切表示しない（追加したい形式はここに足す）。
EXCLUDE_FILE_EXTS = {".rtf"}
# 名前が完全一致したら除外するフォルダ
EXCLUDE_NAME_EXACT = [".Trash", "node_modules", "venv", "dist", "build", ".git"]

# macOS が保護していて移動・削除できない「管理ライブラリ」。仕分け対象から完全に除外する。
PROTECTED_LIBRARY_EXTS = {
    ".photoslibrary", ".photolibrary", ".migratedphotolibrary", ".aplibrary",
    ".musiclibrary", ".tvlibrary", ".itlp",
}

# macOS パッケージ拡張子（中身に潜らず 1 個の塊として扱う）
BUNDLE_EXTS = {
    ".logicx", ".band", ".key", ".pages", ".numbers", ".app", ".rtfd",
    ".photoslibrary", ".fcpbundle", ".imovielibrary", ".tvproj", ".theater",
}

# サブフォルダ内のファイル数がこれを超えたら「確立済みフォルダ」とみなし、
# 中を展開せず “1 つの塊” として扱う（音源ライブラリ・大規模プロジェクト等）。
FOLDER_UNIT_THRESHOLD = 25
SCAN_MAX_DEPTH = 6

# 拡張子グループ
IMAGE = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tiff", ".tif", ".bmp", ".svg"}
VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm"}
AUDIO = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
DOC = {".doc", ".docx", ".pages", ".rtf", ".rtfd", ".odt"}
SHEET = {".xls", ".xlsx", ".csv", ".tsv", ".numbers"}
SLIDE = {".ppt", ".pptx", ".key"}
PDF = {".pdf"}
TEXT = {".txt", ".md", ".markdown", ".log"}
DTM = {".logicx", ".band", ".als", ".flp", ".nki", ".exs", ".mid", ".midi"}
# config.json の ext_rules はこの名前でグループを指定する
EXT_GROUPS = {"IMAGE": IMAGE, "VIDEO": VIDEO, "AUDIO": AUDIO, "DOC": DOC,
              "SHEET": SHEET, "SLIDE": SLIDE, "PDF": PDF, "TEXT": TEXT, "DTM": DTM}

# 「ユーザーが普段扱うファイル」の拡張子（さらに詳しく仕分けの既定フィルタ）。
# .md/.txt/.log・コード・設定ファイル等のノイズはここに含めない＝既定で非表示。
USER_FACING_EXTS = (IMAGE | VIDEO | AUDIO | PDF | DOC | SHEET | SLIDE | DTM | BUNDLE_EXTS | {
    ".zip", ".rar", ".7z", ".dmg", ".epub", ".ai", ".psd", ".xd", ".sketch", ".fig",
})

# ─────────────────────────────────────────────────────────────
# ユーザー設定（同フォルダの config.json で上書き可能）
#   config.json が無ければ以下の汎用デフォルト（~/Documents 整理）で起動。
#   各項目の意味は README.md / config.json のコメントを参照。
# ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # 整理先のルートフォルダ。移動先候補はこの配下のみ提示される。
    "root_dir": "~/Documents",
    # 追加で「片付ける対象」にする場所（移動先候補としては提示しない）。
    # [一覧上の表示名, ホームからの相対 or 絶対パス] の配列。存在する物だけ有効。
    "extra_scan_roots": [
        ["@デスクトップ", "Desktop"],
        ["@ダウンロード", "Downloads"],
        ["@ピクチャ", "Pictures"],
        ["@ムービー", "Movies"],
        ["@ミュージック", "Music"],
    ],
    # root_dir 直下の主要フォルダ（トップ扱い）
    "main_folders": ["Work", "Personal", "Media", "Archive", "_tmp"],
    # キーワード（正規表現） → 推奨移動先（root_dir からの相対パス）
    "keyword_rules": [
        ["invoice|receipt|請求|領収|見積|確定申告|tax", "Work/Finance"],
        ["resume|cv|履歴書|職務経歴", "Work"],
        ["screenshot|スクショ|スクリーンショット|画面収録|画面キャプチャ", "Media/Screenshots"],
        ["旅行|travel|trip|itinerary|しおり|旅程", "Personal/Travel"],
    ],
    # 拡張子グループ（EXT_GROUPS のキー名） → 推奨移動先（弱め）
    "ext_rules": [
        ["IMAGE", ["Media/Photos", "Media"]],
        ["VIDEO", ["Media/Videos", "Media"]],
        ["AUDIO", ["Media/Audio", "Media"]],
        ["DOC", ["Work", "Personal"]],
        ["SHEET", ["Work"]],
        ["SLIDE", ["Work"]],
    ],
    # 種別ごとの「定位置トップ」。ここ以外に在ると「怪しい順」で上位に来る。
    "kind_home_tops": {
        "image": ["Media"],
        "video": ["Media"],
        "audio": ["Media"],
        "dtm": ["Media"],
    },
    # 何があっても 1 個の塊として扱うサブツリー（root_dir からの相対パス）
    "stable_atomic_prefixes": [],
    # 名前にこの文字列を含むフォルダを除外
    "exclude_name_substr": ["Photo Booth"],
    # 名前がこの接頭辞で始まるフォルダを除外（"." は隠しフォルダ）
    "exclude_name_prefix": ["."],
}


def _load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                # "_" 始まりのキー（コメント用）は無視
                cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            print(f"[!] config.json の読み込みに失敗しました（{e}）。既定値で起動します。")
    return cfg


_CFG = _load_config()

# 整理先ルート
ROOT = os.path.abspath(os.path.expanduser(_CFG.get("root_dir", "~/Documents")))
if not os.path.isdir(ROOT):
    print(f"[!] 整理先フォルダが見つかりません: {ROOT}")
    print("    config.json の \"root_dir\" を実在するフォルダに直してください。")

# 追加の走査元（＝片付ける元）。移動ではなく「ここから整理する」フォルダ群。
# rel 上は仮想トップ（@…）で表現し、移動先候補は ROOT 配下のみ提示する。
EXTRA_ROOTS = []
for _pfx, _name in _CFG.get("extra_scan_roots", []):
    _exp = os.path.expanduser(_name)
    _base = os.path.abspath(_exp if os.path.isabs(_exp) else os.path.join(HOME, _name))
    if os.path.isdir(_base) and _base != ROOT:
        EXTRA_ROOTS.append((_pfx, _base))
EXTRA_PREFIXES = [pfx for pfx, _ in EXTRA_ROOTS]
EXTRA_ROOT_PATHS = {b for _, b in EXTRA_ROOTS}

# 主要トップフォルダ
MAINS = list(_CFG.get("main_folders", []))

# キーワード → 推奨トップ（or サブ）相対パス。複数マッチ時はスコア加点。
KEYWORD_RULES = [(pat, dest) for pat, dest in _CFG.get("keyword_rules", [])]

# 拡張子グループ → 推奨先（弱め）
EXT_RULES = [(EXT_GROUPS[g], list(dests))
             for g, dests in _CFG.get("ext_rules", []) if g in EXT_GROUPS]

# 種別 → 定位置トップ
KIND_HOME_TOPS = {k: list(v) for k, v in _CFG.get("kind_home_tops", {}).items()}

# 何があっても塊として扱うサブツリー
STABLE_ATOMIC_PREFIXES = list(_CFG.get("stable_atomic_prefixes", []))

# 候補先・対象から除外するフォルダ
EXCLUDE_NAME_SUBSTR = list(_CFG.get("exclude_name_substr", []))
EXCLUDE_NAME_PREFIX = list(_CFG.get("exclude_name_prefix", ["."]))


# ─────────────────────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────────────────────
def norm(s: str) -> str:
    """正規化（全半角・大小文字を吸収して照合しやすく）"""
    return unicodedata.normalize("NFKC", s).lower()


def is_excluded_name(name: str) -> bool:
    # macOS の listdir は NFD を返すため、必ず NFC に正規化して比較する
    n = unicodedata.normalize("NFC", name)
    for sub in EXCLUDE_NAME_SUBSTR:
        if unicodedata.normalize("NFC", sub) in n:
            return True
    for pre in EXCLUDE_NAME_PREFIX:
        if n.startswith(pre):
            return True
    if n in EXCLUDE_NAME_EXACT:
        return True
    if os.path.splitext(n)[1].lower() in PROTECTED_LIBRARY_EXTS:  # 写真/ミュージック等の保護ライブラリ
        return True
    return False


def is_bundle(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in BUNDLE_EXTS


def _extra_for_rel(rel_path: str):
    for pfx, base in EXTRA_ROOTS:
        if rel_path == pfx or rel_path.startswith(pfx + os.sep):
            return pfx, base
    return None


def _extra_for_path(ap: str):
    for pfx, base in EXTRA_ROOTS:
        if ap == base or ap.startswith(base + os.sep):
            return pfx, base
    return None


def safe_under_root(path: str) -> bool:
    rp = os.path.realpath(path)
    for base in [ROOT] + [b for _, b in EXTRA_ROOTS]:
        if rp == base or rp.startswith(base + os.sep):
            return True
    return False


def rel(path: str) -> str:
    ap = os.path.abspath(path)
    m = _extra_for_path(ap)
    if m:
        pfx, base = m
        sub = os.path.relpath(ap, base)
        r = pfx if sub == "." else os.path.join(pfx, sub)
    else:
        r = os.path.relpath(ap, ROOT)
    return unicodedata.normalize("NFC", r)


def abspath_of(rel_path: str) -> str:
    m = _extra_for_rel(rel_path)
    if m:
        pfx, base = m
        sub = rel_path[len(pfx):].lstrip(os.sep)
        return os.path.normpath(os.path.join(base, sub))
    return os.path.normpath(os.path.join(ROOT, rel_path))


# ─────────────────────────────────────────────────────────────
# スキャン
# ─────────────────────────────────────────────────────────────
def list_all_dirs():
    """候補先になり得るディレクトリ（相対パス）を列挙。バンドル・除外は除く。"""
    out = []
    for dirpath, dirnames, _ in os.walk(ROOT):
        # 除外フィルタ（下位探索も止める）
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded_name(d) and not is_bundle(os.path.join(dirpath, d))
        ]
        for d in dirnames:
            full = os.path.join(dirpath, d)
            r = rel(full)
            depth = r.count(os.sep)
            if depth <= 4:
                out.append(r)
    out.sort()
    return out


def quick_count(path, cap):
    """サブツリーのファイル数を cap+1 まで数えて返す（巨大ツリー対策で打ち切り）。"""
    n = 0
    for dp, dirnames, fns in os.walk(path):
        dirnames[:] = [d for d in dirnames if not is_excluded_name(d)]
        for f in fns:
            if f == ".DS_Store" or f.startswith("._") or f.startswith("~$"):
                continue
            n += 1
            if n > cap:
                return n
    return n


def subtree_changed_since(path, ts, cap=8000):
    """path（ファイル or フォルダ）が ts 以降に変更/追加されたか。
    フォルダはサブツリーを走査し、ts より新しい mtime が1つでもあれば True（見つけ次第打ち切り）。"""
    eps = 1.0  # 浮動小数の揺れ対策
    try:
        if not os.path.isdir(path):
            return os.stat(path).st_mtime > ts + eps
        if os.stat(path).st_mtime > ts + eps:
            return True
    except OSError:
        return False
    n = 0
    for dp, dirnames, fns in os.walk(path):
        dirnames[:] = [d for d in dirnames if not is_excluded_name(d)]
        try:
            if os.stat(dp).st_mtime > ts + eps:
                return True
        except OSError:
            pass
        for f in fns:
            if f == ".DS_Store" or f.startswith("._") or f.startswith("~$"):
                continue
            try:
                if os.stat(os.path.join(dp, f)).st_mtime > ts + eps:
                    return True
            except OSError:
                continue
            n += 1
            if n > cap:           # 大きすぎる場合は打ち切り（変更なし扱い）
                return False
    return False


def is_stable_atomic(rel_path):
    for pre in STABLE_ATOMIC_PREFIXES:
        if rel_path == pre or rel_path.startswith(pre + os.sep) or pre.startswith(rel_path + os.sep):
            return True
    return False


def scan_files():
    """対象アイテムを列挙。
    - 単独ファイルは 1 件。
    - バンドル(.logicx 等) / 大きいフォルダ / 確立済みサブツリー は『塊』として 1 件。
    """
    items = []

    def walk(dirpath, depth):
        try:
            entries = os.listdir(dirpath)
        except OSError:
            return
        for name in entries:
            if name == ".DS_Store" or name.startswith("._") or name.startswith("~$"):
                continue
            full = os.path.join(dirpath, name)
            if is_excluded_name(name):
                continue
            if os.path.isdir(full):
                r = rel(full)
                if is_bundle(full):
                    items.append(make_item(full, is_dir=True))
                    continue
                # 塊として扱うか判定（depth>=2 = main 配下のサブフォルダから）
                collapse = False
                if is_stable_atomic(r):
                    collapse = True
                elif dirpath in EXTRA_ROOT_PATHS:  # 追加走査元の直下フォルダは丸ごと1単位
                    collapse = True
                elif depth >= SCAN_MAX_DEPTH:
                    collapse = True
                elif depth >= 1:
                    cnt = quick_count(full, FOLDER_UNIT_THRESHOLD)
                    if cnt > FOLDER_UNIT_THRESHOLD:
                        collapse = True
                if collapse:
                    items.append(make_item(full, is_dir=True))
                else:
                    walk(full, depth + 1)
            elif os.path.splitext(name)[1].lower() not in EXCLUDE_FILE_EXTS:
                items.append(make_item(full, is_dir=False))

    walk(ROOT, 0)
    for _pfx, base in EXTRA_ROOTS:
        walk(base, 1)                # 追加走査元の直下: ファイルは個別・フォルダは塊

    # 空フォルダ（＝新規作成したフォルダ等）は、塊に畳まれた中にあっても 1 件として表示する。
    # ここで拾うのは「真に空（ディスク上に何も無い）」フォルダのみ → 一括削除と一致する。
    by_rel = {it["rel"]: it for it in items}
    for base in ([ROOT] + [b for _, b in EXTRA_ROOTS]):
        for d in find_empty_dirs(base):
            r = rel(d)
            if r in by_rel:
                by_rel[r]["empty"] = True   # 既に塊として登録済みでも空フラグを立てる
            else:
                it = make_item(d, is_dir=True)
                it["empty"] = True          # 一括削除の対象フラグ
                items.append(it)
                by_rel[r] = it
    return items


def find_empty_dirs(base, cap=200000):
    """base 配下の『真に空のフォルダ』（ディスク上に実体が何も無い末端）を列挙。
    塊に畳まれる大きいフォルダの中にある空フォルダも拾うための別パス。
    判定は厳密：サブフォルダが1つでもあれば（除外・隠し・保護対象でも）空とみなさない
    → 実際にゴミ箱へ移せる物だけを返す（一覧表示と一括削除を一致させる）。"""
    out = []
    seen = 0
    for dp, dirnames, fns in os.walk(base):
        raw_dirs = list(dirnames)        # 枝刈り前のサブフォルダ全部（空判定に使う）
        # 走査の枝刈り（巨大/保護領域には潜らない）。空判定は raw_dirs で行う。
        dirnames[:] = [
            d for d in raw_dirs
            if not is_excluded_name(d)
            and not is_bundle(os.path.join(dp, d))
            and not is_stable_atomic(rel(os.path.join(dp, d)))
        ]
        seen += 1
        if seen > cap:
            break
        if dp == base:
            continue
        real = [f for f in fns if not (f == ".DS_Store" or f.startswith("._") or f.startswith("~$"))]
        if not real and not raw_dirs:    # 実ファイル0 かつ サブフォルダ0 ＝ 真に空
            out.append(dp)
    return out


def is_excluded_name_path(full: str) -> bool:
    r = rel(full)
    for part in r.split(os.sep):
        if is_excluded_name(part):
            return True
    return False


def list_dir(rel_path, max_entries=400):
    """フォルダの直下の中身を一覧で返す（削除前に中身を確認するため）。"""
    path = abspath_of(rel_path)
    if not safe_under_root(path) or not os.path.isdir(path):
        return {"error": "フォルダが見つかりません", "entries": [], "total": 0}
    try:
        names = sorted(os.listdir(path), key=lambda s: unicodedata.normalize("NFC", s).lower())
    except OSError as e:
        return {"error": str(e), "entries": [], "total": 0}
    entries = []
    more = False
    for name in names:
        if name == ".DS_Store" or name.startswith("._") or name.startswith("~$"):
            continue
        full = os.path.join(path, name)
        isd = os.path.isdir(full)
        if not isd and os.path.splitext(name)[1].lower() in EXCLUDE_FILE_EXTS:
            continue
        try:
            st = os.stat(full)
            size = 0 if isd else st.st_size
        except OSError:
            size = 0
        ent = {"name": unicodedata.normalize("NFC", name), "rel": rel(full),
               "is_dir": isd, "size": size, "ext": os.path.splitext(name)[1].lower()}
        if isd:
            c = quick_count(full, 500)
            ent["filecount"] = c
            ent["capped"] = c > 500
        entries.append(ent)
        if len(entries) >= max_entries:
            more = True
            break
    total = quick_count(path, 5000)
    return {
        "rel": rel(path),
        "entries": entries,
        "total": total,
        "capped": total > 5000,
        "more": more,
    }


def list_all_files(rel_path, cap=2000, types="all", min_age_days=0):
    """フォルダ配下の『全ファイル』を再帰的に列挙（階層を無視して1つずつ仕分け用）。
    types="common" で『よく使うファイル』のみ、min_age_days>0 で最終利用が古い物のみに絞る。"""
    base = abspath_of(rel_path)
    if not safe_under_root(base) or not os.path.isdir(base):
        return {"error": "フォルダが見つかりません", "files": [], "total": 0, "capped": False}
    files = []
    capped = False
    now = time.time()
    min_age = max(0, min_age_days) * 86400

    def add(full):
        is_d = os.path.isdir(full)
        ext = os.path.splitext(full)[1].lower()
        if not is_d and ext in EXCLUDE_FILE_EXTS:   # 普段扱わない形式は出さない
            return False
        # 種類フィルタはファイルのみに適用（フォルダ/パッケージは常に1単位として出す）
        if types == "common" and not is_d and ext not in USER_FACING_EXTS:
            return False
        try:
            st = os.stat(full)
        except OSError:
            return False
        last = max(st.st_mtime, st.st_atime)   # 最終更新 or 最終アクセスの新しい方
        if min_age and (now - last) < min_age:
            return False
        sub = os.path.dirname(os.path.relpath(full, base))
        files.append({
            "name": unicodedata.normalize("NFC", os.path.basename(full)),
            "rel": rel(full),
            "ext": ext,
            "size": 0 if is_d else st.st_size,
            "sub": unicodedata.normalize("NFC", sub),
            "last": last,
            "is_dir": is_d,
        })
        return len(files) >= cap

    for dp, dirnames, fns in os.walk(base):
        raw_dirs = list(dirnames)        # 枝刈り前のサブフォルダ全部（空判定に使う）
        kept = []
        for d in sorted(dirnames):
            if is_excluded_name(d):
                continue
            full = os.path.join(dp, d)
            if is_bundle(full):          # .app/.logicx/.key 等は中に潜らず1単位
                if add(full):
                    capped = True
                    break
            else:
                kept.append(d)
        dirnames[:] = kept
        if capped:
            break
        real_files = [f for f in sorted(fns)
                      if not (f == ".DS_Store" or f.startswith("._") or f.startswith("~$"))]
        # 真に空（サブフォルダ0・実ファイル0）のフォルダだけを1件として表示
        if dp != base and not real_files and not raw_dirs:
            if add(dp):
                capped = True
                break
        for f in real_files:
            if add(os.path.join(dp, f)):
                capped = True
                break
        if capped:
            break
    return {"files": files, "total": len(files), "capped": capped}


def make_item(full: str, is_dir: bool):
    filecount = 0
    try:
        st = os.stat(full)
        size = 0 if is_dir else st.st_size
        mtime = st.st_mtime
        if is_dir:
            filecount = quick_count(full, 500)  # 表示用（500+ で打ち切り）
    except OSError:
        size, mtime = 0, 0
    r = rel(full)
    name = os.path.basename(full)
    ext = os.path.splitext(name)[1].lower()
    parent = os.path.dirname(r)
    top = r.split(os.sep)[0] if os.sep in r else r
    return {
        "rel": r,
        "name": name,
        "ext": ext,
        "size": size,
        "filecount": filecount,
        "mtime": mtime,
        "parent": parent,
        "top": top,
        "is_dir": is_dir,
    }


# ─────────────────────────────────────────────────────────────
# 候補ロジック
# ─────────────────────────────────────────────────────────────
def kind_of(ext: str) -> str:
    if ext in IMAGE:
        return "image"
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in PDF:
        return "pdf"
    if ext in TEXT:
        return "text"
    if ext in DOC:
        return "doc"
    if ext in SHEET:
        return "sheet"
    if ext in SLIDE:
        return "slide"
    if ext in DTM:
        return "dtm"
    return "other"


def dir_leaf_tokens(rel_dir: str):
    """ディレクトリ名から照合トークンを作る（記号・連番を除去）"""
    leaf = rel_dir.split(os.sep)[-1]
    leaf = re.sub(r"^#?\d+[_\-\s]*", "", leaf)      # 先頭の #1_ / 01_ を除去
    leaf = re.sub(r"^[_\-]+", "", leaf)
    return norm(leaf)


def score_dir(file_item, rel_dir, current_parent):
    """ファイルに対する 1 ディレクトリのスコア。"""
    if rel_dir == current_parent:
        return -1, []  # 現在地は候補にしない（別途「留める」がある）
    score = 0
    reasons = []
    fname = norm(file_item["name"])
    fparent = norm(file_item["parent"])
    hay = fname + " " + fparent
    ext = file_item["ext"]
    kind = kind_of(ext)

    # 1) ディレクトリ名そのものがファイル名に含まれる（最強）
    leaf = dir_leaf_tokens(rel_dir)
    if len(leaf) >= 2 and leaf in hay:
        score += 12 + min(len(leaf), 8)
        reasons.append(f"名称一致『{rel_dir.split(os.sep)[-1]}』")

    # 2) キーワードルール
    for pat, dest in KEYWORD_RULES:
        if re.search(pat, hay):
            if rel_dir == dest:
                score += 9
                reasons.append("キーワード一致")
            elif rel_dir.startswith(dest + os.sep) or dest.startswith(rel_dir + os.sep):
                score += 5
                reasons.append("キーワード系統")
            elif rel_dir.split(os.sep)[0] == dest.split(os.sep)[0]:
                score += 2

    # 3) 拡張子ルール
    for exts, dests in EXT_RULES:
        if ext in exts:
            for i, d in enumerate(dests):
                if rel_dir == d:
                    score += 6 - i
                    reasons.append(f"{kind}向き")
                elif rel_dir.split(os.sep)[0] == d.split(os.sep)[0]:
                    score += 1

    return score, reasons


def candidates_for(file_item, all_dirs, top_n=7):
    cur = file_item["parent"]
    scored = []
    for d in all_dirs:
        s, reasons = score_dir(file_item, d, cur)
        if s > 0:
            scored.append((s, d, reasons))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    out = [{"dir": d, "score": s, "why": "・".join(dict.fromkeys(reasons))} for s, d, reasons in scored[:top_n]]

    # 主要トップを最低限フォールバックとして足す（重複除外）
    have = {c["dir"] for c in out}
    if len(out) < 4:
        for m in MAINS:
            if m not in have and m != cur:
                out.append({"dir": m, "score": 0, "why": ""})
                have.add(m)
            if len(out) >= 5:
                break
    return out


# ─────────────────────────────────────────────────────────────
# 状態（レビュー済み / 移動履歴）
# ─────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("reviewed", [])
            s.setdefault("moves", [])
            s.setdefault("reviewed_at", {})   # rel -> 確認した時刻(epoch)
            return s
        except Exception:
            pass
    return {"reviewed": [], "moves": [], "reviewed_at": {}}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


STATE = load_state()


def log_move(entry):
    with open(MOVES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def do_move(src_rel, dest_dir_rel, make_dir=True):
    src = abspath_of(src_rel)
    dest_dir = abspath_of(dest_dir_rel)
    if not safe_under_root(src) or not os.path.exists(src):
        raise ValueError("移動元が見つかりません: " + src_rel)
    if not safe_under_root(dest_dir):
        raise ValueError("移動先が ROOT 外です")
    # 自分自身/配下への移動を防ぐ
    if os.path.realpath(dest_dir) == os.path.realpath(src) or \
       os.path.realpath(dest_dir).startswith(os.path.realpath(src) + os.sep):
        raise ValueError("自分自身/配下へは移動できません")
    if not os.path.isdir(dest_dir):
        if make_dir:
            os.makedirs(dest_dir, exist_ok=True)
        else:
            raise ValueError("移動先フォルダがありません")
    name = os.path.basename(src)
    target = os.path.join(dest_dir, name)
    if os.path.exists(target):
        base, ext = os.path.splitext(name)
        i = 2
        while os.path.exists(os.path.join(dest_dir, f"{base} ({i}){ext}")):
            i += 1
        target = os.path.join(dest_dir, f"{base} ({i}){ext}")
    try:
        shutil.move(src, target)
    except PermissionError:
        raise ValueError("macOS に保護されているため操作できませんでした（写真・ミュージック等のライブラリは仕分け対象外です）。")
    entry = {
        "ts": time.time(),
        "action": "move",
        "from": src_rel,
        "to": rel(target),
    }
    STATE["moves"].append(entry)
    if src_rel in STATE["reviewed"]:
        STATE["reviewed"].remove(src_rel)
    STATE.get("reviewed_at", {}).pop(src_rel, None)
    STATE["reviewed"].append(rel(target))
    STATE.setdefault("reviewed_at", {})[rel(target)] = time.time()
    save_state(STATE)
    log_move(entry)
    return rel(target)


def do_review(src_rel):
    if src_rel not in STATE["reviewed"]:
        STATE["reviewed"].append(src_rel)
    STATE.setdefault("reviewed_at", {})[src_rel] = time.time()   # この時刻以降の変更で再表示
    STATE["moves"].append({"ts": time.time(), "action": "review", "rel": src_rel})
    save_state(STATE)


def _is_empty_dir(path):
    """中身が無い（.DS_Store 等の雑多ファイルのみ）フォルダか。"""
    try:
        for name in os.listdir(path):
            if name == ".DS_Store" or name.startswith("._") or name.startswith("~$"):
                continue
            return False
        return True
    except OSError:
        return False


def trash_empty_dirs():
    """ROOT・追加走査元 配下の『真に空のフォルダ』をすべてゴミ箱へ移動。移動した rel のリストを返す。
    空フォルダを消すと親が空になる入れ子に備えて、空が無くなるまで繰り返す。"""
    done = []
    for _ in range(20):
        targets = []
        for base in [ROOT] + [b for _, b in EXTRA_ROOTS]:
            targets += find_empty_dirs(base)
        progressed = False
        for d in targets:
            if not os.path.isdir(d) or not _is_empty_dir(d):
                continue
            try:
                r = rel(d)
                do_trash(r)
                done.append(r)
                progressed = True
            except Exception:
                continue
        if not progressed:
            break
    return done


def do_trash(src_rel):
    """ファイル/塊を macOS のゴミ箱(~/.Trash)へ移動する（完全消去はしない）。"""
    src = abspath_of(src_rel)
    if not safe_under_root(src) or not os.path.exists(src):
        raise ValueError("対象が見つかりません: " + src_rel)
    os.makedirs(TRASH, exist_ok=True)
    name = os.path.basename(src)
    target = os.path.join(TRASH, name)
    if os.path.exists(target):
        base, ext = os.path.splitext(name)
        i = 2
        while os.path.exists(os.path.join(TRASH, f"{base} ({i}){ext}")):
            i += 1
        target = os.path.join(TRASH, f"{base} ({i}){ext}")
    try:
        shutil.move(src, target)
    except PermissionError:
        raise ValueError("macOS に保護されているため操作できませんでした（写真・ミュージック等のライブラリは仕分け対象外です）。")
    entry = {
        "ts": time.time(),
        "action": "trash",
        "from": src_rel,
        "trash_abs": target,   # ゴミ箱は ROOT 外なので絶対パスで保持
    }
    STATE["moves"].append(entry)
    if src_rel in STATE["reviewed"]:
        STATE["reviewed"].remove(src_rel)
    save_state(STATE)
    log_move(entry)
    return target


def do_rename(src_rel, new_name):
    """同じフォルダ内でファイル/フォルダ名を変更する（移動はしない）。"""
    src = abspath_of(src_rel)
    if not safe_under_root(src) or not os.path.exists(src):
        raise ValueError("対象が見つかりません: " + src_rel)
    new_name = unicodedata.normalize("NFC", (new_name or "").strip())
    if not new_name or "/" in new_name or os.sep in new_name or new_name in (".", ".."):
        raise ValueError("ファイル名が不正です（/ は使えません）")
    # 拡張子を省略されたら元の拡張子を維持（うっかり拡張子を消すのを防ぐ）
    orig_ext = os.path.splitext(os.path.basename(src))[1]
    if os.path.splitext(new_name)[1] == "" and orig_ext:
        new_name += orig_ext
    parent = os.path.dirname(src)
    target = os.path.join(parent, new_name)
    if os.path.realpath(target) == os.path.realpath(src):
        return src_rel  # 変更なし
    if os.path.exists(target):
        base, ext = os.path.splitext(new_name)
        i = 2
        while os.path.exists(os.path.join(parent, f"{base} ({i}){ext}")):
            i += 1
        target = os.path.join(parent, f"{base} ({i}){ext}")
    try:
        os.rename(src, target)
    except PermissionError:
        raise ValueError("macOS に保護されているため操作できませんでした（写真・ミュージック等のライブラリは仕分け対象外です）。")
    entry = {"ts": time.time(), "action": "rename", "from": src_rel, "to": rel(target)}
    STATE["moves"].append(entry)
    if src_rel in STATE["reviewed"]:   # 確認済みだったら新名で引き継ぐ
        STATE["reviewed"].remove(src_rel)
        STATE["reviewed"].append(rel(target))
    save_state(STATE)
    log_move(entry)
    return rel(target)


def do_undo():
    # 直近の move / trash / rename / review を 1 件取り消す
    for i in range(len(STATE["moves"]) - 1, -1, -1):
        m = STATE["moves"][i]
        if m["action"] == "rename":
            cur = abspath_of(m["to"])
            if not os.path.exists(cur):
                STATE["moves"].pop(i)
                save_state(STATE)
                continue
            back = abspath_of(m["from"])
            if os.path.exists(back):
                base, ext = os.path.splitext(os.path.basename(m["from"]))
                d = os.path.dirname(back)
                k = 2
                while os.path.exists(os.path.join(d, f"{base} ({k}){ext}")):
                    k += 1
                back = os.path.join(d, f"{base} ({k}){ext}")
            os.rename(cur, back)
            if m["to"] in STATE["reviewed"]:
                STATE["reviewed"].remove(m["to"])
                STATE["reviewed"].append(rel(back))
            STATE["moves"].pop(i)
            save_state(STATE)
            log_move({"ts": time.time(), "action": "undo_rename", "restored": rel(back)})
            return {"undone": "rename", "restored": rel(back)}
        if m["action"] == "trash":
            cur = m.get("trash_abs")
            if not cur or not os.path.exists(cur):
                STATE["moves"].pop(i)
                save_state(STATE)
                continue
            back_dir = abspath_of(os.path.dirname(m["from"]) or ".")
            os.makedirs(back_dir, exist_ok=True)
            back_target = os.path.join(back_dir, os.path.basename(m["from"]))
            if os.path.exists(back_target):
                base, ext = os.path.splitext(os.path.basename(m["from"]))
                k = 2
                while os.path.exists(os.path.join(back_dir, f"{base} ({k}){ext}")):
                    k += 1
                back_target = os.path.join(back_dir, f"{base} ({k}){ext}")
            shutil.move(cur, back_target)
            STATE["moves"].pop(i)
            save_state(STATE)
            log_move({"ts": time.time(), "action": "undo_trash", "restored": rel(back_target)})
            return {"undone": "trash", "restored": rel(back_target)}
        if m["action"] == "move":
            cur = abspath_of(m["to"])
            back_dir = abspath_of(os.path.dirname(m["from"]) or ".")
            if not os.path.exists(cur):
                STATE["moves"].pop(i)
                continue
            os.makedirs(back_dir, exist_ok=True)
            back_target = os.path.join(back_dir, os.path.basename(m["from"]))
            shutil.move(cur, back_target)
            if m["to"] in STATE["reviewed"]:
                STATE["reviewed"].remove(m["to"])
            STATE["moves"].pop(i)
            save_state(STATE)
            log_move({"ts": time.time(), "action": "undo_move", "restored": m["from"], "from": m["to"]})
            return {"undone": "move", "restored": m["from"]}
        if m["action"] == "review":
            if m["rel"] in STATE["reviewed"]:
                STATE["reviewed"].remove(m["rel"])
            STATE["moves"].pop(i)
            save_state(STATE)
            return {"undone": "review", "rel": m["rel"]}
    return {"undone": None}


# ─────────────────────────────────────────────────────────────
# HTTP ハンドラ
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静かに

    # --- helpers ---
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not safe_under_root(path) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = guess_type(path)
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                self.wfile.write(f.read(length))
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def _read_json_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # --- GET ---
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p == "/" or p == "/index.html":
            self._serve_index()
            return
        if p == "/api/scan":
            self._api_scan()
            return
        if p == "/api/dirs":
            self._json({"dirs": list_all_dirs()})
            return
        if p == "/api/list":
            rp = q.get("rel", [""])[0]
            self._json(list_dir(rp))
            return
        if p == "/api/listall":
            rp = q.get("rel", [""])[0]
            types = q.get("types", ["all"])[0]
            try:
                age = int(q.get("age", ["0"])[0])
            except ValueError:
                age = 0
            self._json(list_all_files(rp, types=types, min_age_days=age))
            return
        if p == "/api/candidates":
            rp = q.get("rel", [""])[0]
            path = abspath_of(rp)
            if not safe_under_root(path) or not os.path.exists(path):
                self._json({"candidates": []})
                return
            item = make_item(path, os.path.isdir(path))
            item["kind"] = kind_of(item["ext"])
            item["candidates"] = candidates_for(item, list_all_dirs())
            self._json({"candidates": item["candidates"], "item": item})
            return
        if p == "/api/iwork-preview":
            rp = q.get("rel", [""])[0]
            path = abspath_of(rp)
            data = iwork_preview(path) if safe_under_root(path) else None
            if not data:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/raw":
            rp = q.get("rel", [""])[0]
            self._send_file(abspath_of(rp))
            return
        if p == "/api/text":
            rp = q.get("rel", [""])[0]
            self._api_text(rp)
            return
        self.send_error(404)

    # --- POST ---
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/move":
                body = self._read_json_body()
                new_rel = do_move(body["from"], body["dir"], make_dir=body.get("mkdir", True))
                self._json({"ok": True, "to": new_rel,
                            "reviewed": len(STATE["reviewed"]), "moves": len(STATE["moves"])})
                return
            if u.path == "/api/review":
                body = self._read_json_body()
                do_review(body["rel"])
                self._json({"ok": True, "reviewed": len(STATE["reviewed"])})
                return
            if u.path == "/api/trash":
                body = self._read_json_body()
                do_trash(body["from"])
                self._json({"ok": True, "moves": len(STATE["moves"])})
                return
            if u.path == "/api/trash-empty":
                done = trash_empty_dirs()
                self._json({"ok": True, "count": len(done), "trashed": done})
                return
            if u.path == "/api/rename":
                body = self._read_json_body()
                new_rel = do_rename(body["from"], body["name"])
                self._json({"ok": True, "to": new_rel})
                return
            if u.path == "/api/undo":
                res = do_undo()
                self._json({"ok": True, **res})
                return
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, code=400)
            return
        self.send_error(404)

    # --- API 実装 ---
    def _serve_index(self):
        idx = os.path.join(HERE, "index.html")
        with open(idx, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_scan(self):
        all_dirs = list_all_dirs()
        items = scan_files()
        reviewed = set(STATE["reviewed"])
        reviewed_at = STATE.get("reviewed_at", {})
        unreview = []   # 確認後に変更されたので確認済みを解除する rel
        for it in items:
            rv = it["rel"] in reviewed
            # 確認後に中身が変更/追加されたフォルダ・ファイルは再表示する
            if rv and it["rel"] in reviewed_at:
                ts = reviewed_at[it["rel"]]
                if it["is_dir"]:
                    changed = subtree_changed_since(abspath_of(it["rel"]), ts)
                else:
                    changed = it["mtime"] > ts + 1
                if changed:
                    rv = False
                    unreview.append(it["rel"])
            it["reviewed"] = rv
            it["kind"] = kind_of(it["ext"])
            it["candidates"] = candidates_for(it, all_dirs)
            # 現在地が拡張子/キーワードと食い違う＝怪しい度
            it["suspect"] = compute_suspect(it)
        if unreview:
            for r in unreview:
                if r in STATE["reviewed"]:
                    STATE["reviewed"].remove(r)
                reviewed_at.pop(r, None)
            save_state(STATE)
            reviewed = set(STATE["reviewed"])
        present = [pfx for pfx in EXTRA_PREFIXES if any(it["top"] == pfx for it in items)]
        self._json({
            "root": ROOT,
            "count": len(items),
            "reviewed": len(reviewed),
            "items": items,
            "dirs": all_dirs,
            "mains": MAINS,
            "tops": MAINS + present,
        })

    def _api_text(self, rp):
        path = abspath_of(rp)
        if not safe_under_root(path) or not os.path.isfile(path):
            self._json({"text": "", "error": "not found"})
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in (".docx", ".pptx"):
            self._json({"text": extract_office_text(path)})
            return
        if ext == ".xlsx":
            self._json({"text": extract_xlsx_text(path)})
            return
        try:
            with open(path, "rb") as f:
                raw = f.read(8000)
            txt = raw.decode("utf-8", errors="replace")
        except Exception as e:
            txt = f"(プレビュー不可: {e})"
        self._json({"text": txt})


def compute_suspect(it):
    """現在地と中身が食い違っていそうなら高スコア（怪しい順ソート用）"""
    kind = it["kind"]
    top = it["top"]
    s = 0
    # 種別の「定位置トップ」(config: kind_home_tops) 以外に在るファイルは要確認
    homes = KIND_HOME_TOPS.get(kind)
    if homes is not None and top not in homes:
        s += 3 if kind in ("image", "video") else 2
    # トップに直置き（サブフォルダ無し）も要確認
    if it["parent"] in ("", ".") or it["parent"] in MAINS:
        s += 1
    # 一時フォルダ直下は優先的に片付け対象
    if any(t in str(it["parent"]) for t in ("_tmp", "_一時")):
        s += 4
    if it["top"] in EXTRA_PREFIXES:     # 追加走査元（DL/デスクトップ等）は片付け対象として上位に
        s += 3
    return s


def extract_office_text(path, limit=20000):
    """.docx / .pptx の本文テキストを stdlib だけで抽出（プレビュー用）。
    OOXML は ZIP の中の XML。<w:t>/<a:t> のテキストを段落区切り付きで拾う。"""
    import zipfile
    import html
    ext = os.path.splitext(path)[1].lower()
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return f"(プレビュー不可: {e})"
    try:
        names = z.namelist()
        if ext == ".docx":
            targets = ["word/document.xml"]
            para_tag = "w:p"
        elif ext == ".pptx":
            slides = [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            targets = sorted(slides, key=lambda s: int(re.search(r"(\d+)", s).group(1)))
            para_tag = "a:p"
        else:
            return "(この形式は本文抽出に未対応)"
        parts = []
        for t in targets:
            try:
                xml = z.read(t).decode("utf-8", "replace")
            except KeyError:
                continue
            xml = re.sub(r"<" + para_tag + r"\b[^>]*>", "\n", xml)  # 段落タグごと改行に
            xml = re.sub(r"<[wa]:tab[ /]*>", "\t", xml)
            xml = re.sub(r"<[wa]:br[ /]*>", "\n", xml)
            txt = re.sub(r"<[^>]+>", "", xml)                   # 残りのタグを除去
            parts.append(html.unescape(txt))
            if sum(len(p) for p in parts) > limit:
                break
    finally:
        z.close()
    text = "\n".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] or "(本文なし)"


def extract_xlsx_text(path, max_rows=60, max_cols=20, limit=20000):
    """.xlsx の各シートをタブ区切りテキストに復元（stdlib のみ）。
    文字列は xl/sharedStrings.xml に分離格納され、セルは index 参照する構造。"""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

    def col_idx(ref):
        m = re.match(r"([A-Z]+)", ref or "")
        if not m:
            return None
        n = 0
        for ch in m.group(1):
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return f"(プレビュー不可: {e})"
    try:
        names = z.namelist()
        def si_text(si):
            # 直下の <t> と リッチテキスト <r><t> だけ拾う。
            # ふりがな <rPh>（ルビ）内の <t> は本文ではないので除外。
            parts = []
            for child in si:
                if child.tag == NS + "t":
                    parts.append(child.text or "")
                elif child.tag == NS + "r":
                    for t in child.findall(NS + "t"):
                        parts.append(t.text or "")
            return "".join(parts)

        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(NS + "si"):
                shared.append(si_text(si))
        sheet_files = sorted(
            [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)],
            key=lambda s: int(re.search(r"(\d+)", s).group(1)),
        )
        out = []
        for sf in sheet_files:
            root = ET.fromstring(z.read(sf))
            sd = root.find(NS + "sheetData")
            if sd is None:
                continue
            out.append(f"# {sf.split('/')[-1]}")
            rcount = 0
            for row in sd.findall(NS + "row"):
                rowmap = {}
                maxc = -1
                for c in row.findall(NS + "c"):
                    ci = col_idx(c.get("r"))
                    if ci is None or ci >= max_cols:
                        continue
                    t = c.get("t")
                    v = c.find(NS + "v")
                    val = ""
                    if t == "s":
                        if v is not None and v.text is not None:
                            idx = int(v.text)
                            val = shared[idx] if 0 <= idx < len(shared) else ""
                    elif t == "inlineStr":
                        is_ = c.find(NS + "is")
                        if is_ is not None:
                            val = "".join(tt.text or "" for tt in is_.iter(NS + "t"))
                    elif v is not None:
                        val = v.text or ""
                    rowmap[ci] = val
                    if ci > maxc:
                        maxc = ci
                cells = [rowmap.get(i, "") for i in range(maxc + 1)]
                out.append("\t".join(cells))
                rcount += 1
                if rcount >= max_rows:
                    out.append(f"…（{sf.split('/')[-1]} は {max_rows} 行で打ち切り）")
                    break
            if sum(len(x) for x in out) > limit:
                break
    finally:
        z.close()
    return ("\n".join(out)[:limit]).strip() or "(空)"


# iWork（.key/.pages/.numbers）に埋め込まれたプレビューの探索順（画像優先）
IWORK_PREVIEW_MEMBERS = [
    "preview.jpg", "preview-web.jpg", "preview-micro.jpg",
    "QuickLook/Thumbnail.jpg",
]


def iwork_preview(path):
    """iWork ファイルに埋め込まれたプレビュー画像(JPEG)を取り出す。
    .key/.pages/.numbers は本文が独自バイナリ(IWA)なので、Keynote 等が同梱する
    プレビュー画像で代替する。返り値: bytes or None。"""
    import zipfile
    if os.path.isdir(path):          # パッケージ（フォルダ）形式
        for m in IWORK_PREVIEW_MEMBERS:
            cand = os.path.join(path, *m.split("/"))
            if os.path.isfile(cand):
                try:
                    with open(cand, "rb") as f:
                        return f.read()
                except OSError:
                    continue
        return None
    try:                              # フラットファイル（zip）形式
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for m in IWORK_PREVIEW_MEMBERS:
                if m in names:
                    return z.read(m)
    except Exception:
        return None
    return None


def guess_type(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".heic": "image/heic", ".bmp": "image/bmp", ".tiff": "image/tiff",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    }.get(ext, "application/octet-stream")


def main():
    if not os.path.isdir(ROOT):
        raise SystemExit("ROOT が見つかりません: " + ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"[file_sorter] ROOT = {ROOT}")
    print(f"[file_sorter] 起動: {url}  （Ctrl+C で停止）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[file_sorter] 停止しました。")


if __name__ == "__main__":
    main()
