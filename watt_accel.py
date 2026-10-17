#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watt-accel —— Watt Toolkit(Steam++) 网络加速模块的独立 CLI 提取版
=====================================================================

把 Watt Toolkit(原 Steam++) 的"网络加速"模块核心单独提取为命令行工具，
**零第三方依赖**，仅使用 Python 3.8+ 标准库（urllib / socket / asyncio / json /
argparse / subprocess / threading 等），可直接运行。

与原模块的对应关系（源码见 https://github.com/BeyondDimension/SteamTools ）：

  - 加速服务订阅列表  ←  BD.WTTS.MicroServices.ClientSDK / Accelerate.All()
                        POST https://api.steampp.net/accelerator/projectgroups
  - 加速项目模型      ←  AccelerateProjectGroupDTO / AccelerateProjectDTO
                        (ListenDomainNames = hosts 要写入的域名，分号分隔)
  - hosts 写入/还原   ←  HostsFileServiceImpl.HandleHosts
                        (标记区 "# Steam++ Start/End" + 备份区
                         "# Steam++ Backup Start/End"，含 V1 格式兼容)
  - 快速 DNS 解析     ←  ReverseProxy 的 ProxyDNS / DoH 加速解析
  - 本地代理(Serve)   ←  ProxyMode.Hosts 的本地 443 反向代理（本实现为
                        SNI 透传代理，不做中间人/证书/脚本注入）

用法：
  python3 watt_accel.py list                # 拉取并展示加速服务列表
  python3 watt_accel.py apply --all         # 加速全部服务(写入 hosts)
  python3 watt_accel.py apply --default    # 默认模式：其它网站、网盘服务、Github、国外验证码平台、公共 CDN
  python3 watt_accel.py apply --config mycfg.json  # 加载自定义配置文件
  python3 watt_accel.py apply --exportcfg template.json #导出默认配置模板
  python3 watt_accel.py apply "Steam 社区"  # 加速指定服务
  python3 watt_accel.py apply --group "Steam" --dns 223.5.5.5
  python3 watt_accel.py restore             # 还原 hosts(删除标记区与备份区)
  python3 watt_accel.py status              # 查看当前加速状态
  python3 watt_accel.py serve --all --dns 1.1.1.1   # 启动本地加速代理
  python3 watt_accel.py flushdns            # 刷新系统 DNS 缓存

许可证：本文件为 SteamTools(GPL-3.0) 网络加速模块的衍生作品，按 GPL-3.0 分发。
详见随附 LICENSE 文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib import request as urllib_request

__version__ = "1.2.0"

# ---------------------------------------------------------------------------
# 常量：与 SteamTools 原模块保持一致
# ---------------------------------------------------------------------------

# 加速服务订阅接口（原 IMicroServiceClient.Instance.Accelerate.All() 的请求路由）
SUBSCRIBE_URL = "https://api.steampp.net/accelerator/projectgroups"

# hosts 标记（原 HostsFileServiceImpl.MarkStart / MarkEnd / BackupMarkStart / BackupMarkEnd）
MARK_START = "# Steam++ Start"
MARK_END = "# Steam++ End"
BACKUP_MARK_START = "# Steam++ Backup Start"
BACKUP_MARK_END = "# Steam++ Backup End"
V1_MARK = "#Steam++"  # 旧版本格式标记，遇到即移除

# 订阅缓存
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".watt-accel")
CACHE_FILE = os.path.join(CACHE_DIR, "subscribe.json")
CACHE_MAX_AGE_SECONDS = 6 * 3600

# 请求体参数（原 GetAccelerateProjectGroupRequest，Platform/DeviceIdiom 沿用桌面端取值）
SUBSCRIBE_BODY = {
    "platform": 2,
    "deviceIdiom": 0,
    "version": 1,
}
USER_AGENT = "watt-accel/{} (SteamTools accelerator CLI, GPL-3.0)".format(__version__)

DNS_TIMEOUT = 4.0
RESOLVE_MAX_WORKERS = 16
SNI_MATCH_PORT = 443

# ===================== 默认模式 --default 配置 =====================
# --default 按分组名选中以下分组
DEFAULT_ACCEL_GROUPS = [
    "其它网站",
    "网盘服务",
    "Github",
    "国外验证码平台",
    "公共 CDN"
]

BUILTIN_DEFAULT_CONFIG = {
    "names": [],
    "group": [],
    "all": False,
    "dns": "223.5.5.5",
    "doh": None,
    "ipv6": False,
    "target": None,
    "hosts_file": None,
    "dry_run": False,
    "refresh": False,
    "offline": False
}

CONFIG_TEMPLATE_COMMENT = '''
# watt‑accel apply 配置文件模板(json)
# 注意：--default 参数会忽略本配置的 names/group，固定加速：其它网站,网盘服务,Github,国外验证码平台,公共 CDN
# 字段说明:
# names: 字符串数组，服务名称关键字，对应命令行位置参数
# group: 字符串数组，按分组名加速，对应 --group
# all: bool，是否全部加速，对应 --all
# dns: str|null，自定义DNS服务器IP，例如"223.5.5.5"
# doh: str|null，DoH地址，例如"https://1.1.1.1/dns‑query"
# ipv6: bool，优先IPv6解析
# target: str|null，固定写入hosts的IP，跳过DNS解析
# hosts_file: str|null，自定义hosts路径
# dry_run: bool，仅预览不写入磁盘
# refresh: bool，强制重新拉取订阅
# offline: bool，仅使用本地缓存订阅
'''

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def warn(msg: str) -> None:
    eprint("[警告] " + msg)


def info(msg: str) -> None:
    print(msg)


def default_hosts_path() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "drivers", "etc", "hosts")
    return "/etc/hosts"


def is_admin() -> bool:
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False

# ========================= 配置加载导出工具 =========================
def export_config_template(out_path: str):
    template = dict(BUILTIN_DEFAULT_CONFIG)
    out_text = CONFIG_TEMPLATE_COMMENT + json.dumps(template, ensure_ascii=False, indent=2)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_text)
    info(f"配置模板已导出到 {out_path}")

def load_custom_config(cfg_path: str) -> dict:
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            lines.append(line)
    clean_json = "\n".join(lines)
    cfg = json.loads(clean_json)
    for k, v in BUILTIN_DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def merge_config_to_args(args, cfg: dict):
    for key in BUILTIN_DEFAULT_CONFIG.keys():
        val = cfg.get(key)
        if val is not None:
            setattr(args, key, val)
    return args

def select_default_groups_items(groups: list):
    """--default模式：选中 DEFAULT_ACCEL_GROUPS 全部分组内项目"""
    selected = []
    seen = set()
    for g in groups:
        gname = g.get("Name", "")
        if gname in DEFAULT_ACCEL_GROUPS:
            for item in g.get("Items") or []:
                key = id(item)
                if key not in seen:
                    seen.add(key)
                    selected.append((g, item))
    return selected

# ---------------------------------------------------------------------------
# 订阅：拉取 / 缓存 / 解析 加速服务列表
# ---------------------------------------------------------------------------

def http_post_json(url: str, body: dict, timeout: float = 20.0):
    """纯标准库 POST 请求并解析 JSON（原 IMicroServiceClient 的 HTTP 客户端）。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_subscribe_response(rsp) -> list:
    """
    解析订阅响应。原 IApiRsp 的包装键是表情符号：
      🦓 = Content(数据), 🦄 = Code(状态码), 🐴 = Message
    兼容普通 {"data": [...]} 或裸数组的返回。
    """
    if isinstance(rsp, list):
        return rsp
    if isinstance(rsp, dict):
        for key in ("🦓", "data", "Data", "content", "Content"):
            if key in rsp and isinstance(rsp[key], list):
                return rsp[key]
        # 只有 code 包装：code == 200 视为空数据
        code = rsp.get("🦄", rsp.get("code", rsp.get("Code")))
        if code in (200, "200", 0):
            return []
    raise RuntimeError("订阅接口返回格式无法识别: %s" % (str(rsp)[:300]))


def load_subscribe(force_refresh: bool = False, timeout: float = 20.0) -> list:
    """加载订阅数据：优先网络拉取，失败时回退本地缓存。"""
    os.makedirs(CACHE_DIR, exist_ok=True)

    def _cache_valid():
        try:
            if os.path.getmtime(CACHE_FILE) + CACHE_MAX_AGE_SECONDS < datetime.now().timestamp():
                return False
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else None
        except Exception:
            return None

    if not force_refresh:
        cached = _cache_valid()
        if cached:
            return cached

    try:
        rsp = http_post_json(SUBSCRIBE_URL, dict(SUBSCRIBE_BODY, **{
            "dateTimeOffset": datetime.now(timezone.utc).isoformat(),
        }), timeout=timeout)
        data = parse_subscribe_response(rsp)
        if force_refresh or not _cache_valid():
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except OSError as e:
                warn("订阅缓存写入失败: %s" % e)
        return data
    except Exception as e:
        cached = _cache_valid()
        if cached:
            warn("订阅拉取失败(%s)，使用本地缓存。" % e)
            return cached
        raise


def iter_items(groups: list):
    """遍历所有分组下的顶层加速项目。"""
    for group in groups:
        for item in (group.get("Items") or []):
            yield group, item


def split_domains(value) -> list:
    """原 ApiConstants.GetSplitValues：按 ';' 分割并去除空白。"""
    if not value:
        return []
    return [d.strip() for d in value.split(";") if d.strip()]


def select_items(groups: list, names=(), group_names=(), all_flag: bool = False) -> list:
    """
    按名称选择加速项目（不区分大小写、子串匹配）。
    返回 [(group, item), ...]。
    """
    if all_flag:
        return [(g, it) for g, it in iter_items(groups)]

    selected = []
    seen = set()

    def _push(g, it):
        key = id(it)
        if key not in seen:
            seen.add(key)
            selected.append((g, it))

    for g, it in iter_items(groups):
        gname = (g.get("Name") or "").lower()
        for gn in group_names:
            if gn.lower() in gname:
                _push(g, it)
                break
        # 关键词匹配：顶层精确 > 顶层子串 > 子项目子串，避免"Steam 社区"
        # 同时误匹配"Steam 社区(解锁访问限制)"
        top_exact = [n for n in names if n.lower() == (it.get("Name") or "").lower()]
        if top_exact:
            _push(g, it)
            continue
        for n in names:
            if n.lower() in (it.get("Name") or "").lower():
                _push(g, it)
                break
        else:
            # 子项目也参与匹配（例如"Steam 社区(解锁访问限制)"）
            for sub in (it.get("Items") or []):
                sname = (sub.get("Name") or "").lower()
                if any(n.lower() in sname for n in names):
                    _push(g, sub)

    return selected


def collect_listen_domains(selected) -> list:
    """收集选中项目的 ListenDomainNames（去重，保持顺序）。"""
    domains = []
    seen = set()
    for _, item in selected:
        for d in split_domains(item.get("ListenDomainNames")):
            if d not in seen:
                seen.add(d)
                domains.append(d)
    return domains

# ---------------------------------------------------------------------------
# 域名解析：系统 DNS / 自定义 DNS(UDP+TCP) / DoH
# ---------------------------------------------------------------------------
def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("idna")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _decode_name(data: bytes, offset: int):
    """解析 DNS 报文中的域名（支持压缩指针），返回 (name, 新偏移)。"""
    labels = []
    jumped = False
    pos = offset
    end = offset
    while True:
        if pos >= len(data):
            break
        length = data[pos]
        if length == 0:
            pos += 1
            if not jumped:
                end = pos
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                end = pos + 2
            jumped = True
            pos = ptr
            continue
        pos += 1
        if pos + length > len(data):
            break
        # DNS 名称实际为 ASCII(punycode)，idna codec 不支持 replace 错误处理
        labels.append(data[pos:pos + length].decode("ascii", "replace"))
        pos += length
    return ".".join(labels), end


def build_dns_query(name: str, qtype: int) -> bytes:
    query_id = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = _encode_name(name) + struct.pack(">HH", qtype, 1)
    return header + question


def parse_dns_response(data: bytes):
    """解析 DNS 应答，返回 [('A'|'AAAA'|'CNAME', value), ...]（跳过 OPT 记录）。"""
    if len(data) < 12:
        return []
    qdcount, ancount = struct.unpack(">HH", data[4:8])
    pos = 12
    for _ in range(qdcount):  # 跳过问题区
        _, pos = _decode_name(data, pos)
        pos += 4
    answers = []
    for _ in range(ancount):
        _, pos = _decode_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if pos + rdlen > len(data):
            break
        rdata = data[pos:pos + rdlen]
        pos += rdlen
        if rclass != 1:  # 仅 IN 类
            continue
        if rtype == 1 and rdlen == 4:
            answers.append(("A", socket.inet_ntop(socket.AF_INET, rdata)))
        elif rtype == 28 and rdlen == 16:
            answers.append(("AAAA", socket.inet_ntop(socket.AF_INET6, rdata)))
        elif rtype == 5:
            name, _ = _decode_name(data, pos - rdlen)
            answers.append(("CNAME", name))
        # 其他类型(OPT 等)忽略
    return answers


def dns_query_udp(name: str, server: str, qtype: int, timeout: float = DNS_TIMEOUT) -> list:
    query = build_dns_query(name, qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(2):  # 至多重试一次
            sock.sendto(query, (server, 53))
            data, _ = sock.recvfrom(4096)
            if len(data) >= 2 and data[0:2] == query[0:2]:
                if len(data) >= 4 and data[2] & 0x02:  # TC 截断 → 走 TCP
                    return dns_query_tcp(name, server, qtype, timeout)
                return parse_dns_response(data)
    except socket.timeout:
        pass
    finally:
        sock.close()
    return []


def dns_query_tcp(name: str, server: str, qtype: int, timeout: float = DNS_TIMEOUT) -> list:
    query = build_dns_query(name, qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((server, 53))
        sock.sendall(struct.pack(">H", len(query)) + query)
        length_data = b""
        while len(length_data) < 2:
            chunk = sock.recv(2 - len(length_data))
            if not chunk:
                return []
            length_data += chunk
        (length,) = struct.unpack(">H", length_data)
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                return []
            data += chunk
        return parse_dns_response(data)
    except (socket.timeout, OSError):
        return []
    finally:
        sock.close()


def resolve_with_dns_server(domain: str, server: str, qtype: int) -> str | None:
    answers = dns_query_udp(domain, server, qtype)
    # 跟随 CNAME 链继续查询
    seen = {domain.lower()}
    for _ in range(8):
        for rtype, value in answers:
            if rtype in ("A", "AAAA"):
                return value
        cnames = [v for r, v in answers if r == "CNAME"]
        if not cnames:
            return None
        nxt = cnames[-1].lower()
        if nxt in seen:
            return None
        seen.add(nxt)
        answers = dns_query_udp(nxt, server, qtype)
    return None


def resolve_with_doh(domain: str, doh_url: str, qtype: int, timeout: float = DNS_TIMEOUT) -> str | None:
    type_name = "AAAA" if qtype == 28 else "A"
    sep = "&" if "?" in doh_url else "?"
    url = "{}{}name={}&type={}".format(doh_url, sep, urllib_request.quote(domain), type_name)
    req = urllib_request.Request(url, headers={
        "Accept": "application/dns-json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        for ans in obj.get("Answer", []):
            if ans.get("type") in (1, 28) and "data" in ans:
                return ans["data"]
    except Exception:
        pass
    return None


def resolve_system(domain: str, qtype: int) -> str | None:
    """系统 DNS 解析（等价于原模块在未指定 ProxyDNS 时走系统解析）。"""
    family = socket.AF_INET6 if qtype == 28 else socket.AF_INET
    try:
        infos = socket.getaddrinfo(domain, None, family, socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0]
            if "%" in addr:
                addr = addr.split("%", 1)[0]
            return addr
    except OSError:
        pass
    return None


def resolve_domain(domain: str, dns_server: str | None = None, doh_url: str | None = None,
                   prefer_ipv6: bool = False, timeout: float = DNS_TIMEOUT) -> str | None:
    """按优先级解析域名到单个 IP：自定义 DNS / DoH / 系统 DNS。"""
    order = ([28, 1] if prefer_ipv6 else [1, 28])
    for qtype in order:
        ip = None
        if dns_server:
            ip = resolve_with_dns_server(domain, dns_server, qtype)
        elif doh_url:
            ip = resolve_with_doh(domain, doh_url, qtype, timeout)
        else:
            ip = resolve_system(domain, qtype)
        if ip:
            return ip
    return None


def resolve_many(domains: list, dns_server=None, doh_url=None,
                 prefer_ipv6=False, timeout: float = DNS_TIMEOUT) -> dict:
    """并发解析多个域名，返回 {domain: ip}（失败项不包含在内）。"""
    result: dict = {}
    lock = threading.Lock()

    def _one(d: str):
        ip = resolve_domain(d, dns_server, doh_url, prefer_ipv6, timeout)
        if ip:
            with lock:
                result[d] = ip

    with ThreadPoolExecutor(max_workers=min(RESOLVE_MAX_WORKERS, max(1, len(domains)))) as ex:
        futures = [ex.submit(_one, d) for d in domains]
        for fut in as_completed(futures):
            fut.result()  # 收集异常
    return result


# ---------------------------------------------------------------------------
# hosts 引擎：原 HostsFileServiceImpl.HandleHosts 的忠实移植
# ---------------------------------------------------------------------------
class DuplicateMarkError(Exception):
    pass

def _get_mark(line_split: list) -> str | None:
    """原 GetMarkValue：识别 Steam++ 标记行（不区分大小写）。"""
    if len(line_split) == 3:
        value = " ".join(line_split)
        if value.lower() == MARK_START.lower():
            return MARK_START
        if value.lower() == MARK_END.lower():
            return MARK_END
    elif len(line_split) == 4:
        value = " ".join(line_split)
        if value.lower() == BACKUP_MARK_START.lower():
            return BACKUP_MARK_START
        if value.lower() == BACKUP_MARK_END.lower():
            return BACKUP_MARK_END
    return None


def detect_encoding(raw: bytes):
    """检测 hosts 文件编码（UTF-8 BOM / UTF-8 / GBK / latin-1），与原模块编码逻辑对应。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def read_hosts(path: str):
    """读取 hosts 文件，返回 (编码, 行列表[无行尾], 是否以换行结尾, 是否保留 BOM)。"""
    if not os.path.exists(path):
        return "utf-8", [], False, False
    with open(path, "rb") as f:
        raw = f.read()
    encoding = detect_encoding(raw)
    text = raw.decode(encoding)
    bom = text.startswith("\ufeff")
    if bom:
        text = text.lstrip("\ufeff")
    has_trailing_newline = text.endswith("\n")
    normalized = text.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # 去掉末尾空串
    return encoding, lines, has_trailing_newline, bom


def write_hosts(path: str, lines: list, encoding: str, has_trailing_newline: bool, bom: bool) -> None:
    """原子写入 hosts 文件（临时文件 + os.replace），保留权限。"""
    eol = "\r\n" if os.name == "nt" else "\n"
    content = eol.join(lines)
    if has_trailing_newline or content:
        content += eol
    if bom:
        content = "\ufeff" + content
    data = content.encode(encoding)

    mode = None
    try:
        mode = os.stat(path).st_mode
    except OSError:
        pass

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hosts.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def handle_hosts(is_update: bool, hosts: dict | None, path: str, dry_run: bool = False) -> dict:
    """
    HostsFileServiceImpl.HandleHosts 的 Python 移植。

    is_update=True  (对应 UpdateHosts) ：把 hosts{domain:ip} 写入标记区，
        标记区外已存在的同名行移入备份区；旧的备份按需还原。
    is_update=False (对应 RemoveHostsByTag)：删除标记区与备份区，
        把备份区记录的原始行还原回原位置。

    返回 {"lines": [...], "changed": bool, "encoding": str, ...}
    """
    if is_update and not hosts:
        raise ValueError("hosts 不能为空")
    if is_update and not path:
        raise ValueError("path 不能为空")

    encoding, lines, has_trailing_newline, bom = read_hosts(path)

    mark_start_seen = mark_end_seen = False
    backup_start_seen = backup_end_seen = False
    seen_marks = set()
    domains: dict = {}          # 全文件域名去重（后出现者覆盖，与原版一致）
    insert_mark: dict = {}      # 将要写入标记区的 domain -> ip
    backup_insert: dict = {}    # 标记区外被替换的原行：domain -> (line_num, line_value)
    backup_datas: dict = {}     # 备份区解析出的原始行：domain -> (line_num, line_value)
    out_lines: list = []
    last_line_value = None

    for idx, line in enumerate(lines, start=1):
        line_split = line.split()
        line_value = line
        if backup_start_seen and not backup_end_seen:
            if (len(line_split) >= 3 and line_split[0].startswith("#")
                    and line_split[0][1:].isdigit()):
                bak_no = int(line_split[0][1:])
                bak_value = " ".join(line_split[1:])
                bak_split = bak_value.split()
                if len(bak_split) >= 2:
                    backup_datas.setdefault(bak_split[1], (bak_no, bak_value))
            continue
        mark = _get_mark(line_split)
        if mark:
            if mark == MARK_END and not mark_start_seen:
                continue
            if mark == BACKUP_MARK_END and not backup_start_seen:
                continue
            if mark in (MARK_START, BACKUP_MARK_START):
                if last_line_value is not None and not last_line_value.strip():
                    out_lines.pop()
                    last_line_value = out_lines[-1] if out_lines else None
            if mark in seen_marks:
                raise DuplicateMarkError("hosts 中存在重复标记: %s" % mark)
            seen_marks.add(mark)
            if mark == MARK_START:
                mark_start_seen = True
            elif mark == MARK_END:
                mark_end_seen = True
            elif mark == BACKUP_MARK_START:
                backup_start_seen = True
            elif mark == BACKUP_MARK_END:
                backup_end_seen = True
            continue
        if len(line_split) < 2:
            out_lines.append(line_value)
            last_line_value = line_value
            continue
        if line_split[0].startswith("#"):
            out_lines.append(line_value)
            last_line_value = line_value
            continue
        if len(line_split) > 2 and not line_split[2].startswith("#"):
            out_lines.append(line_value)
            last_line_value = line_value
            continue
        if len(line_split) == 3 and line_split[2] == V1_MARK:
            continue
        ip, domain = line_split[0], line_split[1]
        domains[domain] = ip
        match_domain = is_update and hosts and domain in hosts
        if mark_start_seen and not mark_end_seen:
            if match_domain:
                insert_mark[domain] = hosts[domain]
            continue
        else:
            if match_domain:
                if is_update:
                    insert_mark[domain] = ip
                backup_insert[domain] = (idx, line_value)
                continue
        out_lines.append(line_value)
        last_line_value = line_value

    if is_update:
        for d, ip in hosts.items():
            insert_mark[d] = ip
    is_restore = (not is_update) and (hosts is None)
    changed = False

    if is_restore:
        for d, (bak_no, bak_value) in list(backup_datas.items()):
            pos = min(max(bak_no - 1, 0), len(out_lines))
            out_lines.insert(pos, bak_value)
            changed = True
    else:
        restore_items = [(d, v) for d, v in backup_datas.items() if d not in insert_mark]
        for d, (bak_no, bak_value) in restore_items:
            pos = min(max(bak_no - 1, 0), len(out_lines))
            out_lines.insert(pos, bak_value)
            backup_datas.pop(d)
            changed = True
        if insert_mark:
            out_lines.append("")
            out_lines.append(MARK_START)
            for d, ip in insert_mark.items():
                out_lines.append("%s %s" % (ip, d))
            out_lines.append(MARK_END)
            changed = True
        backup_lines = []
        for d, (bak_no, bak_value) in backup_insert.items():
            backup_lines.append("#%d %s" % (bak_no, bak_value))
        for d, (bak_no, bak_value) in backup_datas.items():
            if d in insert_mark and d not in backup_insert:
                backup_lines.append("#%d %s" % (bak_no, bak_value))
        if backup_lines:
            out_lines.append("")
            out_lines.append(BACKUP_MARK_START)
            out_lines.extend(backup_lines)
            out_lines.append(BACKUP_MARK_END)
            changed = True
    changed = changed or out_lines != lines
    if changed and not dry_run:
        write_hosts(path, out_lines, encoding, has_trailing_newline, bom)
    return {
        "lines": out_lines,
        "changed": changed,
        "encoding": encoding,
        "inserted": len(insert_mark) if is_update else 0,
    }

def contains_hosts_tag(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode(detect_encoding(raw))
        return any(line.strip().lower() == MARK_END.lower() for line in text.splitlines())
    except Exception:
        return False

def read_marker_entries(path: str) -> dict:
    encoding, lines, _, _ = read_hosts(path)
    result = {}
    in_marker = False
    for line in lines:
        ls = line.split()
        mark = _get_mark(ls)
        if mark == MARK_START:
            in_marker = True
            continue
        if mark == MARK_END:
            break
        if in_marker and len(ls) >= 2 and not ls[0].startswith("#"):
            result[ls[1]] = ls[0]
    return result

# ---------------------------------------------------------------------------
# 本地加速代理 SNI 透传
# ---------------------------------------------------------------------------
def parse_client_hello_sni(data: bytes) -> str | None:
    pos = 0
    try:
        while pos + 5 <= len(data):
            rtype, rver = data[pos], data[pos + 1:pos + 3]
            rlen = struct.unpack(">H", data[pos + 3:pos + 5])[0]
            pos += 5
            if pos + rlen > len(data):
                break
            record = data[pos:pos + rlen]
            pos += rlen
            if rtype != 22:
                continue
            htype, hlen = record[0], struct.unpack(">I", b"\x00" + record[1:4])[0]
            if htype != 1 or 4 + hlen > len(record):
                continue
            hello = record[4:4 + hlen]
            p = 2 + 32
            sid_len = hello[p]
            p += 1 + sid_len
            cipher_len = struct.unpack(">H", hello[p:p + 2])[0]
            p += 2 + cipher_len
            comp_len = hello[p]
            p += 1 + comp_len
            ext_total = struct.unpack(">H", hello[p:p + 2])[0]
            p += 2
            end = p + ext_total
            while p + 4 <= end:
                etype, elen = struct.unpack(">HH", hello[p:p + 4])
                p += 4
                if p + elen > end:
                    break
                edata = hello[p:p + elen]
                p += elen
                if etype == 0:
                    if len(edata) >= 5:
                        name_list_len = struct.unpack(">H", edata[0:2])[0]
                        if name_list_len + 2 <= len(edata):
                            name_type = edata[2]
                            name_len = struct.unpack(">H", edata[3:5])[0]
                            if name_type == 0 and 5 + name_len <= len(edata):
                                return edata[5:5 + name_len].decode("ascii", "replace")
            break
    except (IndexError, struct.error):
        return None
    return None

def domain_matches(sni: str, listen_domains: list) -> bool:
    sni = sni.rstrip(".").lower()
    for d in listen_domains:
        d = d.rstrip(".").lower()
        if sni == d or sni.endswith("." + d):
            return True
    return False

async def _pipe(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def _handle_client(reader, writer, listen_domains, dns_server, doh_url, prefer_ipv6):
    peer = writer.get_extra_info("peername")
    try:
        buff = b""
        while len(buff) < 5:
            chunk = await reader.read(1024)
            if not chunk:
                return
            buff += chunk
        try:
            hello_len = 5 + struct.unpack(">H", buff[3:5])[0]
        except struct.error:
            return
        while len(buff) < hello_len:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buff += chunk
        sni = parse_client_hello_sni(buff[:hello_len])
        if sni is None:
            warn("来自 %s 的连接无法解析 SNI，已拒绝" % str(peer))
            return
        accelerated = domain_matches(sni, listen_domains)
        if accelerated and (dns_server or doh_url):
            ip = await asyncio.to_thread(
                resolve_domain, sni, dns_server, doh_url, prefer_ipv6)
            if ip is None:
                warn("加速解析 %s 失败，回退系统 DNS" % sni)
                ip = await asyncio.to_thread(resolve_system, sni, 1)
        else:
            ip = await asyncio.to_thread(resolve_system, sni, 1)
        if ip is None:
            warn("无法解析 %s，已拒绝连接" % sni)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, SNI_MATCH_PORT), timeout=15)
        except (OSError, ConnectionError, asyncio.TimeoutError) as e:
            warn("连接 %s(%s) 失败或超时: %s" % (sni, ip, e))
            return
        if accelerated:
            info("[加速] %s -> %s (%s)" % (peer[0], sni, ip))
        upstream_writer.write(buff[:hello_len])
        await upstream_writer.drain()
        t1 = asyncio.create_task(_pipe(reader, upstream_writer))
        t2 = asyncio.create_task(_pipe(upstream_reader, writer))
        done, _ = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.cancel()
        try:
            upstream_writer.close()
        except Exception:
            pass
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def serve(host: str, port: int, listen_domains: list,
                dns_server=None, doh_url=None, prefer_ipv6=False):
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, listen_domains, dns_server, doh_url, prefer_ipv6),
        host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    info("本地加速代理已启动，监听 %s" % addrs)
    info("加速域名数: %d；HTTPS 端口: %d；DNS: %s" % (
        len(listen_domains), SNI_MATCH_PORT,
        dns_server or doh_url or "系统默认"))
    info("将系统代理/Steam 下载代理指向上述地址即可（仅 HTTPS 流量）")
    try:
        async with server:
            await server.serve_forever()
    except KeyboardInterrupt:
        info("\n已停止。")

# ---------------------------------------------------------------------------
# 平台 DNS 缓存刷新
# ---------------------------------------------------------------------------
def flush_dns() -> bool:
    commands = []
    if os.name == "nt":
        commands.append(["ipconfig", "/flushdns"])
    elif sys.platform == "darwin":
        commands.append(["dscacheutil", "-flushcache"])
        commands.append(["killall", "-HUP", "mDNSResponder"])
    else:
        commands.append(["resolvectl", "flush-caches"])
        commands.append(["systemd-resolve", "--flush-caches"])
        commands.append(["nscd", "-i", "hosts"])
    for cmd in commands:
        try:
            subprocess.run(cmd, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            info("已执行: %s" % " ".join(cmd))
            return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    warn("未能执行 DNS 刷新命令，可手动执行："
         "Windows: ipconfig /flushdns；macOS: dscacheutil -flushcache")
    return False

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watt-accel",
        description="Watt Toolkit(Steam++) 网络加速模块独立 CLI（零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 watt_accel.py list\n"
               "  python3 watt_accel.py apply --default\n"
               "  python3 watt_accel.py apply --config mycfg.json\n"
               "  python3 watt_accel.py apply --exportcfg template.json\n"
               "  python3 watt_accel.py apply --all --dns 223.5.5.5\n"
               "  python3 watt_accel.py restore\n",
    )
    p.add_argument("--version", action="version", version="watt-accel " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="拉取并展示加速服务列表")
    sp.add_argument("--refresh", action="store_true", help="强制重新拉取订阅")
    sp.add_argument("--filter", default="", help="按关键字过滤分组/服务名")
    sp.add_argument("--offline", action="store_true", help="仅使用本地缓存")

    sp = sub.add_parser("apply", help="应用加速：解析域名并写入 hosts（Steam++ 标记区）")
    sp.add_argument("names", nargs="*", help="服务名称关键字（可多个）")
    sp.add_argument("--group", action="append", default=[], metavar="名称", help="按分组名加速")
    sp.add_argument("--all", action="store_true", help="加速全部服务")
    sp.add_argument("--default", action="store_true",
                   help="【默认模式】加速分组：其它网站、网盘服务、Github、国外验证码平台、公共 CDN")
    sp.add_argument("--config", metavar="CFG_JSON", help="加载自定义JSON配置文件")
    sp.add_argument("--exportcfg", metavar="OUT_JSON", help="导出配置模板，仅输出文件，不执行加速")
    sp.add_argument("--dns", default=None, metavar="IP", help="指定 DNS 服务器解析(如 223.5.5.5)")
    sp.add_argument("--doh", default=None, metavar="URL", help="DoH 地址(如 https://1.1.1.1/dns‑query)")
    sp.add_argument("--ipv6", action="store_true", help="优先解析 AAAA(IPv6)")
    sp.add_argument("--target", default=None, metavar="IP", help="固定写入该 IP（配合 serve 用 127.0.0.1）")
    sp.add_argument("--hosts-file", default=None, help="hosts 文件路径（默认按平台）")
    sp.add_argument("--dry-run", action="store_true", help="只显示将要写入的内容，不落盘")
    sp.add_argument("--refresh", action="store_true", help="强制重新拉取订阅")
    sp.add_argument("--offline", action="store_true", help="仅使用本地缓存")

    sp = sub.add_parser("restore", help="还原 hosts：删除 Steam++ 标记区与备份区")
    sp.add_argument("--hosts-file", default=None, help="hosts 文件路径（默认按平台）")
    sp.add_argument("--dry-run", action="store_true", help="只显示将要还原的内容，不落盘")

    sp = sub.add_parser("status", help="查看当前加速状态")
    sp.add_argument("--hosts-file", default=None, help="hosts 文件路径（默认按平台）")

    sp = sub.add_parser("serve", help="启动本地加速代理(SNI 透传，HTTPS 443)")
    sp.add_argument("names", nargs="*", help="服务名称关键字（可多个）")
    sp.add_argument("--group", action="append", default=[], metavar="名称", help="按分组名")
    sp.add_argument("--all", action="store_true", help="加速全部服务")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    sp.add_argument("--port", type=int, default=443, help="监听端口（默认 443，Linux 需 root）")
    sp.add_argument("--dns", default=None, metavar="IP", help="加速使用的 DNS 服务器")
    sp.add_argument("--doh", default=None, metavar="URL", help="加速使用的 DoH 地址")
    sp.add_argument("--ipv6", action="store_true", help="优先解析 AAAA(IPv6)")
    sp.add_argument("--refresh", action="store_true", help="强制重新拉取订阅")
    sp.add_argument("--offline", action="store_true", help="仅使用本地缓存")

    sub.add_parser("flushdns", help="刷新系统 DNS 缓存")
    return p

def _get_groups(args) -> list:
    if getattr(args, "offline", False):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return load_subscribe(force_refresh=getattr(args, "refresh", False))

def cmd_list(args) -> int:
    groups = _get_groups(args)
    current = {}
    hosts_path = default_hosts_path()
    if contains_hosts_tag(hosts_path):
        current = read_marker_entries(hosts_path)
    kw = (args.filter or "").lower()
    total_domains = 0
    for g in groups:
        gname = g.get("Name", "")
        if kw and kw not in gname.lower():
            continue
        info("■ %s" % gname)
        for it in (g.get("Items") or []):
            name = it.get("Name", "")
            if kw and kw not in name.lower():
                continue
            domains = split_domains(it.get("ListenDomainNames"))
            applied = any(d in current for d in domains)
            flag = "[已加速]" if applied and domains else "[未加速]"
            info("    %-34s %s  (%d 域名)" % (name, flag, len(domains)))
            for sub in (it.get("Items") or []):
                if kw and kw not in (sub.get("Name") or "").lower():
                    continue
                sdomains = split_domains(sub.get("ListenDomainNames"))
                info("        └ %-30s (%d 域名)" % (sub.get("Name", ""), len(sdomains)))
            total_domains += len(domains)
    info("共 %d 个分组，%d 个顶层域名；当前 hosts 标记区内 %d 条加速记录" % (
        len(groups), total_domains, len(current)))
    return 0

def cmd_apply(args) -> int:
    groups = _get_groups(args)

    if args.exportcfg:
        if args.default or args.config or args.names or args.group or args.all:
            eprint("错误：--exportcfg 只能单独使用，不能搭配 --default / --config / names / --group / --all")
            return 1
        export_config_template(args.exportcfg)
        return 0

    if args.default and args.config:
        eprint("错误：--default 和 --config 不能同时使用")
        return 1

    if not args.default and not args.config and not args.names and not args.group and not args.all:
        eprint("错误：请指定 --default / --config 文件 / 服务名 / --group / --all")
        return 1

    if args.default:
        info(f"[默认模式] 将加速分组：{', '.join(DEFAULT_ACCEL_GROUPS)}")
        selected = select_default_groups_items(groups)
    elif args.config:
        cfg = load_custom_config(args.config)
        args = merge_config_to_args(args, cfg)
        selected = select_items(groups, args.names, args.group, args.all)
    else:
        selected = select_items(groups, args.names, args.group, args.all)

    if not selected:
        eprint("错误：未匹配到任何加速服务。可用 `list` 查看，或用 `--all` 加速全部。")
        return 1
    domains = collect_listen_domains(selected)
    if not domains:
        eprint("错误：所选服务没有可用的 ListenDomainNames。")
        return 1

    info("已选择 %d 个服务，共 %d 个域名：" % (len(selected), len(domains)))
    for d in domains:
        info("    %s" % d)

    hosts_path = args.hosts_file or default_hosts_path()
    if args.target:
        hosts = {d: args.target for d in domains}
        info("固定目标 IP：%s（跳过 DNS 解析）" % args.target)
    else:
        info("正在解析域名（DNS: %s）..." % (args.dns or args.doh or "系统默认"))
        hosts = resolve_many(domains, args.dns, args.doh, args.ipv6)
        failed = [d for d in domains if d not in hosts]
        if failed:
            warn("%d 个域名解析失败，已跳过：%s" % (len(failed), ", ".join(failed[:8])))
        if not hosts:
            eprint("错误：所有域名解析均失败。")
            return 1
        for d, ip in hosts.items():
            info("    %-48s -> %s" % (d, ip))

    if not is_admin() and not args.dry_run:
        warn("当前非管理员/root 权限，写入 %s 可能失败（Windows 请以管理员运行，Linux/macOS 请用 sudo）。"
             % hosts_path)
    try:
        result = handle_hosts(is_update=True, hosts=hosts, path=hosts_path, dry_run=args.dry_run)
    except DuplicateMarkError as e:
        eprint("错误：%s" % e)
        eprint("建议：手动检查 %s 中是否混入了其他工具的 Steam++ 标记，或先执行 restore。" % hosts_path)
        return 1
    except PermissionError:
        eprint("错误：没有权限写入 %s。" % hosts_path)
        return 1
    if args.dry_run:
        info("---- [dry‑run] 写入后的 hosts 内容 ----")
        for line in result["lines"]:
            info("    %s" % line)
    else:
        info("已完成：写入 %d 条加速记录到 %s" % (result["inserted"], hosts_path))
        flush_dns()
    return 0

def cmd_restore(args) -> int:
    hosts_path = args.hosts_file or default_hosts_path()
    if not contains_hosts_tag(hosts_path):
        info("hosts 中没有 Steam++ 标记区，无需还原。")
        return 0
    try:
        result = handle_hosts(is_update=False, hosts=None, path=hosts_path, dry_run=args.dry_run)
    except PermissionError:
        eprint("错误：没有权限写入 %s。" % hosts_path)
        return 1
    if args.dry_run:
        info("---- [dry‑run] 还原后的 hosts 内容 ----")
        for line in result["lines"]:
            info("    %s" % line)
    else:
        info("已还原 hosts，删除标记区。")
        flush_dns()
    return 0

def cmd_status(args) -> int:
    hosts_path = args.hosts_file or default_hosts_path()
    if not os.path.exists(hosts_path):
        info("hosts 文件不存在：%s" % hosts_path)
        return 0
    entries = read_marker_entries(hosts_path)
    if contains_hosts_tag(hosts_path):
        info("加速状态：运行中（标记区存在）")
        info("当前加速 %d 个域名：" % len(entries))
        for d, ip in entries.items():
            info("    %-48s -> %s" % (d, ip))
    else:
        info("加速状态：未加速（hosts 中无 Steam++ 标记区）")
    return 0

def cmd_serve(args) -> int:
    groups = _get_groups(args)
    selected = select_items(groups, args.names, args.group, args.all)
    if not selected:
        eprint("错误：未匹配到任何加速服务。")
        return 1
    domains = collect_listen_domains(selected)
    if not domains:
        eprint("错误：所选服务没有可用域名。")
        return 1
    info("代理将加速 %d 个服务、%d 个域名（SNI 匹配）" % (len(selected), len(domains)))
    if not is_admin() and args.port == 443:
        warn("监听 443 通常需要管理员/root 权限（Linux 可用 sudo setcap cap_net_bind_service=+eip 或换端口）。")
    try:
        asyncio.run(serve(args.host, args.port, domains, args.dns, args.doh, args.ipv6))
    except PermissionError:
        eprint("错误：没有权限监听 %s:%d。" % (args.host, args.port))
        return 1
    except OSError as e:
        eprint("错误：监听失败：%s" % e)
        return 1
    return 0

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "apply":
            return cmd_apply(args)
        if args.command == "restore":
            return cmd_restore(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "flushdns":
            return 0 if flush_dns() else 1
    except KeyboardInterrupt:
        eprint("已取消。")
        return 130
    except Exception as e:
        eprint("错误：%s" % e)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
