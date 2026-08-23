#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蒸馏人 — 微信 4.x 聊天记录提取工具

从运行中的微信 4.x（Weixin.exe）进程内存提取 SQLCipher 密钥，
解密本地数据库，查找指定联系人，输出结构化 JSON。

Usage:
    python distill_person.py extract --db-dir "<路径>" --output all_keys.json
    python distill_person.py decrypt --keys all_keys.json --target "张三"
    python distill_person.py distill --target "张三" --output chat.json
    python distill_person.py --auto-detect

Requirements:
    - Python 3.10+（Windows，零第三方依赖，使用系统自带 bcrypt.dll）
    - 微信 4.x 已登录并运行

https://github.com/TANGandXUE/wcdb-key-tool (密钥提取核心逻辑来源)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

_print = lambda *a, **kw: print(*a, flush=True, **kw)

# ============================================================
# Constants
# ============================================================
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b"SQLite format 3\x00"

# ============================================================
# AES-CBC via Windows CNG (bcrypt.dll, 零第三方依赖)
# ============================================================
if sys.platform == "win32":
    _bcrypt = ctypes.WinDLL("bcrypt")
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong
    ]
    _bcrypt.BCryptSetProperty.argtypes = [
        wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong
    ]
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong,
    ]
    _bcrypt.BCryptDecrypt.argtypes = [
        wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
    ]


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC 解密（无 padding），走 CNG 的 bcrypt.dll。"""
    h_alg = wt.HANDLE()
    status = _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
    if status != 0:
        raise RuntimeError(f"BCryptOpenAlgorithmProvider failed: {status:#x}")
    try:
        mode = ("ChainingModeCBC\x00").encode("utf-16-le")
        status = _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
        if status != 0:
            raise RuntimeError(f"BCryptSetProperty failed: {status:#x}")
        h_key = wt.HANDLE()
        status = _bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0
        )
        if status != 0:
            raise RuntimeError(f"BCryptGenerateSymmetricKey failed: {status:#x}")
        try:
            iv_buf = ctypes.create_string_buffer(iv, len(iv))
            out_buf = ctypes.create_string_buffer(len(data))
            result_len = ctypes.c_ulong(0)
            status = _bcrypt.BCryptDecrypt(
                h_key, data, len(data), None,
                iv_buf, len(iv),
                out_buf, len(out_buf), ctypes.byref(result_len), 0,
            )
            if status != 0:
                raise RuntimeError(f"BCryptDecrypt failed: {status:#x}")
            return out_buf.raw[: result_len.value]
        finally:
            _bcrypt.BCryptDestroyKey(h_key)
    finally:
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# ============================================================
# HMAC Verification (SQLCipher4 规范)
# ============================================================

def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


# ============================================================
# DB File Collection
# ============================================================

def collect_db_files(db_dir: str) -> tuple[list, dict]:
    db_files = []
    salt_to_dbs: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            if name.endswith(".decrypted.db"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
            salt_to_dbs.setdefault(salt, []).append(rel)
    return db_files, salt_to_dbs


# ============================================================
# 密钥提取 — Windows 4.1+ Config.Cipher runtime 扫描
# ============================================================

WINDOWS_CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
WINDOWS_CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
WINDOWS_MAX_USER_ADDRESS = 0x0000_8000_0000_0000
WINDOWS_CONFIG_BLOB_MAX = 1024
WINDOWS_CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
_HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


def _get_pids_windows() -> list[tuple[int, int]]:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pids = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(",", "").replace(" K", "").strip() or "0")
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行，请先启动并登录微信 4.x")
    pids.sort(key=lambda x: x[1], reverse=True)
    for pid, mem in pids:
        _print(f"  Weixin.exe PID={pid} ({mem // 1024}MB)")
    return pids


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def _u64_from(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_32_byte_key(data: bytes) -> bool:
    return len(data) == KEY_SZ and len(set(data)) >= 15 and data not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ}


def _iter_region_chunks(regions, read_region, chunk_size=2 * 1024 * 1024, overlap=0):
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            cs = min(chunk_size, size - offset)
            chunk = read_region(base + offset, cs) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + cs
            else:
                tail = b""
                tail_base = base + offset + cs
            offset += cs


def _find_bytes_in_regions(regions, read_region, needle):
    addresses = set()
    overlap = max(0, len(needle) - 1)
    for data_base, haystack in _iter_region_chunks(regions, read_region, overlap=overlap):
        pos = haystack.find(needle)
        while pos >= 0:
            addresses.add(data_base + pos)
            pos = haystack.find(needle, pos + 1)
    return addresses


def _config_key_candidates(blob: bytes) -> list:
    if not blob or len(blob) > WINDOWS_CONFIG_BLOB_MAX:
        return []
    decoded = _xor_repeat(blob, WINDOWS_CONFIG_XOR_MASK)
    out = []
    seen = set()
    for match in WINDOWS_CONFIG_LITERAL_RE.finditer(decoded):
        run = match.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            ek_hex = run[start:start + 64]
            try:
                ek = bytes.fromhex(ek_hex)
            except ValueError:
                continue
            if not _probable_32_byte_key(ek):
                continue
            salt = run[start + 64:start + 96] if start + 96 <= len(run) else None
            item = (ek_hex, salt)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def extract_keys(db_dir: str, out_file: str) -> dict:
    """提取微信 4.x 数据库密钥。"""
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError(f"在 {db_dir} 未找到可解密的 .db 文件")
    _print(f"找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同的 salt")

    kernel32 = ctypes.windll.kernel32
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
            ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
            ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
        ]

    def read_mem(h, addr, sz):
        buf = ctypes.create_string_buffer(sz)
        n = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
            return buf.raw[:n.value]
        return None

    def enum_regions(h):
        regs = []
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
                regs.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return regs

    pids = _get_pids_windows()
    key_map: dict[str, str] = {}
    remaining_salts = set(salt_to_dbs.keys())
    t0 = time.time()

    for pid, _mem in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            _print(f"  [WARN] 无法打开进程 PID={pid}（尝试以管理员身份运行）")
            continue
        try:
            regions = enum_regions(h)
            # Config.Cipher runtime scan
            needle_addrs = _find_bytes_in_regions(
                regions, lambda base, sz, _h=h: read_mem(_h, base, sz),
                WINDOWS_CONFIG_CIPHER_NAME
            )
            if not needle_addrs:
                continue

            pair_patterns = [
                struct.pack("<Q", a) + struct.pack("<Q", len(WINDOWS_CONFIG_CIPHER_NAME))
                for a in needle_addrs
            ]
            seen_candidates = set()

            for base, data in _iter_region_chunks(
                regions, lambda base, sz, _h=h: read_mem(_h, base, sz), overlap=0x80
            ):
                if not remaining_salts:
                    break
                for pattern in pair_patterns:
                    pos = data.find(pattern)
                    while pos >= 0:
                        node_base = base + pos - 0x10
                        node = read_mem(h, node_base, 0x50)
                        if not node or len(node) < 0x40:
                            pos = data.find(pattern, pos + 1)
                            continue
                        if _u64_from(node, 0x10) not in needle_addrs:
                            pos = data.find(pattern, pos + 1)
                            continue
                        if _u64_from(node, 0x18) != len(WINDOWS_CONFIG_CIPHER_NAME):
                            pos = data.find(pattern, pos + 1)
                            continue
                        config_ptr = _u64_from(node, 0x28)
                        if not (0x10000 <= config_ptr < WINDOWS_MAX_USER_ADDRESS):
                            pos = data.find(pattern, pos + 1)
                            continue
                        obj = read_mem(h, config_ptr + 0x88, 0x28)
                        if not obj or len(obj) < 0x18:
                            pos = data.find(pattern, pos + 1)
                            continue
                        data_ptr = _u64_from(obj, 0x8)
                        data_len = _u64_from(obj, 0x10)
                        if not (0 < data_len <= WINDOWS_CONFIG_BLOB_MAX):
                            pos = data.find(pattern, pos + 1)
                            continue
                        if not (0x10000 <= data_ptr < WINDOWS_MAX_USER_ADDRESS):
                            pos = data.find(pattern, pos + 1)
                            continue
                        blob = read_mem(h, data_ptr, int(data_len))
                        if not blob or len(blob) != data_len:
                            pos = data.find(pattern, pos + 1)
                            continue
                        for ek_hex, emb_salt in _config_key_candidates(blob):
                            cand = (ek_hex, emb_salt)
                            if cand in seen_candidates:
                                continue
                            seen_candidates.add(cand)
                            try:
                                ek = bytes.fromhex(ek_hex)
                            except ValueError:
                                continue
                            if not _probable_32_byte_key(ek):
                                continue
                            target_salts = [emb_salt] if emb_salt in remaining_salts else list(remaining_salts)
                            for sh in target_salts:
                                if sh not in remaining_salts:
                                    continue
                                for _rel, _path, _sz, s, p1 in db_files:
                                    if s == sh and verify_enc_key(ek, p1):
                                        key_map[sh] = ek_hex
                                        remaining_salts.discard(sh)
                                        _print(f"  [FOUND] {sh} → {ek_hex[:16]}...")
                                        break
                        pos = data.find(pattern, pos + 1)
        finally:
            kernel32.CloseHandle(h)
        if not remaining_salts:
            break

    # 回退：老版本 raw key 扫描
    if not key_map:
        _print("[INFO] Config.Cipher 扫描未命中，尝试老版本 raw key 扫描...")
        for pid, _mem in pids:
            h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                continue
            try:
                for base, size in enum_regions(h):
                    if not remaining_salts:
                        break
                    data = read_mem(h, base, size)
                    if not data:
                        continue
                    for m in _HEX_RE.finditer(data):
                        hs = m.group(1).decode()
                        if len(hs) < 96:
                            continue
                        ek_hex, salt_hex = hs[:64], hs[64:96]
                        if salt_hex not in remaining_salts:
                            continue
                        ek = bytes.fromhex(ek_hex)
                        for _rel, _path, _sz, s, p1 in db_files:
                            if s == salt_hex and verify_enc_key(ek, p1):
                                key_map[salt_hex] = ek_hex
                                remaining_salts.discard(salt_hex)
                                _print(f"  [FOUND] {salt_hex} → {ek_hex[:16]}...")
                                break
            finally:
                kernel32.CloseHandle(h)
            if not remaining_salts:
                break

    _print(f"\n扫描完成: {time.time() - t0:.1f}s, {len(key_map)}/{len(salt_to_dbs)} salts 命中")

    if not key_map:
        raise RuntimeError("未能提取到任何密钥")

    # 保存
    result = {}
    for rel, _path, sz, salt_hex, _p1 in db_files:
        if salt_hex in key_map:
            result[rel] = {"enc_key": key_map[salt_hex], "salt": salt_hex, "size_mb": round(sz / 1024 / 1024, 1)}
    result["_db_dir"] = db_dir
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    _print(f"密钥保存到: {out_file}")
    return key_map


# ============================================================
# Database Decryption
# ============================================================

def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return bytes(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ)
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return decrypted + b"\x00" * RESERVE_SZ


def decrypt_database(db_path: str, out_path: str, enc_key: bytes) -> bool:
    import sqlite3
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ

    with open(db_path, "rb") as f:
        page1 = f.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        return False

    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hm = hmac_mod.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    if hm.digest() != page1[PAGE_SZ - HMAC_SZ:PAGE_SZ]:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                page = page + b"\x00" * (PAGE_SZ - len(page)) if page else b"\x00" * PAGE_SZ
            fout.write(_decrypt_page(enc_key, page, pgno))

    # 验证
    conn = sqlite3.connect(out_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return len(tables) > 0


# ============================================================
# Contact Lookup & Message Extraction
# ============================================================

def find_contact(decrypted_contact_db: str, target_name: str) -> dict | None:
    """在解密后的 contact.db 中查找联系人。"""
    import sqlite3
    conn = sqlite3.connect(decrypted_contact_db)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(contact)")
    cols = [r[1] for r in cur.fetchall()]

    # 搜索 remark / nick_name / alias
    found = None
    for col in cols:
        if col.lower() in ("remark", "nickname", "nick_name", "alias", "username"):
            try:
                cur.execute(f"SELECT * FROM contact WHERE {col} LIKE ?", (f"%{target_name}%",))
                for row in cur.fetchall():
                    found = dict(zip(cols, row))
                    break
            except:
                pass
            if found:
                break

    # 搜索所有文本列
    if not found:
        for col in cols:
            try:
                cur.execute(f"SELECT * FROM contact WHERE CAST({col} AS TEXT) LIKE ?", (f"%{target_name}%",))
                rows = cur.fetchall()
                if rows:
                    found = dict(zip(cols, rows[0]))
                    break
            except:
                pass

    conn.close()
    return found


def extract_messages(
    decrypted_msg_dbs: list[str],
    target_wxid: str,
    my_wxid: str,
    contact_name: str,
) -> list[dict]:
    """从解密后的 message 数据库中提取指定联系人的所有消息。"""
    import sqlite3
    md5 = hashlib.md5(target_wxid.encode()).hexdigest()
    table = f"Msg_{md5}"
    all_messages = []

    for db_path in decrypted_msg_dbs:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # 查 Name2Id 映射
        sender_map = {}
        try:
            cur.execute("SELECT rowid, user_name FROM Name2Id")
            for rowid, uname in cur.fetchall():
                if uname == target_wxid:
                    sender_map[rowid] = contact_name
                elif uname == my_wxid:
                    sender_map[rowid] = "me"
        except:
            pass

        # 查消息表
        try:
            cur.execute(f"PRAGMA table_info({table})")
            mcols = [r[1] for r in cur.fetchall()]
            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()
            _print(f"  {os.path.basename(db_path)} / {table}: {len(rows)} 条消息")

            for row in rows:
                m = dict(zip(mcols, row))
                sid = m.get("real_sender_id")
                sender = sender_map.get(sid, f"unknown({sid})")

                content = m.get("message_content", "")
                if isinstance(content, (bytes, bytearray)):
                    content = content.decode("utf-8", errors="replace")

                all_messages.append({
                    "time": datetime.fromtimestamp(m.get("create_time", 0)).strftime("%Y-%m-%d %H:%M:%S"),
                    "sender": sender,
                    "type": _decode_msg_type(m.get("local_type")),
                    "content": content or "",
                })
        except sqlite3.OperationalError:
            # 表不在此库
            pass

        conn.close()

    # 按时间排序
    all_messages.sort(key=lambda x: x["time"])
    return all_messages


def _decode_msg_type(t: int) -> str:
    types = {
        1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
        48: "location", 50: "voip", 10000: "system", 10002: "revoke",
        49: "link_file", 42: "contact_card", 244813135921: "quote_reply",
    }
    return types.get(t, f"type_{t}")


# ============================================================
# Auto-detect
# ============================================================

def auto_detect_db_dir() -> str | None:
    appdata = os.environ.get("APPDATA", "")
    config_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
    if not os.path.isdir(config_dir):
        # 回退：直接搜索 Documents
        docs = os.path.join(os.path.expanduser("~"), "Documents", "xwechat_files")
        if os.path.isdir(docs):
            for sub in os.listdir(docs):
                db = os.path.join(docs, sub, "db_storage")
                if os.path.isdir(db):
                    return db
        return None

    data_roots = []
    for ini_file in glob.glob(os.path.join(config_dir, "*.ini")):
        for enc in ("utf-8", "gbk"):
            try:
                with open(ini_file, "r", encoding=enc) as f:
                    content = f.read(1024).strip()
                if content and os.path.isdir(content):
                    data_roots.append(content)
                break
            except:
                continue

    for root in data_roots:
        for match in glob.glob(os.path.join(root, "xwechat_files", "*", "db_storage")):
            if os.path.isdir(match):
                return match
    return None


# ============================================================
# CLI Commands
# ============================================================

def cmd_extract(args):
    db_dir = args.db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能自动检测数据库目录，请用 --db-dir 指定")
        sys.exit(1)
    _print(f"[*] 数据库目录: {db_dir}")
    extract_keys(db_dir, args.output)


def cmd_distill(args):
    """一键提取：探测目录 → 提取密钥 → 解密 → 查找联系人 → 导出 JSON"""
    import sqlite3

    # 1. 探测目录
    db_dir = args.db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能自动检测数据库目录，请用 --db-dir 指定")
        sys.exit(1)
    _print(f"[*] 数据库目录: {db_dir}")

    # 2. 提取密钥
    keys_file = args.keys or os.path.join(os.path.dirname(args.output), "all_keys.json")
    if not os.path.exists(keys_file):
        _print("\n[1/4] 提取密钥...")
        extract_keys(db_dir, keys_file)
    else:
        _print(f"[1/4] 使用已有密钥文件: {keys_file}")

    with open(keys_file, encoding="utf-8") as f:
        keys = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

    # 3. 解密 contact.db
    _print("\n[2/4] 解密 contact.db...")
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="distill_")
    contact_rel = "contact" + os.sep + "contact.db"
    contact_key_info = keys.get(contact_rel) or keys.get("contact/contact.db")
    if not contact_key_info:
        _print("[ERROR] 密钥文件中找不到 contact.db 的密钥")
        sys.exit(1)

    dec_contact = os.path.join(tmp_dir, "contact.db")
    enc_key = bytes.fromhex(contact_key_info["enc_key"])
    if decrypt_database(os.path.join(db_dir, contact_rel), dec_contact, enc_key):
        _print("  contact.db 解密成功")
    else:
        _print("[ERROR] contact.db 解密失败")
        sys.exit(1)

    # 4. 查找联系人
    _print(f"\n[3/4] 查找联系人 '{args.target}'...")
    contact = find_contact(dec_contact, args.target)
    if not contact:
        _print(f"[ERROR] 未找到备注/昵称包含 '{args.target}' 的联系人")
        # 列出所有有备注的
        conn = sqlite3.connect(dec_contact)
        cur = conn.cursor()
        cur.execute("SELECT username, remark, nick_name FROM contact WHERE remark IS NOT NULL AND remark != '' LIMIT 30")
        _print("  以下是有备注的联系人（前30个）:")
        for row in cur.fetchall():
            _print(f"    {row[0]}: 备注={row[1]}, 昵称={row[2]}")
        conn.close()
        sys.exit(1)

    wxid = contact.get("username", "")
    remark = contact.get("remark", "")
    nick = contact.get("nick_name", "")
    alias = contact.get("alias", "")
    _print(f"  找到! wxid={wxid}, 备注={remark}, 昵称={nick}, alias={alias}")

    # 5. 解密 message 库并提取消息
    _print("\n[4/4] 解密消息库并提取聊天记录...")
    md5 = hashlib.md5(wxid.encode()).hexdigest()
    table = f"Msg_{md5}"

    # 找到包含此表的 message 库
    msg_dbs_to_decrypt = []
    for rel, info in keys.items():
        if rel.startswith("message") and rel.endswith(".db") and "fts" not in rel:
            # 先快速检查这个库是否包含目标表
            db_path = os.path.join(db_dir, rel.replace("/", os.sep))
            # 解密到临时目录
            out_path = os.path.join(tmp_dir, os.path.basename(rel))
            ek = bytes.fromhex(info["enc_key"])
            if decrypt_database(db_path, out_path, ek):
                # 检查是否包含目标表
                conn = sqlite3.connect(out_path)
                cur = conn.cursor()
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    count = cur.fetchone()[0]
                    _print(f"  {rel}: {count} 条消息")
                    msg_dbs_to_decrypt.append(out_path)
                except:
                    pass  # 表不在此库
                conn.close()

    # 推断用户自己的 wxid
    my_wxid = ""
    # 从目录名提取
    parent = os.path.basename(os.path.dirname(db_dir))
    if parent.startswith("wxid_"):
        my_wxid = parent.split("_2d")[0].split("_2a")[0] if "_2" in parent else parent

    # 提取消息
    messages = extract_messages(msg_dbs_to_decrypt, wxid, my_wxid, remark or nick or args.target)

    if not messages:
        _print("[ERROR] 未提取到任何消息")
        sys.exit(1)

    # 统计
    sender_counts = Counter(m["sender"] for m in messages)
    type_counts = Counter(m["type"] for m in messages)

    _print(f"\n总消息: {len(messages)}")
    _print(f"发送者: {dict(sender_counts)}")
    _print(f"类型: {dict(type_counts)}")
    if messages:
        _print(f"时间: {messages[0]['time']} ~ {messages[-1]['time']}")

    # 输出 JSON
    result = {
        "meta": {
            "contact_name": remark or nick or args.target,
            "wxid": wxid,
            "alias": alias,
            "nickname": nick,
            "total_messages": len(messages),
            "time_range": {
                "start": messages[0]["time"] if messages else None,
                "end": messages[-1]["time"] if messages else None,
            },
            "sender_stats": dict(sender_counts),
            "type_stats": dict(type_counts),
        },
        "messages": messages,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    _print(f"\n已保存: {args.output}")
    _print(f"大小: {os.path.getsize(args.output) / 1024 / 1024:.1f}MB")

    # 清理临时文件
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="蒸馏人 — 微信 4.x 聊天记录提取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python distill_person.py distill --target '张三' --output chat.json\n"
               "  python distill_person.py extract --db-dir 'C:\\...\\db_storage' --output all_keys.json\n",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # extract
    p_ext = sub.add_parser("extract", help="提取数据库密钥")
    p_ext.add_argument("--db-dir", help="微信 db_storage 目录")
    p_ext.add_argument("--output", default="all_keys.json", help="密钥输出文件")

    # distill (一键)
    p_dis = sub.add_parser("distill", help="一键提取：密钥→解密→查找→导出 JSON")
    p_dis.add_argument("--target", required=True, help="联系人姓名/备注/昵称")
    p_dis.add_argument("--output", default="chat.json", help="输出 JSON 文件")
    p_dis.add_argument("--db-dir", help="微信 db_storage 目录（默认自动检测）")
    p_dis.add_argument("--keys", help="已有密钥文件（跳过提取步骤）")

    args = parser.parse_args()
    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "distill":
        cmd_distill(args)


if __name__ == "__main__":
    main()
